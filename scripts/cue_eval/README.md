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
  --endpoint http://maxai.xerktech.com:9402/v1 --model qwen3.8-27b \
  --out results.json [--conversations id1,id2,...]
```

Replays each conversation with the exact session gating: an 8-turn rolling
context window, one attempt in flight at a time (modelled as 2.5 s of
transcript time), the 1.5 s min interval between emitted cues, and all three
dedupe backstops (normalized title + substance fingerprint + title-subject
containment). Ungrounded by default.

Options that make the replay match production more closely:

- `--grounded` adds evidence from Tenir's own `LiveEvidenceRetriever`
  (`--wikipedia`, `--kiwix`, `--searxng`; the news FTS tier lives in the
  deployment DB and is skipped). Evidence is cached per transcript window in
  `--evidence-cache`, so every model compared on the same cache sees identical
  evidence — reuse one cache file across all runs of a comparison. Wikipedia
  rate-limits bursts (HTTP 429), and a rate-limited window caches as empty:
  warm the cache with `--workers 1`, then rerun with `--refetch-empty` until the
  reported empty count stops falling, and only then compare models on it.
- `--realtime` spaces attempts by the measured call (+ verify) latency instead
  of a fixed 2.5 s, as a live session's one-in-flight rule does. Without it a
  slow model is credited with attempts it would never get. Use `--workers 1` so
  latency isn't inflated by queueing (a run then takes about as long as the
  conversations themselves).
- `--verify off|low|medium` runs the self-check pass (`VERIFY_SYSTEM`) on each
  emitted cue and drops the ones it calls unsafe. Verifier failures (HTTP
  errors, unparseable verdicts) also drop the cue but are counted separately
  as `verify_errors` — a non-zero count means the verifier, not the model, is
  shaping the result.
- `--max-tokens N` and `--extra JSON` override the shipped payload, e.g.
  `--extra '{"custom_params": {"thinking_budget": 512}}'` (SGLang thinking cap)
  or `'{"chat_template_kwargs": {"reasoning_effort": "low"}}'` (Qwen3.8 effort;
  the template default is `xhigh`).

For single-request latency (the number that matters live), use
`latency_probe.py` on an otherwise idle server rather than replay call times.

## 3. Judge and report

```bash
python scripts/cue_eval/judge.py results.json segments.json \
  --endpoint http://maxai.xerktech.com:9402/v1 --model qwen3.8-27b
python scripts/cue_eval/report.py results.judged.json
```

The judge grades each cue 0-2 on **novelty** (does the body add information not
in the transcript — 0 is a pure restatement), **relevance**, and **accuracy**,
and flags **duplicates** of earlier cues. The judge shares the generator's
weights, so treat absolute accuracy numbers as comparative, not ground truth —
spot-check the flagged cues by hand.

Model judges miss their own failure class (gpt-oss-120b judged 3 of its own 273
cues wrong where a blind review found 37). For a model comparison, pool the
runs with `blind_judge.py pack`, hand each pack plus the generated `RUBRIC.md`
to an independent cross-family reviewer (e.g. one Claude subagent per pack),
and score with `blind_judge.py report`. Include an anchor run in every batch:
batch-to-batch drift is a few points.

## Baseline numbers (2026-07, 12 recorded conversations)

These numbers are gpt-oss:120b-era; the production model was replaced by
Qwen3.8-27B on SGLang in Aug 2026, so re-baseline before comparing anything
new (rule: never compare against an old export).

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
The August 2026 re-baseline after the Qwen3.8-27B cutover is in
`RESULTS-2026-08.md`. The September 2026 Qwen3-30B-A3B (FP8) candidate spike — which
found no A3B variant reaches 27B cue accuracy — is in `RESULTS-2026-09.md`. The
late-September model/VRAM/latency shoot-out (Qwen3.8-27B vs gpt-oss-120b, Qwen3-30B-A3B,
Granite 4.2, Nemotron 3.5 Lightning; thinking caps; the self-check; sizing for a
24–48 GB card) is in `RESULTS-2026-09-shootout.md`.

## Prompt-variant replays (Aug 2026, Qwen3.8-27B retune)

`cue_replay_prompt.py` extends the replay with emission-posture tails and
full-frame prompt variants (v1–v7) — the harness behind the Qwen3.8-27B
retune in `RESULTS-2026-08.md`:

```bash
python scripts/cue_eval/cue_replay_prompt.py segments.json \
  --endpoint http://maxai.xerktech.com:9402/v1 --model qwen3.8-27b \
  --out results.json --variant v5 --thinking on --max-tokens 2048
```

Full-prompt variants (v5–v7) replace only the frame — the text before the
shipped worked examples — and keep the examples, avoid list, and final reply
line, so `--variant v5` reproduces the shipped ungrounded prompt byte for byte.
If the shipped prompt loses its `Examples of the standard:` anchor the harness
raises rather than silently dropping those blocks (which it did before
2026-09-23 — see the caveat in `RESULTS-2026-09.md`). The tail variants v1–v4
anchor on the July frame's closing sentence (`_SHIPPED_TAIL`), which no longer
ships — re-baseline against the current shipped prompt before comparing those
tails.
