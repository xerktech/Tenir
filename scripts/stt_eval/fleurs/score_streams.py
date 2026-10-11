"""Score a ws_driver run over build_streams.py output (XERK-1476). See README.md here.

Usage: python score_streams.py clips.json run.json [API_BASE]
Long-form WER per stream and pooled, per-final language tags, and (with API_BASE, logging in as
TENIR_USERNAME / TENIR_PASSWORD) the api's stage.stt.* counters and latencies.
"""

import json
import os
import re
import sys
import unicodedata

import httpx
import jiwer


def norm(t):
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


if len(sys.argv) not in (3, 4):
    sys.exit(__doc__)
clips = {c["id"]: c for c in json.load(open(sys.argv[1]))}
run = json.load(open(sys.argv[2]))
refs, hyps = [], []
for r in sorted(run, key=lambda r: r["id"]):
    hyp = " ".join(f["text"] for f in r["finals"])
    tags = {}
    for f in r["finals"]:
        tags[f.get("lang")] = tags.get(f.get("lang"), 0) + 1
    w = jiwer.wer(norm(clips[r["id"]]["ref"]), norm(hyp) or "x")
    refs.append(norm(clips[r["id"]]["ref"]))
    hyps.append(norm(hyp) or "x")
    print(
        f"{r['id']}: WER={w * 100:.1f} finals={len(r['finals'])} tags={tags} err={r.get('error')}"
    )
for k in ("es", "mix"):
    ix = [
        i
        for i, r in enumerate(sorted(run, key=lambda r: r["id"]))
        if r["id"].startswith(k)
    ]
    print(
        k,
        "pooled WER",
        round(jiwer.wer([refs[i] for i in ix], [hyps[i] for i in ix]) * 100, 1),
    )
if len(sys.argv) > 3:
    b = sys.argv[3]
    tok = httpx.post(
        f"{b}/auth/login",
        json={
            "username": os.environ["TENIR_USERNAME"],
            "password": os.environ["TENIR_PASSWORD"],
        },
    ).json()["token"]
    m = httpx.get(f"{b}/metrics", headers={"Authorization": f"Bearer {tok}"}).json()

    def walk(d, p=""):
        for k, v in d.items():
            if isinstance(v, dict) and k in ("counters", "latency_ms"):
                walk(v, p + k + ".")
            elif "stt" in p + k:
                print(
                    " ",
                    p + k,
                    {a: round(b) for a, b in v.items()} if isinstance(v, dict) else v,
                )

    walk(m)
