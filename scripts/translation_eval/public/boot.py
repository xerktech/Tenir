"""Paired bootstrap on segment-level COMET-22 vs the Qwen baseline, per set and pooled.

Every run is restricted to the items Qwen's shipped-prompt run covered (replay_text's
fixed subset), so all deltas are on identical units: one per text item, one per clip
for fleurs_asr (its finals joined, as the app shows them). Reads each model's native
run (text_<best_mode>.json) plus the shipped-prompt runs that parsed.

Usage (in the pod, or anywhere with the results tree):
  /work/venv/bin/python boot.py [--results /work/results] [--cpu]
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--results", default="/work/results", type=Path)
ap.add_argument("--cpu", action="store_true", help="score COMET on CPU (~35 min)")
a = ap.parse_args()
R = a.results


def native(tag: str) -> Path:
    return R / tag / f"text_{(R / tag / 'best_mode').read_text().strip()}.json"


runs = {
    "qwen": R / "qwen3.8-27b-fp8/text_shipped.json",
    **{t: native(t) for t in ("milmmt-1b", "milmmt-4b", "milmmt-12b", "hymt2-1.8b",
                              "hymt2-7b", "tgemma-4b", "tgemma-12b")},
    # the only candidates that survive the shipped prompt (zero-code swaps)
    "hymt2-1.8b-shipped": R / "hymt2-1.8b/text_shipped.json",
    "hymt2-7b-shipped": R / "hymt2-7b/text_shipped.json",
}
for path in runs.values():
    if not path.is_file():
        raise SystemExit(f"missing {path}")


def units(path: Path) -> dict:
    u, clips = {}, defaultdict(list)
    for r in json.load(open(path)):
        if r["set"] == "passthrough":
            continue
        if r["set"] == "fleurs_asr":
            clips[r["clip"]].append(r)
            continue
        u[r["id"]] = (r["set"], r["text"], r.get("hyp") or "", r["ref"], None)
    for c, ps in clips.items():
        ps.sort(key=lambda r: r["part"])
        u["asr:" + c] = ("fleurs_asr", " ".join(p["text"] for p in ps),
                         " ".join(p["hyp"] for p in ps if p.get("hyp")), ps[0]["ref"],
                         {p.get("app_lang") for p in ps if p.get("translate")})
    return u


from comet import download_model, load_from_checkpoint  # noqa: E402 - slow import, after checks

comet = load_from_checkpoint(download_model("Unbabel/wmt22-comet-da"))
U = {k: units(v) for k, v in runs.items()}
ids = sorted(set.intersection(*(set(u) for u in U.values())))
S = {}
for k, u in U.items():
    out = comet.predict([{"src": u[i][1], "mt": u[i][2], "ref": u[i][3]} for i in ids],
                        batch_size=64, gpus=0 if a.cpu else 1, progress_bar=False)
    S[k] = dict(zip(ids, out.scores))

random.seed(0)


def boot(model: str, sel: list[str]) -> str:
    d = [S[model][i] - S["qwen"][i] for i in sel]
    n = len(d)
    bs = sorted(sum(random.choice(d) for _ in range(n)) / n for _ in range(2000))
    return f"{sum(d) / n * 100:+.2f} [{bs[50] * 100:+.2f},{bs[1949] * 100:+.2f}]"


groups = {
    "fleurs_gold": [i for i in ids if U["qwen"][i][0] == "fleurs_gold"],
    "fleurs_asr": [i for i in ids if U["qwen"][i][0] == "fleurs_asr"],
    "speech (gold+asr)": [i for i in ids if U["qwen"][i][0] != "opus_conv"],
    "opus_conv": [i for i in ids if U["qwen"][i][0] == "opus_conv"],
    "all": ids,
}
print("units:", {g: len(v) for g, v in groups.items()})
print(f"{'model':20s} " + " ".join(f"{g:>22s}" for g in groups))
for k in runs:
    if k != "qwen":
        print(f"{k:20s} " + " ".join(f"{boot(k, sel):>22s}" for sel in groups.values()))

mis = [i for i in ids if U["qwen"][i][4] and U["qwen"][i][4] - {"es", None}]
print(f"\nfleurs_asr clips with a non-es app tag: {len(mis)}")
for k in runs:
    print(f"  {k:20s} mean COMET on them {sum(S[k][i] for i in mis) / max(1, len(mis)) * 100:.2f}")
