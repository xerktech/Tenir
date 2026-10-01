"""Score replay_text / ws_driver outputs against references.

Translation sets: COMET-22 (Unbabel/wmt22-comet-da), chrF++ and BLEU (sacrebleu).
Passthrough set (English in, should come back unchanged): exact-unchanged rate
and chrF vs the input. A missing/failed translation scores as an empty
hypothesis — the app would show nothing.

Usage: python score.py results/*.json --out scores.json
"""

import argparse
import json
import re
from collections import defaultdict

import sacrebleu
from comet import download_model, load_from_checkpoint

ap = argparse.ArgumentParser()
ap.add_argument("files", nargs="+")
ap.add_argument("--out", required=True)
a = ap.parse_args()

comet = load_from_checkpoint(download_model("Unbabel/wmt22-comet-da"))


def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip())


rows = {}
for path in a.files:
    data = json.load(open(path))
    sets = defaultdict(list)
    if isinstance(data, list) and data and "finals" in data[0]:
        # ws_driver output: one clip -> concat translations of its non-English finals
        refs = json.load(open("/work/data/fleurs_refs.json"))
        for clip in data:
            hyp = " ".join(f["translation"] for f in clip["finals"] if f.get("translation"))
            src = " ".join(f["text"] for f in clip["finals"])
            sets["fleurs_e2e"].append({"src": src, "hyp": hyp, "ref": refs[clip["id"]]["en"],
                                       "ms": [f["latency_ms"] for f in clip["finals"]
                                              if f.get("latency_ms") is not None],
                                       "untranslated": sum(1 for f in clip["finals"]
                                                           if not f.get("translation"))})
    else:
        clips = defaultdict(list)
        for r in data:
            if r["set"] == "fleurs_asr":
                clips[r["clip"]].append(r)
                continue
            sets[r["set"]].append({"src": r["text"], "hyp": r.get("hyp") or "", "ref": r["ref"],
                                   "ms": [r["ms"]] if "error" not in r else [],
                                   "untranslated": int(not r.get("hyp"))})
        for cid, parts in clips.items():
            parts.sort(key=lambda r: r["part"])
            sets["fleurs_asr"].append({
                "src": " ".join(p["text"] for p in parts),
                "hyp": " ".join((p.get("hyp") or "") for p in parts if p.get("hyp")),
                "ref": parts[0]["ref"],
                "ms": [p["ms"] for p in parts if p.get("ms") is not None and "error" not in p],
                "untranslated": sum(1 for p in parts if not p.get("hyp"))})
    res = {}
    for name, items in sets.items():
        ms = sorted(m for it in items for m in it["ms"])
        stat = {"n": len(items), "missing": sum(it["untranslated"] for it in items),
                "ms_mean": round(sum(ms) / len(ms)) if ms else None,
                "ms_p50": round(ms[len(ms) // 2]) if ms else None,
                "ms_p90": round(ms[int(0.9 * len(ms))]) if ms else None}
        hyps = [norm(it["hyp"]) for it in items]
        if name == "passthrough":
            stat["unchanged"] = round(sum(h == norm(it["src"]) for h, it in zip(hyps, items))
                                      / len(items), 4)
            stat["chrf_vs_input"] = round(sacrebleu.corpus_chrf(
                hyps, [[norm(it["src"]) for it in items]]).score, 2)
        else:
            refs_ = [norm(it["ref"]) for it in items]
            stat["chrf++"] = round(sacrebleu.corpus_chrf(hyps, [refs_], word_order=2).score, 2)
            stat["bleu"] = round(sacrebleu.corpus_bleu(hyps, [refs_]).score, 2)
            out = comet.predict([{"src": it["src"], "mt": h, "ref": r}
                                 for it, h, r in zip(items, hyps, refs_)],
                                batch_size=64, gpus=1, progress_bar=False)
            stat["comet22"] = round(out.system_score * 100, 2)
        res[name] = stat
    rows[path] = res
    print(path, json.dumps(res))
json.dump(rows, open(a.out, "w"), indent=1)
