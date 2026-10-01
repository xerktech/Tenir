"""Paired bootstrap on segment-level COMET-22, identical items, vs the Qwen baseline."""
import json, random
from collections import defaultdict
from comet import download_model, load_from_checkpoint
comet = load_from_checkpoint(download_model("Unbabel/wmt22-comet-da"))
R = "/work/results"
runs = {"qwen": f"{R}/qwen3.8-27b-fp8/text_shipped.json", "milmmt-1b": f"{R}/milmmt-1b/sub_milmmt.json",
        "milmmt-4b": f"{R}/milmmt-4b/sub_milmmt.json", "milmmt-12b": f"{R}/milmmt-12b/sub_milmmt.json",
        "hymt2-1.8b": f"{R}/hymt2-1.8b/sub_hymt.json", "hymt2-1.8b-shipped": f"{R}/hymt2-1.8b/text_shipped.json",
        "hymt2-7b": f"{R}/hymt2-7b/sub_hymt.json", "tgemma-4b": f"{R}/tgemma-4b/sub_tgemma.json"}
def units(path):
    """Scoring units: one per text item; one per clip for fleurs_asr (joined like the app)."""
    u, clips = {}, defaultdict(list)
    for r in json.load(open(path)):
        if r["set"] == "passthrough": continue
        if r["set"] == "fleurs_asr": clips[r["clip"]].append(r); continue
        u[r["id"]] = (r["set"], r["text"], r.get("hyp") or "", r["ref"], None)
    for c, ps in clips.items():
        ps.sort(key=lambda r: r["part"])
        u["asr:" + c] = ("fleurs_asr", " ".join(p["text"] for p in ps),
                         " ".join(p["hyp"] for p in ps if p.get("hyp")), ps[0]["ref"],
                         {p.get("app_lang") for p in ps if p.get("translate")})
    return u
U = {k: units(v) for k, v in runs.items()}
ids = sorted(set.intersection(*(set(u) for u in U.values())))
S = {}
for k, u in U.items():
    out = comet.predict([{"src": u[i][1], "mt": u[i][2], "ref": u[i][3]} for i in ids], batch_size=64, gpus=1, progress_bar=False)
    S[k] = dict(zip(ids, out.scores))
random.seed(0)
def boot(a, b, sel):
    d = [S[a][i] - S[b][i] for i in sel]; n = len(d); base = sum(d) / n
    bs = sorted(sum(random.choice(d) for _ in range(n)) / n for _ in range(2000))
    return base * 100, bs[50] * 100, bs[1949] * 100, sum(x > 0 for x in bs) / 2000
print("units", len(ids))
for k in runs:
    if k == "qwen": continue
    m, lo, hi, p = boot(k, "qwen", ids)
    print(f"{k:20s} - qwen: {m:+.2f} COMET  95%CI [{lo:+.2f},{hi:+.2f}]  P(better)={p:.2f}")
mis = [i for i in ids if U["qwen"][i][4] and U["qwen"][i][4] - {"es", None}]
print("asr clips with a non-es app tag:", len(mis), "of", sum(1 for i in ids if i.startswith("asr:")))
for k in runs:
    print(f"  mislabelled-clip mean COMET {k:20s} {sum(S[k][i] for i in mis)/max(1,len(mis))*100:.2f}")
