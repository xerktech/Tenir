# Cue-quality eval harness

Replays recorded deployment transcripts through the **shipped** cue prompt
(`api.cue.openai.OpenAICueGenerator._build_payload` from the installed `api`
package) against a real chat model, then LLM-judges every emitted cue for
novelty, relevance, accuracy, and duplication. This is the harness behind the
numbers cited in `api/src/api/cue/tuning.py` and `docs/cue-rag.md`.

Not part of CI — it needs a GPU model endpoint and real conversation data.

## 1. Export transcripts from a deployment

```bash
docker exec Tenir-Postgres psql -U tenir -d tenir -tAc \
  "select json_agg(row_to_json(t)) from (select segment_id, conversation_id, \
   text, start_ms, end_ms, lang, speaker_id, speaker_label from segments \
   order by conversation_id, start_ms) t" > segments.json
```

## 2. Replay

```bash
cd api && pip install -e '.[dev]'   # the harness imports the api package
python scripts/cue_eval/replay.py segments.json \
  --endpoint http://10.10.10.22:9402/v1 --model Qwen/Qwen3.6-27B-FP8 \
  --out results.json [--conversations id1,id2,...]
```

Replays each conversation with the exact session gating: an 8-turn rolling
context window, one attempt in flight at a time (modelled as 2.5 s of
transcript time), the 1.5 s min interval between emitted cues, and all three
dedupe backstops (normalized title + substance fingerprint + title-subject
containment). Ungrounded only — the retrieval tiers need the live
SearXNG/Kiwix/RSS infrastructure.

## 3. Judge and report

```bash
python scripts/cue_eval/judge.py results.json segments.json \
  --endpoint http://10.10.10.22:9402/v1 --model Qwen/Qwen3.6-27B-FP8
python scripts/cue_eval/report.py results.judged.json
```

The judge grades each cue 0-2 on **novelty** (does the body add information not
in the transcript — 0 is a pure restatement), **relevance**, and **accuracy**,
and flags **duplicates** of earlier cues. The judge shares the generator's
weights, so treat absolute accuracy numbers as comparative, not ground truth —
spot-check the flagged cues by hand.

## Baseline numbers (2026-07, 12 recorded conversations)

| prompt | cues | attempts | novelty | relevance | accuracy | restatements | wrong | judged dups |
|---|---|---|---|---|---|---|---|---|
| pre-enrichment production cues | 61 | — | 1.49 | 1.90 | 1.74 | 8 | 6 | 8 |
| pre-enrichment prompt, replayed (t=0.2) | 8 | 566 | 1.88 | 2.00 | 2.00 | 0 | 0 | 0 |
| enrichment prompt, t=0.2 | 29 | 675 | 1.83 | 1.86 | 1.52 | 1 | 5 | 2 |
| enrichment prompt, t=0.0 (shipped) | 27 | 675 | 1.78 | 1.93 | 1.93 | 2 | 1 | 0 |

The pre-enrichment prompt was near-mute on replay (1.4% of attempts); the
production cues above it came from earlier, looser prompt versions plus the
grounded bar, and carried the restatement/duplicate/wrong-cue problems this
calibration removed. The shipped combination restores volume (~3.5x the
replayed baseline, before grounding adds more) at equal-or-better judged
quality; greedy decoding (t=0.0) cut judged-wrong cues 5 -> 1 at equal volume.
