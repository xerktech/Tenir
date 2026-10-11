"""Build conversation-like 16 kHz s16le streams from FLEURS (XERK-1476). See README.md here.

Three Spanish-only streams and three alternating English/Spanish ones, ~3 min each, turns
separated by 0.8 s of silence; clips.json carries each stream's concatenated reference.
Usage: python build_streams.py FLEURS_ROOT OUT_DIR
"""

import csv
import json
import random
import sys
import wave
from pathlib import Path

import numpy as np

from fleurs_lang import load

root, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
rng = random.Random(14760)


def clips(d):
    w = sorted((root / d).rglob("*.wav"))
    rng.shuffle(w)
    return w


es, en = clips("es_419"), clips("en_us")
refs = {}
for d in ("es_419", "en_us"):
    for m in csv.reader(
        open(root / f"{d}.tsv"), delimiter="\t", quoting=csv.QUOTE_NONE
    ):
        refs[m[1]] = m[2]
gap = np.zeros(int(16000 * 0.8), np.float32)
out = []


def build(name, seq):
    parts, texts = [], []
    for p in seq:
        parts += [load(p), gap]
        texts.append(refs[p.name])
        if sum(len(x) for x in parts) > 16000 * 180:
            break
    pcm = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2")
    with wave.open(str(out_dir / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    out.append(
        {"id": name, "wav": str(out_dir / f"{name}.wav"), "ref": " ".join(texts)}
    )


for i in range(3):
    build(f"es{i}", es[i * 40 : (i + 1) * 40])
for i in range(3):
    build(
        f"mix{i}",
        [
            p
            for pair in zip(
                en[i * 30 : (i + 1) * 30], es[200 + i * 30 : 200 + (i + 1) * 30]
            )
            for p in pair
        ],
    )
json.dump(out, open(out_dir / "clips.json", "w"), indent=1)
