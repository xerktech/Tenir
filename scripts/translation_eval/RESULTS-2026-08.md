# Translation model evaluation — 2026-08 (XERK-180)

Question: would live translation benefit from a **dedicated translation model**
instead of riding the cue model (`gpt-oss:120b` behind the shared LiteLLM alias,
which injects `reasoning_effort: medium` for all traffic)? Criteria: translation
speed and accuracy.

**Answer: no dedicated model — every candidate we tried loses accuracy or
robustness. The win is a dedicated *route*: the same `gpt-oss:120b` with
`reasoning_effort: low` for translation traffic is ~2× faster per call with
accuracy within noise of production, at zero extra VRAM and no new model to
operate.**

## Method

Harness in this directory (see README.md); everything replays the shipped
`OpenAITranslator` payload/parser from the installed `api` package against the
prod GPU host's Ollama (10.10.10.22:9402), same prompt, same
`response_format: json_object`, same fail-closed parsing.

- **Eval set**: all 594 utterances the production trigger would translate,
  drawn from the full deployment export (5,438 segments, 43 conversations with
  eligible turns): effective language (stored `lang` else `detect_lang`)
  non-English opens/extends a run; undetected segments inside a run inherit it
  with `source_lang=None`. Tagged mix: 192 es, 81 pt, 81 it, 35 fr, 1 de,
  204 run-inherited.
- **Accuracy sweep**: full set, `--workers 4` (= `OLLAMA_NUM_PARALLEL`).
- **Latency bench**: fixed 40-item stratified subset, `--workers 1` (production
  holds one translation in flight per session), model warmed first.
- **Judging**: `gpt-oss:120b` at reasoning low grades adequacy/fluency 0–2 and
  classifies what the source *actually* was (foreign/mixed vs english). Judge
  shares the baseline's weights — numbers are comparative only; ~90
  utterances hand-reviewed across models (all parse failures, all
  non-English renderings, 26 side-by-side samples, and every
  medium-vs-low disagreement).
- Single replay per candidate at temperature 0; each call is independent (no
  avoid-list path dependence as in cue evals), so run-to-run drift is far
  smaller than the gaps reported — except the medium-vs-low gap, which is
  analyzed disagreement-by-disagreement below.

### Candidates

| candidate | why |
|---|---|
| gpt-oss:120b, reasoning medium | production today (gateway-injected medium) |
| gpt-oss:120b, reasoning low | same weights, cheaper route config |
| gpt-oss:20b, reasoning low | same family, 5× smaller |
| gemma3:12b | strong small multilingual instruct |
| gemma3:4b | same, smallest useful size |
| aya-expanse:8b | multilingual-specialist instruct (23 languages) |

Dedicated seq2seq MT models (NLLB, MADLAD) were ruled out without a run:
Ollama/llama.cpp doesn't serve encoder-decoder MT models, so they'd need a new
serving stack, and they can't follow the "already English → return unchanged"
rule that (see below) turns out to carry most of the production risk.

## Results

### Latency (workers 1, warm, 40-item subset)

| candidate | mean ms | p50 | p90 | max |
|---|---|---|---|---|
| 120b medium (prod) | 1606 | 1206 | 3072 | 5612 |
| **120b low** | **946** | **818** | **1358** | **4243** |
| 20b low | 678 | 589 | 1000 | 1493 |
| gemma3:12b | 551 | 518 | 795 | 1043 |
| gemma3:4b | 468 | 446 | 615 | 840 |
| aya-expanse:8b | 1523* | 379 | 813 | 44119* |

\* aya's mean/max include one mid-bench model reload (the card can't keep every
candidate resident); its warm p50/p90 are the comparable numbers.

At production concurrency (workers 4, full 594 items) the same ordering holds:
medium mean 3.79 s, low 2.05 s, 20b 1.55 s, gemma3:12b 1.41 s, gemma3:4b 0.96 s,
aya 0.74 s.

### Accuracy (judged; foreign/mixed vs actually-English source)

| candidate | translated | foreign n | adequacy | fluency | adeq=0 | english n | adequacy |
|---|---|---|---|---|---|---|---|
| 120b medium (prod) | 593/594 | 236 | **1.44** | 1.72 | 51 | 357 | **1.97** |
| **120b low** | **594/594** | 236 | 1.36 | 1.71 | 58 | 358 | **1.97** |
| 20b low | 583/594 | 232 | 1.30 | 1.64 | 64 | 351 | 1.94 |
| gemma3:12b | 594/594 | 237 | 1.22 | 1.65 | 72 | 357 | 1.64 |
| gemma3:4b | 583/594 | 229 | 1.10 | 1.82 | 77 | 354 | 1.82 |
| aya-expanse:8b | 594/594 | 237 | 1.23 | 1.76 | 70 | 357 | 1.54 |

The `english` column is not a corner case: **the judge classified ~60% of
everything production translates as already-English source** — langid
misdetection (short fillers and plain English turns tagged pt/it/fr) is the
dominant reason a translation fires. Adequacy there means "left it alone", the
prompt's explicit rule.

### Failure classes (hand review)

- **Reverse translation** (product-breaking): given an English utterance
  mistagged with a source language, gemma3:12b and aya-expanse:8b repeatedly
  translated *into* the tagged language — an English turn tagged `it` came back
  in Italian, one tagged `fr` in French; aya even rendered a Spanish utterance
  tagged `fr` into French instead of English. gpt-oss (120b and 20b, both
  efforts) never did this in 594 items: it obeys the already-English rule even
  when the source clause lies. This is why the small multilingual models' 
  english-bucket adequacy craters (1.54–1.64 vs 1.97).
- **Degeneration** (gemma3:4b, 11/594 hard failures): emits the JSON object,
  then rambles — smart-quote corruption inside the JSON string breaks parsing;
  fail-closed, so production would show no translation. Also the most prone to
  inventing content on garbled input.
- **Empty responses** (20b low, 11/594): returns no content at all, mostly on
  trivial short English turns; harmless (fail-closed) but a reliability tax.
- **Literalism/meaning shifts on garbled STT** (all small models): the hardest
  slice is ambient *song lyrics* the STT half-hears; 120b renders the plausible
  intent, the small models go word-by-word or flip meaning (negation dropped,
  wrong subject).

### 120b medium vs low

Judged foreign adequacy 1.44 vs 1.36. Item-level: of 236 foreign items, low is
worse on 32, better on 16, and 4 of those disagreements are the judge scoring
*identical output text* differently — so the real gap is a net ~7% of foreign
items, concentrated in the garbled-lyrics slice, where low goes more literal
and occasionally inverts a negation. On conversational speech (the product's
actual purpose) the two are indistinguishable in the hand review, and both are
perfect-or-near on English passthrough. For that, low buys: p50 1.21 s → 0.82 s
warm-serialized, mean 3.79 s → 2.05 s at prod concurrency, p90 7.6 s → 3.6 s —
medium's contended p90 blows the "translations in 1–2 s" budget
(`api/src/api/metrics.py`); low sits inside it.

## Recommendation

1. **Don't adopt a dedicated translation model.** Every smaller model is a net
   accuracy loss, and the two multilingual specialists fail exactly where
   production needs robustness (misdetected/mixed-language input). The speed
   they'd buy (~0.3–0.7 s p50 over 120b-low) is small against the ~3 s mean
   turn-finalization wait that precedes every call
   (`api/src/api/config.py`, stt window comment).
2. **Split the route, not the model**: add a LiteLLM alias (e.g.
   `gpt-oss:120b-translate`) pointing at the same Ollama backend with
   `reasoning_effort: low`, add an `API_TRANSLATION_MODEL` setting consumed by
   `make_translator()` (it already parameterizes `model`; today it hardcodes
   `settings.llm_model`), and point translation at the new alias. ~10 lines
   across `litellm/config.yaml`, `api/src/api/config.py`,
   `api/src/api/translate/__init__.py`; cues are untouched. Keep medium if the
   lyric-slice fidelity is ever judged worth 2× latency — the alias makes the
   dial per-feature either way.
3. **Follow-ups this eval surfaced** (separate tickets): (a) langid
   misdetection drives ~60% of translation calls and all of the
   reverse-translation risk — raising its confidence threshold for short
   utterances would cut most spurious calls; (b) end-to-end latency is
   dominated by turn finalization (~3.0 s mean), not the model — any future
   speed work should start there; (c) the translator opens a fresh HTTP
   connection per call (`httpx.post`) — a pooled client is a free few-hundred-ms
   p90 saving at prod concurrency.
