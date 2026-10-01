import json, glob
full = json.load(open("/work/results/scores.json")); sub = json.load(open("/work/results/scores_sub.json"))
vram = {d.split("/")[3]: max(int(l) for l in open(d) if l.strip()) for d in glob.glob("/work/results/*/vram_trace.txt")}
def row(s, k, m): return s.get(k, {}).get(m)
print("== subset (gold60 opus100 pass50 asr100clips)")
for f, s in sorted(sub.items()):
    print(f"{f.split('/')[1]:16s} {f.split('/')[-1]:18s} gold={row(s,'fleurs_gold','comet22')} opus={row(s,'opus_conv','comet22')} "
          f"asr={row(s,'fleurs_asr','comet22')} pass_unch={row(s,'passthrough','unchanged')} pass_chrf={row(s,'passthrough','chrf_vs_input')} miss={row(s,'fleurs_asr','missing')}")
q = full["results/qwen3.8-27b-fp8/text_shipped.json"]
print("qwen shipped   ", {k: (v.get("comet22"), v.get("unchanged"), v.get("chrf_vs_input")) for k, v in q.items()})
print("== e2e (200 clips through Tenir app)")
for f, s in sorted(full.items()):
    if "e2e" in f:
        e = s["fleurs_e2e"]; t = f.split("/")[1]
        print(f"{t:16s} comet={e['comet22']} chrf={e['chrf++']} bleu={e['bleu']} lat={e['ms_mean']}/{e['ms_p90']} untranslated_turns={e['missing']} vram_peak={vram.get(t)}")
print("== full native text sets")
for f, s in sorted(full.items()):
    if "/text_" in f and "shipped" not in f:
        print(f"{f.split('/')[1]:16s} gold={s['fleurs_gold']['comet22']} opus={s['opus_conv']['comet22']} asr={s['fleurs_asr']['comet22']} "
              f"pass_unch={s['passthrough']['unchanged']} pass_chrf={s['passthrough']['chrf_vs_input']} asr_ms={s['fleurs_asr']['ms_mean']}/{s['fleurs_asr']['ms_p90']}")
