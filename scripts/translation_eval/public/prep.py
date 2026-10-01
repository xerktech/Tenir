"""Build the public eval sets (seeded, frozen before any model runs)."""
import csv, json, random, io, urllib.request
import pandas as pd
random.seed(20261001)
D = "/work/data"

def tsv(p):
    return list(csv.reader(open(p, encoding="utf-8"), delimiter="\t", quoting=csv.QUOTE_NONE))

en = {r[0]: r[2] for p in ("en_test.tsv", "en_dev.tsv") for r in tsv(f"{D}/{p}")}
es_rows = tsv(f"{D}/es_test.tsv")
by_id = {}
for r in es_rows:
    if r[0] in en:
        by_id.setdefault(r[0], []).append(r)
ids = sorted(by_id)
pick = random.sample(ids, 200)
clips, refs, items = [], {}, []
for i in pick:
    r = random.choice(by_id[i])
    clips.append({"id": i, "wav": f"{D}/test/{r[1]}"})
    refs[i] = {"es": r[2], "en": en[i]}
    items.append({"id": i, "set": "fleurs_gold", "text": r[2], "source_lang": "es", "ref": en[i]})
print("fleurs ids with en ref:", len(ids), "picked", len(pick))

url = "https://huggingface.co/datasets/Helsinki-NLP/opus-100/resolve/main/en-es/test-00000-of-00001.parquet"
df = pd.read_parquet(io.BytesIO(urllib.request.urlopen(url).read()))
pairs = [(t["es"].strip(), t["en"].strip()) for t in df["translation"]]
pairs = [p for p in pairs if 3 <= len(p[0].split()) <= 25 and 3 <= len(p[1].split()) <= 30]
for k, (s, t) in enumerate(random.sample(pairs, 300)):
    items.append({"id": f"opus{k}", "set": "opus_conv", "text": s, "source_lang": "es", "ref": t})

en_pool = sorted(set(en) - set(pick))
for i in random.sample(en_pool, 150):
    items.append({"id": f"en{i}", "set": "passthrough", "text": en[i], "source_lang": None, "ref": en[i]})

json.dump(clips, open(f"{D}/clips.json", "w"), indent=1)
json.dump(refs, open(f"{D}/fleurs_refs.json", "w"), ensure_ascii=False, indent=1)
json.dump(items, open(f"{D}/items.json", "w"), ensure_ascii=False, indent=1)
from collections import Counter; print(Counter(i["set"] for i in items))
