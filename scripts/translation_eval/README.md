# Translation eval harness

Measures the live-translation path (XERK-160) the same way `scripts/cue_eval/`
measures cues: replay real deployment utterances through the **shipped**
`OpenAITranslator` payload/parser from the installed `api` package, then
LLM-judge the outputs and compare runs. Built for XERK-180 (dedicated
translation model investigation); results narrative in
`RESULTS-2026-08.md`. The September 2026 Qwen3-30B-A3B (FP8) candidate spike is in
`RESULTS-2026-09.md`. The October 2026 dedicated-model sweep on public data (Hy-MT2,
MiLMMT-46, TranslateGemma) is in `RESULTS-2026-10.md`, harness in `public/` (§4).

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

# production setup (Qwen3.8-27B on SGLang; the shipped payload already sets
# chat_template_kwargs.enable_thinking=false)
python replay.py eval_set.json --endpoint http://localhost:4000/v1 --api-key "$LITELLM_KEY" \
  --model qwen3.8-27b-dflash --out results.qwen3.8-27b.json
```

- `--workers 4` (default) keeps full sweeps under ~10 minutes on the shared
  SGLang server; 4-6 is a good cap. For latency numbers run `--workers 1`
  (production holds one translation in flight per session) — a concurrent
  run's `call_ms` includes queueing.
- Candidate models must clear the same bar as production: same prompt, same
  `response_format: json_object`, same `_parse`. A model that can't reliably
  emit the JSON envelope fails closed (counted as a parse fail).
- The pre-cutover candidate sweep (gpt-oss:120b via Ollama on
  `10.10.10.22:9402`, plus smaller Ollama candidates like gemma3:12b) used
  the `--reasoning-effort` flag and Ollama's `/api/pull` +
  `keep_alive=0` cleanup workflow. That dial was gpt-oss-specific; for
  Qwen the equivalent lever is `chat_template_kwargs.enable_thinking`,
  which the shipped payload already carries.

## 3. Judge and compare

```bash
python judge.py results.qwen3.8-27b.json \
  --endpoint http://localhost:4000/v1 --api-key "$LITELLM_KEY" --model qwen3.8-27b-dflash
python report.py results.*.judged.json
```

The judge already sets `enable_thinking=false` for Qwen; `--reasoning-effort`
remains available for gpt-oss-era judges only.

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

## 4. Public-data sweep through the live app (`public/`)

No private transcripts: FLEURS es_419 audio + references, OPUS-100 es→en, English
passthrough. Runs in one pod on talos04 that holds the RTX PRO 6000 (vLLM) next to the
production Tenir image (the app under test), with real STT from `tenir-stt` (ArgoCD `ai/tenir/stt.yaml`; `tenir-parakeet` before XERK-1384).

```bash
cd scripts/translation_eval/public
export EVAL_PASSWORD=$(openssl rand -hex 12) EVAL_AUTH_SECRET=$(openssl rand -hex 32) \
  TENIR_IMAGE=$(kubectl -n ai get deploy tenir -o jsonpath='{.spec.template.spec.containers[0].image}')
envsubst < mt-eval.yaml | kubectl apply -f -          # claim + pod (whole Blackwell card)
kubectl -n ai exec mt-eval -c vllm -- mkdir -p /work
for f in *.py *.sh ../../../api/src/api/translate/openai.py; do
  kubectl -n ai cp "$f" mt-eval:/work/"$(basename "$f" | sed 's/^openai.py$/openai_translator.py/')" -c vllm
done
echo "$EVAL_PASSWORD" | kubectl -n ai exec -i mt-eval -c vllm -- tee /work/eval_pw >/dev/null
# then, inside the pod, detached (kubectl exec streams drop on long commands):
#   setsid nohup bash setup.sh > setup.log 2>&1 < /dev/null &
#   STT capture (the frozen fleurs_asr set; waits 20 s per clip):
#     TENIR_USERNAME=evaluser TENIR_PASSWORD=$(cat eval_pw) \
#     python3 ws_driver.py data/clips.json --out results/stt_capture.json --concurrency 4 --no-wait \
#     && echo STT_OK > stt.log
#   setsid nohup bash run_all.sh > run_all.log 2>&1 < /dev/null &
#   /work/venv/bin/python score.py results/*/text_*.json results/*/e2e_*.json --out results/scores.json
#   /work/venv/bin/python boot.py      # paired bootstrap vs Qwen, per set (--cpu off-GPU)
kubectl -n ai delete pod mt-eval && kubectl -n ai delete resourceclaim mt-eval-rtx6000
```

- `shim.py` is the prompt adapter (shipped prompt forwarded vs each model's native one);
  `run_model.sh` serves one model with a fixed 2 GiB KV cache and records VRAM.
- Unit tests: `python -m pytest scripts/translation_eval/public/tests/` (needs `api[dev]`).
- Pitfalls that cost real time (vLLM image, Hy-MT2, TranslateGemma, kubectl):
  `.claude/rules/translation-eval.md`.
