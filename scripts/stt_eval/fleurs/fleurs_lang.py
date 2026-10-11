"""FLEURS es/en/fr through Tenir's own STT client (XERK-1476). See README.md here.

Per clip: hypothesis via api.stt.parakeet.ParakeetEngine (the production client, so its
non-speech filter applies), WER vs the FLEURS transcription, the language Tenir would tag
(engine language, else api's detect_lang), wall time, and the first 1.5 s decoded as an early
partial. Then silence/noise windows as a hallucination probe.
"""

import argparse
import csv
import json
import random
import re
import sys
import time
import unicodedata
from pathlib import Path

import jiwer
import numpy as np
from api.stt.engine import pcm16_to_float32
from api.stt.langid import detect_lang
from api.stt.parakeet import ParakeetEngine

LANGS = {"es_419": "es", "en_us": "en", "fr_fr": "fr"}


def norm(t):
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def load(p):
    # FLEURS ships IEEE-float WAV (format 3), which the wave module refuses.
    b = Path(p).read_bytes()
    i = 12
    fmt = None
    while i < len(b):
        cid, n = b[i : i + 4], int.from_bytes(b[i + 4 : i + 8], "little")
        if cid == b"fmt ":
            tag, ch, sr = (
                int.from_bytes(b[i + 8 : i + 10], "little"),
                int.from_bytes(b[i + 10 : i + 12], "little"),
                int.from_bytes(b[i + 12 : i + 16], "little"),
            )
            bits = int.from_bytes(b[i + 22 : i + 24], "little")
            fmt = (tag, ch, sr, bits)
        elif cid == b"data":
            d = b[i + 8 : i + 8 + n]
            assert fmt[1] == 1 and fmt[2] == 16000, (p, fmt)
            return (
                np.frombuffer(d, "<f4").copy() if fmt[0] == 3 else pcm16_to_float32(d)
            )
        i += 8 + n + (n & 1)
    raise ValueError(p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--pinned", action="store_true")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--words",
        action="store_true",
        help="want_words=True (finals in prod pass False)",
    )
    a = ap.parse_args()
    eng = ParakeetEngine(endpoint=a.endpoint, model=a.model, timeout=30)
    root = Path(a.root)
    rows = []
    rng = random.Random(1476)
    for d, lang in LANGS.items():
        meta = list(
            csv.reader(open(root / f"{d}.tsv"), delimiter="\t", quoting=csv.QUOTE_NONE)
        )
        wavs = {p.name: p for p in (root / d).rglob("*.wav")}
        meta = [m for m in meta if m[1] in wavs]
        seen = set()
        picks = []
        for m in rng.sample(meta, len(meta)):
            if m[1] in seen:
                continue
            seen.add(m[1])
            picks.append(m)
            if len(picks) == a.n:
                break
        for m in picks:
            x = load(wavs[m[1]])
            x = x[: 16000 * 30]  # whole clip (FLEURS clips are ~5-25 s)
            t0 = time.perf_counter()
            for attempt in range(10):
                try:
                    r = eng.transcribe(
                        x, language=lang if a.pinned else None, want_words=a.words
                    )
                    break
                except Exception as e:  # port-forward restarts
                    print("retry", m[1], repr(e)[:80], file=sys.stderr)
                    time.sleep(3)
                    t0 = time.perf_counter()
            ms = (time.perf_counter() - t0) * 1000
            rows.append(
                dict(
                    set=lang,
                    id=m[1],
                    ref=m[2],
                    hyp=r.text,
                    ms=ms,
                    dur=len(x) / 16000,
                    full=True,
                    tag=r.language or detect_lang(r.text),
                    words=len(r.words),
                )
            )
            # an early partial: the first 1.5 s of the turn, auto-language, no words
            if not a.pinned:
                for attempt in range(10):
                    try:
                        rs = eng.transcribe(x[:24000], language=None, want_words=False)
                        break
                    except Exception:
                        time.sleep(3)
                rows[-1]["p15_hyp"] = rs.text
                rows[-1]["p15_tag"] = rs.language or detect_lang(rs.text)
        print(d, "done", file=sys.stderr)
    # hallucination probe: windows that VAD might pass on as a turn tail or a partial
    rng2 = np.random.default_rng(1476)
    probes = []
    for kind, n in [
        ("silence", 0.0),
        ("noise_0.003", 0.003),
        ("noise_0.01", 0.01),
        ("noise_0.03", 0.03),
    ]:
        for dur in (1, 3, 6):
            x = (rng2.standard_normal(16000 * dur) * n).astype(np.float32)
            r = eng.transcribe(x, language=None, want_words=False)
            probes.append(dict(kind=kind, dur=dur, hyp=r.text))
    json.dump(
        dict(rows=rows, probes=probes), open(a.out, "w"), ensure_ascii=False, indent=1
    )
    for lang in LANGS.values():
        rs = [r for r in rows if r["set"] == lang]
        full = [r for r in rs if r["full"]]
        w = jiwer.wer(
            [norm(r["ref"]) for r in full], [norm(r["hyp"]) or "x" for r in full]
        )
        ms = sorted(r["ms"] for r in rs)
        tags = {}
        for r in rs:
            tags[r["tag"]] = tags.get(r["tag"], 0) + 1
        blank = sum(1 for r in rs if not r["hyp"].strip())
        ptags = {}
        for r in rs:
            if "p15_tag" in r:
                ptags[r["p15_tag"]] = ptags.get(r["p15_tag"], 0) + 1
        print(f"{lang}: 1.5s-partial tags={ptags}")
        print(
            f"{lang}: n={len(rs)} WER={w * 100:.1f} tag={tags} blank={blank} "
            f"ms p50={ms[len(ms) // 2]:.0f} p90={ms[int(len(ms) * 0.9)]:.0f} max={ms[-1]:.0f}"
        )
    print(
        "hallucination probes (non-empty):",
        [(p["kind"], p["dur"], p["hyp"]) for p in probes if p["hyp"].strip()],
    )


if __name__ == "__main__":
    main()
