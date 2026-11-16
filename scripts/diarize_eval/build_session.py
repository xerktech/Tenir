"""Build a synthetic "dinner table" session with known speakers (XERK-2958).

Real recordings have no speaker ground truth, so speaker tracking is measured on
LibriSpeech utterances laid out like a table conversation: N speakers taking turns,
short gaps, back-channels and people talking over each other, each speaker at their
own distance from one mono mic (gain + a little reverb), plus room noise.

    python build_session.py LibriSpeech/dev-clean out/session --speakers 4 --minutes 4

Writes ``out/session.wav`` (16 kHz s16le mono) and ``out/session.json``: every
utterance with its speaker (1-based, in order of first appearance), start/end seconds
and reference text.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 16_000


def load_corpus(root: Path) -> dict[str, list[tuple[Path, str]]]:
    """speaker id -> [(flac, text)] for a LibriSpeech split."""
    out: dict[str, list[tuple[Path, str]]] = {}
    for trans in sorted(root.glob("*/*/*.trans.txt")):
        for line in trans.read_text().splitlines():
            uid, text = line.split(" ", 1)
            spk = uid.split("-")[0]
            out.setdefault(spk, []).append((trans.parent / f"{uid}.flac", text.lower()))
    return out


def room(x: np.ndarray, rng: random.Random, distance: float) -> np.ndarray:
    """Cheap far-field: exponentially decaying noise RIR, wetter with distance."""
    t60 = 0.25 + 0.25 * distance
    n = int(SR * t60)
    tail = np.random.default_rng(rng.randrange(1 << 30)).standard_normal(n)
    tail *= np.exp(-6.9 * np.arange(n) / n)
    rir = np.zeros(n)
    rir[0] = 1.0
    rir += 0.25 * distance * tail / np.abs(tail).max()
    y = np.convolve(x, rir)[: len(x)]
    return y / (np.abs(y).max() + 1e-9) * np.abs(x).max()


def build(
    corpus_root: Path,
    out: Path,
    speakers: int,
    minutes: float,
    seed: int,
    overlap_p: float,
    snr_db: float,
    parallel: bool = False,
) -> None:
    rng = random.Random(seed)
    corpus = load_corpus(corpus_root)
    ids = rng.sample(sorted(corpus), speakers)
    gains = [10 ** (rng.uniform(-8, 0) / 20) for _ in ids]
    dist = [rng.uniform(0.2, 1.0) for _ in ids]
    pools = {s: rng.sample(corpus[s], len(corpus[s])) for s in ids}

    total = int(minutes * 60 * SR)
    mix = np.zeros(total + 30 * SR, np.float32)
    utts: list[dict] = []
    # --parallel: the table splits into two conversations held at once (half the speakers
    # each), the case separation exists for. Otherwise one conversation, everyone in it.
    groups = [ids[::2], ids[1::2]] if parallel else [ids]
    end_t = 0.0
    for group in groups:
        end_t = max(
            end_t,
            _converse(group, ids, rng, corpus, pools, gains, dist, mix, utts, total, overlap_p),
        )
    utts.sort(key=lambda u: u["start"])
    order: list[str] = []
    for u in utts:
        if u["libri"] not in order:
            order.append(u["libri"])
        u["speaker"] = order.index(u["libri"]) + 1
    t = end_t
    mix = mix[: int((t + 1.0) * SR)]
    finish(mix, utts, t, out, seed, snr_db, speakers)


def _converse(group, ids, rng, corpus, pools, gains, dist, mix, utts, total, overlap_p) -> float:
    """Lay one conversation among ``group`` into ``mix``; append its utterances to ``utts``."""
    mine: list[dict] = []
    t = 0.5
    last = None
    while t * SR < total:
        # Mostly alternate between people; sometimes the same person goes on.
        spk = rng.choice(
            [s for s in group if s != last]
            if last and len(group) > 1 and rng.random() < 0.8
            else group
        )
        if not pools[spk]:
            pools[spk] = rng.sample(corpus[spk], len(corpus[spk]))
        path, text = pools[spk].pop()
        x, sr = sf.read(path, dtype="float32")
        assert sr == SR
        backchannel = False
        if rng.random() < 0.12 and mine:
            # A short "yes"/"mm" inside someone else's turn: first second of an utterance.
            x = x[: int(SR * rng.uniform(0.5, 1.0))]
            backchannel = True
        x = room(x, rng, dist[ids.index(spk)]) * gains[ids.index(spk)]
        if mine and rng.random() < overlap_p:
            prev = mine[-1]
            start = max(prev["start"] + 0.3, prev["end"] - rng.uniform(0.5, 2.5))
        else:
            start = t + rng.uniform(0.1, 0.8)
        a = int(start * SR)
        mix[a : a + len(x)] += x
        mine.append(
            {
                "speaker": 0,  # numbered by first appearance once every group is laid out
                "libri": spk,
                "start": round(start, 3),
                "end": round(start + len(x) / SR, 3),
                "text": None if backchannel else text,
                "backchannel": backchannel,
            }
        )
        t = max(t, start + len(x) / SR)
        last = spk
    utts.extend(mine)
    return t


def finish(mix, utts, t, out, seed, snr_db, speakers) -> None:
    speech_rms = float(np.sqrt(np.mean(mix[np.abs(mix) > 1e-4] ** 2)))
    noise = np.random.default_rng(seed).standard_normal(len(mix)).astype(np.float32)
    noise = np.convolve(noise, np.ones(8) / 8, mode="same")  # pinkish
    noise *= speech_rms / (np.sqrt(np.mean(noise**2)) + 1e-9) * 10 ** (-snr_db / 20)
    mix = mix + noise
    mix = 0.7 * mix / np.abs(mix).max()
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out.with_suffix(".wav"), mix, SR, subtype="PCM_16")
    out.with_suffix(".json").write_text(
        json.dumps({"speakers": speakers, "seconds": len(mix) / SR, "utterances": utts}, indent=1)
    )
    ov = sum(1 for i in range(1, len(utts)) if utts[i]["start"] < utts[i - 1]["end"])
    print(f"{out}: {len(mix) / SR:.0f}s, {len(utts)} utterances, {ov} overlapped starts")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", type=Path, help="LibriSpeech split dir (e.g. dev-clean)")
    ap.add_argument("out", type=Path, help="output path stem")
    ap.add_argument("--speakers", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--overlap", type=float, default=0.3, help="share of turns that overlap")
    ap.add_argument("--snr", type=float, default=20.0, help="speech-to-noise dB")
    ap.add_argument("--parallel", action="store_true", help="two conversations at once")
    a = ap.parse_args()
    build(a.corpus, a.out, a.speakers, a.minutes, a.seed, a.overlap, a.snr, a.parallel)


if __name__ == "__main__":
    main()
