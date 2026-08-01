# Translation eval harness

Measures the live-translation path (XERK-160) the same way `scripts/cue_eval/`
measures cues: replay real deployment utterances through the **shipped**
`OpenAITranslator` payload/parser from the installed `api` package, then
LLM-judge the outputs and compare runs. Built for XERK-180 (dedicated
translation model investigation); results narrative in
`RESULTS-2026-08.md`.

Deployment transcripts are private family conversations: keep exports and
results in a scratch directory outside the repo, and never paste transcript
content into shipped code, prompts, tests, or docs.

## 1. Export and select the data

```bash
# includes lang + translation columns on top of the cue_eval export
docker exec Tenir-Postgres psql -U tenir -d tenir -tAc \
  "select json_agg(row_to_json(t)) from (select segment_id, conversation_id, \
   text, start_ms, end_ms, lang, speaker_id, speaker_label, translation \
   from segments order by conversation_id, start_ms) t" > segments.json

python select_data.py segments.json --out eval_set.json
```

`select_data.py` mirrors the production trigger exactly: effective language is
stored `lang` else `detect_lang`, non-English opens/extends a run, undetected
segments inside a run are translated with `source_lang=None`. The set therefore
includes utterances that are *actually English but misdetected* — production
translates those too, and a candidate model is graded on leaving them alone.

## 2. Replay a candidate

```bash
cd api && pip install -e '.[dev]'   # replay imports the installed package

# production baseline (LiteLLM injects reasoning_effort medium in prod)
python replay.py eval_set.json --endpoint http://10.10.10.22:9402/v1 \
  --model gpt-oss:120b --reasoning-effort medium --out results.120b-medium.json

# a dedicated candidate served by the same Ollama (pull it first:
#   curl -X POST http://10.10.10.22:9402/api/pull -d '{"model":"gemma3:12b"}')
python replay.py eval_set.json --endpoint http://10.10.10.22:9402/v1 \
  --model gemma3:12b --out results.gemma3-12b.json
```

- `--workers 4` (default) matches `OLLAMA_NUM_PARALLEL`; use for accuracy
  sweeps. For latency numbers run `--workers 1` (production holds one
  translation in flight per session) — a concurrent run's `call_ms` includes
  queueing.
- Candidate models must clear the same bar as production: same prompt, same
  `response_format: json_object`, same `_parse`. A model that can't reliably
  emit the JSON envelope fails closed (counted as a parse fail).
- After evaluating a pulled model, free its VRAM and disk:
  `curl -X POST .../api/generate -d '{"model":"<m>","keep_alive":0}'` then
  `curl -X DELETE .../api/delete -d '{"model":"<m>"}'` (the compose file sets
  `OLLAMA_KEEP_ALIVE=-1`, so an unloaded model otherwise stays resident).

## 3. Judge and compare

```bash
python judge.py results.120b-medium.json \
  --endpoint http://10.10.10.22:9402/v1 --model gpt-oss:120b \
  --reasoning-effort low
python report.py results.*.judged.json
```

The judge grades adequacy/fluency per utterance and classifies what the source
*actually* was (foreign / english / mixed) — it never sees the langid tag, so
misdetections land in the `english` bucket where adequacy means "returned
unchanged". Honesty rules carry over from `scripts/cue_eval`:

- Freeze the eval set before iterating; re-baseline on the current export
  before comparing anything.
- The judge shares weights with the production model — its numbers compare
  runs on identical data, they are not ground-truth accuracy. Hand-read a
  stratified sample of every candidate's outputs (the judged JSON keeps
  source + rendering side by side), especially items the judge scores 0 and
  items where candidates disagree.
- One replay per candidate is enough at temperature 0 only for coarse gaps;
  re-run before claiming a close win.
