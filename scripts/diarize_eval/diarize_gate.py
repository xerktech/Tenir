"""Replay the separated-caption gate offline on recorded pieces (XERK-3069).

Reads the ``pieces.jsonl`` that ``diarize_pieces.py`` wrote, so a threshold sweep costs no
decode. Two jobs:

``mixes`` — build overlap with known words from a real recording. Solo pieces Whisper is sure
of (not overlapped, no separated stream, ``avg_logprob`` ≥ ``--ref-logprob``) become
references; one speaker's clips are chained into a stretch, and stretches of two different
diarizer speakers are summed with a small offset and gain, one pair per turn, into a
session WAV and a ``truth.json``. Real voices, room and
language; only the overlap is made. A reference is Whisper's solo decode, so a score is
"words the separated decode shares with the clean decode", not hand truth.

    python diarize_gate.py mixes out/dinner/pieces.jsonl out/mixes --seed 1

``replay`` — for each policy, choose each piece's caption the way
``StreamingTranscriber._finalize_pieces`` does (same de-dup), then report, per detected
language of the separated decode: how many separated pieces each threshold drops, and with
``--truth`` (a ``mixes`` session) word precision / recall of the captions per policy and the
word accuracy of separated pieces by ``avg_logprob`` band.

    python diarize_gate.py replay out/mixes/pieces.jsonl --truth out/mixes/truth.json
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import wave
from collections import Counter, defaultdict
from pathlib import Path

SR = 16_000
DUPLICATE_SHARE = 0.6  # streaming._DUPLICATE_PIECE_SHARE
GATES = (None, -0.4, -0.5, -0.6, -0.7, -0.8, -1.0)  # None: keep every separated piece
BANDS = (-0.3, -0.5, -0.8)  # band edges for accuracy by avg_logprob


def words(text: str) -> list[str]:
    """Case-folded words, accents kept (Spanish and Portuguese words differ by them)."""
    return re.findall(r"[^\W_]+(?:'[^\W_]+)?", text.casefold())


def _tokens(text: str) -> set[str]:
    # Same normalisation as streaming._tokens, so the de-dup replays exactly.
    return {w.strip(".,!?¿¡;:\"'").casefold() for w in text.split()} - {""}


def _repeats(earlier: str, text: str) -> bool:
    t = _tokens(text)
    return bool(t) and len(t & _tokens(earlier)) / len(t) >= DUPLICATE_SHARE


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _decoded(d: dict | None) -> bool:
    return (
        bool(d) and "error" not in d and not d.get("non_speech") and bool(d.get("text"))
    )


def captions(pieces: list[dict], policy: str, gate: float | None) -> list[dict]:
    """Captions one policy produces. ``policy`` is ``sep`` (separated stream, dropped below
    ``gate``), ``sep+mix`` (a gated piece captions its mixed span instead) or ``mix`` (no
    separation: every piece decoded from the turn's own audio)."""
    out: list[dict] = []
    turn, emitted = None, []
    for p in pieces:
        if p["turn_start_ms"] != turn:
            turn, emitted = p["turn_start_ms"], []
        d = p["sep"] if policy != "mix" and "sep" in p else p["mix"]
        lp = d.get("avg_logprob")
        if d is p.get("sep") and gate is not None and lp is not None and lp < gate:
            if policy != "sep+mix":
                continue
            d = p["mix"]
        if not _decoded(d):
            continue
        text = d["text"]
        if p["overlap"] and any(
            q["start_ms"] < p["end_ms"]
            and p["start_ms"] < q["end_ms"]
            and _repeats(t, text)
            for q, t in emitted
        ):
            continue
        emitted.append((p, text))
        out.append(
            {
                "n": p["n"],
                "start_ms": p["start_ms"],
                "end_ms": p["end_ms"],
                "text": text,
            }
        )
    return out


def pool(
    runs: list[list[dict]], truths: list[dict] | None
) -> tuple[list[dict], dict | None]:
    """Several runs as one timeline: each run's times shifted past the previous run's end,
    so pieces and mixes of different sessions never overlap."""
    pieces: list[dict] = []
    mixes: list[dict] = []
    shift = 0
    for i, run in enumerate(runs):
        ends = [p["end_ms"] for p in run]
        if truths:
            ends += [m["end_ms"] for m in truths[i]["mixes"]]
            mixes += [
                {**m, "start_ms": m["start_ms"] + shift, "end_ms": m["end_ms"] + shift}
                for m in truths[i]["mixes"]
            ]
        for p in run:
            pieces.append(
                {
                    **p,
                    "n": len(pieces),
                    "turn_start_ms": p["turn_start_ms"] + shift,
                    "start_ms": p["start_ms"] + shift,
                    "end_ms": p["end_ms"] + shift,
                }
            )
        shift += max(ends, default=0) + 1
    return pieces, ({"mixes": mixes} if truths else None)


def _bag_match(ref: Counter, hyp: list[str]) -> int:
    return sum((ref & Counter(hyp)).values())


def _band(lp: float | None) -> str:
    if lp is None:
        return "none"
    for edge in BANDS:
        if lp >= edge:
            return f">={edge}"
    return f"<{BANDS[-1]}"


def replay(pieces: list[dict], truth: dict | None) -> dict:
    sep = [p for p in pieces if _decoded(p.get("sep"))]
    report: dict = {"pieces": len(pieces), "separated": len(sep), "by_lang": {}}
    by_lang: dict[str, list[dict]] = defaultdict(list)
    for p in sep:
        by_lang[p["sep"].get("lang") or "?"].append(p)
    for lang, ps in sorted(by_lang.items()):
        lps = [
            p["sep"]["avg_logprob"]
            for p in ps
            if p["sep"].get("avg_logprob") is not None
        ]
        report["by_lang"][lang] = {
            "separated": len(ps),
            "dropped": {
                str(g): sum(lp < g for lp in lps) for g in GATES if g is not None
            },
        }
    if truth is None:
        return report

    # A piece's reference: the words of the mixes it overlaps (both voices), so the
    # separated stream gets credit for whichever voice it carries.
    mixes = truth["mixes"]

    def ref_for(start: int, end: int) -> Counter:
        c: Counter = Counter()
        for m in mixes:
            if m["start_ms"] < end and start < m["end_ms"]:
                for r in m["refs"]:
                    c.update(words(r["text"]))
        return c

    total_ref = sum(len(words(r["text"])) for m in mixes for r in m["refs"])
    policies = {"mix (no separation)": ("mix", None)}
    policies.update({f"sep gate {g}": ("sep", g) for g in GATES})
    policies.update(
        {f"sep gate {g} else mix": ("sep+mix", g) for g in GATES if g is not None}
    )
    report["policies"] = {}
    for name, (pol, gate) in policies.items():
        caps = captions(pieces, pol, gate)
        # Each mix's words can be credited once across all captions over it.
        hyp_n = good = 0
        left = {i: Counter() for i in range(len(mixes))}
        for i, m in enumerate(mixes):
            for r in m["refs"]:
                left[i].update(words(r["text"]))
        for c in caps:
            hw = words(c["text"])
            hyp_n += len(hw)
            for i, m in enumerate(mixes):
                if m["start_ms"] < c["end_ms"] and c["start_ms"] < m["end_ms"]:
                    hit = left[i] & Counter(hw)
                    left[i] -= hit
                    hw = list((Counter(hw) - hit).elements())
                    good += sum(hit.values())
        report["policies"][name] = {
            "precision": round(good / hyp_n, 3) if hyp_n else None,
            "recall": round(good / total_ref, 3) if total_ref else None,
            "words": hyp_n,
        }

    # Word accuracy of each separated decode by avg_logprob band and language.
    acc: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: defaultdict(lambda: [0, 0])
    )
    for p in sep:
        hw = words(p["sep"]["text"])
        right = _bag_match(ref_for(p["start_ms"], p["end_ms"]), hw)
        cell = acc[p["sep"].get("lang") or "?"][_band(p["sep"].get("avg_logprob"))]
        cell[0] += right
        cell[1] += len(hw)
    report["sep_word_accuracy"] = {
        lang: {b: f"{r}/{n} ({r / n:.0%})" for b, (r, n) in sorted(bands.items()) if n}
        for lang, bands in sorted(acc.items())
    }
    return report


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


def build_mixes(
    pieces: list[dict], src: Path, out: Path, args: argparse.Namespace
) -> dict:
    """Two speakers' confident solo stretches summed, one pair per turn.

    A stretch chains one speaker's solo clips until it is ``--stretch-s`` long: the
    diarizer separates only overlaps of ``MIN_SEPARATE_OVERLAP_S`` (1.5 s) or more, and
    most clean clips of a real dinner are shorter than that. Each clip is used once."""
    import numpy as np

    rng = random.Random(args.seed)
    by_speaker: dict[int, list[dict]] = defaultdict(list)
    for p in pieces:
        if (
            not p["overlap"]
            and "sep" not in p
            and _decoded(p["mix"])
            and (p["mix"].get("avg_logprob") or -9) >= args.ref_logprob
            and args.min_s * 1000 <= p["end_ms"] - p["start_ms"] <= args.max_s * 1000
            and len(words(p["mix"]["text"])) >= 2
        ):
            by_speaker[p["speaker"]].append(p)
    for clips in by_speaker.values():
        rng.shuffle(clips)
    pause = np.zeros(int(0.15 * SR), dtype=np.float32)

    def stretch(speaker: int) -> tuple[np.ndarray, dict]:
        parts, texts, n = [], [], 0
        while by_speaker[speaker] and n < args.stretch_s * SR:
            p = by_speaker[speaker].pop()
            x = np.frombuffer(_read_wav(src / p["mix"]["wav"]), dtype=np.int16)
            parts += [x.astype(np.float32), pause]
            texts.append(p["mix"]["text"])
            n += len(x) + len(pause)
        ref = {"speaker": speaker, "lang": p["mix"]["lang"], "text": " ".join(texts)}
        return np.concatenate(parts[:-1]), ref

    gap = np.zeros(int(args.gap_s * SR), dtype=np.float32)
    session = [gap]
    mixes, t = [], len(gap)
    while len(live := sorted(k for k, v in by_speaker.items() if v)) >= 2:
        sa, sb = rng.sample(live, 2)
        xa, ra = stretch(sa)
        xb, rb = stretch(sb)
        # Level the two voices, then set b at a random ±gain_db against a.
        xb *= (np.sqrt(np.mean(xa**2)) + 1e-6) / (np.sqrt(np.mean(xb**2)) + 1e-6)
        xb *= 10 ** (rng.uniform(-args.gain_db, args.gain_db) / 20)
        off = int(rng.uniform(0.1, args.max_offset_s) * SR)
        n = max(len(xa), off + len(xb))
        mix = np.zeros(n, dtype=np.float32)
        mix[: len(xa)] += xa
        mix[off : off + len(xb)] += xb
        mixes.append(
            {
                "start_ms": t * 1000 // SR,
                "end_ms": (t + n) * 1000 // SR,
                "refs": [ra, rb],
            }
        )
        session += [mix, gap]
        t += n + len(gap)
    pcm = np.concatenate(session)
    # Low dither: VAD and Sortformer never see digital silence in a real room.
    pcm += np.random.default_rng(args.seed).normal(0, 3.0, len(pcm)).astype(np.float32)
    peak = np.abs(pcm).max()
    if peak > 32000:
        pcm *= 32000 / peak
    out.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out / "session.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.astype(np.int16).tobytes())
    truth = {"source": str(src), "seed": args.seed, "mixes": mixes}
    (out / "truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1))
    return truth


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mixes")
    m.add_argument(
        "pieces", type=Path, nargs="+", help="pieces.jsonl from diarize_pieces.py"
    )
    m.add_argument("out", type=Path)
    m.add_argument("--seed", type=int, default=1)
    m.add_argument("--ref-logprob", type=float, default=-0.3)
    m.add_argument("--min-s", type=float, default=0.8)
    m.add_argument("--max-s", type=float, default=8.0)
    m.add_argument("--stretch-s", type=float, default=4.0)
    m.add_argument("--gain-db", type=float, default=4.0)
    m.add_argument("--max-offset-s", type=float, default=0.5)
    m.add_argument("--gap-s", type=float, default=2.0)
    r = sub.add_parser("replay")
    r.add_argument("pieces", type=Path, nargs="+")
    r.add_argument(
        "--truth", type=Path, nargs="*", help="one truth.json per pieces file"
    )
    args = ap.parse_args(argv)
    if args.cmd == "mixes":
        *srcs, args.out = args.pieces + [args.out]
        # Each source's pieces carry speaker ids of their own session: keep pairs within one.
        mixes = []
        for i, src in enumerate(srcs):
            truth = build_mixes(load(src), src.parent, args.out / str(i), args)
            mixes.append(len(truth["mixes"]))
        print(f"mixes per source: {mixes}")
    else:
        if args.truth and len(args.truth) != len(args.pieces):
            ap.error("--truth needs one truth.json per pieces file")
        pieces, truth = pool(
            [load(p) for p in args.pieces],
            [json.loads(t.read_text()) for t in args.truth] if args.truth else None,
        )
        json.dump(replay(pieces, truth), sys.stdout, ensure_ascii=False, indent=1)
        print()


if __name__ == "__main__":
    main()
