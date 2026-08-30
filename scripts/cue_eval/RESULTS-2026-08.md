# Cue-model eval — August 2026 (Qwen3.8-27B retune)

After gpt-oss-120b was taken down and **Qwen3.8-27B** (NVFP4 + DFlash
speculative decoding, SGLang on `maxai.xerktech.com:8890`) took its place
behind the same LiteLLM aliases (`qwen3.8-27b-dflash` for cues,
`qwen3.8-27b-dflash-translate` for translations), the shipped July prompt
replayed badly on the new model and needed a fresh calibration round. This
file is the durable record of that round; the July round is
`RESULTS-2026-07.md`.

## Eval setup

- **Dataset**: frozen 6-conversation set, 795 gated attempts (re-exported
  from the production DB after the cutover; the same sessions that drove the
  gpt-oss-120b baseline of ~150 cues at judged accuracy 1.99 on this set).
- **Replay**: exact session gating (8-turn window, one attempt in flight
  modelled as 2.5 s, 1.5 s min interval, 40-cue avoid list, 0.35 substance
  dedupe), ungrounded (no retrieval evidence), temperature 0.0, via
  `scripts/cue_eval/cue_replay_prompt.py` (variant logic mirrored from
  `replay.py`; full-prompt variants replace the whole system frame while
  keeping the shipped worked examples + final reply line).
- **Judging**: LLM judge scores each cue 0–2 on novelty / relevance /
  accuracy + duplicate flag (`judge.py`).
- **Latency**: mean wall-clock per model call from clean **single-request**
  probes. Multi-threaded replay numbers include queue contention across
  concurrent conversations and are not representative of a live single cue
  call.

## Prompt variants tested

All runs: Qwen3.8-27B (NVFP4, DFlash) via the `qwen3.8-27b-dflash` alias,
frozen 6-conversation set, 795 attempts, ungrounded, t=0.0.

| variant | frame | thinking | max_tokens | cues | emit% | accuracy | restates | wrong | dups |
|---|---|---|---|---|---|---|---|---|---|
| shipped (July enrichment) | old | off | 600 | 6 | 1% | — | — | — | — |
| think2048 | old | on | 2048 | 26 | 3% | 1.962 | 0 | 0 | 0 |
| v5 | emission-first short | off | 2048 | 64 | 8% | 1.81 | 0 | 3 | 0 |
| **v5 + thinking** | emission-first short | **on** | **2048** | **159** | **20%** | **1.969** | 0 | 0* | 1 |
| v6 | v5 + definite-statement rule | on | 2048 | 56 | 7% | 1.71 | — | 5 | — |
| v7 | v5 + 4 confabulation guardrails | on | 2048 | 60 | 8% | 1.78 | — | — | — |

\* One acc=0 cue was a degenerate `{"cue": true, "title": "...", "body":
"..."}` placeholder — a decline the model phrased as an acceptance. No real
wrong cue in the run; the parser now rejects placeholder-only title/body.

**Baseline to beat**: gpt-oss-120b ≈ 150 cues on the same frozen set at judged
accuracy 1.99. v5+thinking beats it on volume (159) and matches on accuracy
(1.969 vs 1.99, within the ±15-cue run-to-run noise floor).

## Prompt variants

- **v5** — full system-prompt replacement: a short emission-first frame
  ("surface a cue on most turns of a substantive conversation; fire when you
  can add ANY of five things…") with a five-bullet absolute-accuracy block.
  Drops the July frame's dense restraint prose (register matching, bare-name
  defaults, acronym-variant rules, candidate discipline) — those failure
  classes stay covered by the shipped worked examples, which v5 keeps verbatim.
- **v6** — v5 plus an extra "definite statement only, never a guess" bullet.
- **v7** — v5 plus four compact confabulation guardrails (garbled-name
  mapping, cross-domain acronym expansions, firsthand-detail contradiction,
  product-line vs specific model).

## Findings

- **The July frame was tuned on gpt-oss and under-emits on Qwen3.8-27B.**
  The dense restraint prose read as "stay quiet" to this model: 6 cues on the
  frozen set with thinking off, 26 with thinking on — a near-mute system.
- **A short emission-first frame (v5) fixes volume without hurting accuracy,
  but only with thinking on.** v5 think-off: 64 cues at accuracy 1.81 (3
  wrong). v5+thinking: 159 cues at 1.969 with effectively 0 real wrong cues.
  Thinking-on reasons into the budget and filters the same confabulation
  classes the July prose rules encoded.
- **The token budget is the enabling constraint.** At 600 tokens, thinking-on
  starves the JSON answer (`finish_reason: length`, empty `content`) — every
  cue silently dropped. 2048 lets the reasoning fit; the body is still
  clipped to 240 chars at parse, so the extra budget costs latency only when
  the reasoning actually uses it.
- **Adding rules back to v5 makes it worse.** v6 (56 cues, acc 1.71, 5 wrong)
  and v7 (60 cues, acc 1.78) both over-constrained the model — it treats each
  extra restraint as another reason to decline, and the guardrails did not
  reduce wrong cues (thinking already handles them). Keep the frame short;
  guard failure classes in the worked examples, not in prose.
- **Run-to-run variance is ±15 cues at temperature 0** on this model (DFlash
  speculative decoding is not bit-deterministic); single-run deltas under
  ~15 cues are noise.
- **Placeholder artifact**: one `{"title":"...","body":"..."}` acceptance
  across the whole run. Guarded in `_parse` (requires an alphanumeric in each
  field) and unit-tested.

## Latency (clean single-request probe, thinking on, 2048 budget)

| metric | value |
|---|---|
| mean | 5.87 s |
| p50 | 6.04 s |
| p90 | 9.71 s |
| max | 16.11 s |

Fits the 30 s call timeout (raised from 20 s) with headroom for concurrent
sessions; the multi-threaded replay's ~20 s mean was 3-way queue contention,
not the call itself.

## Decision

Ship v5 + thinking-on + 2048 budget:

- `api/src/api/cue/openai.py` — v5 emission-first frame (verified
  byte-identical to the evaluated no-evidence prompt), thinking sent
  explicitly in both directions (default on), `max_tokens` constructor
  parameter (default 2048), 30 s timeout default, parser rejects
  placeholder-only title/body.
- `api/src/api/cue/tuning.py` — `CUE_GUIDANCE` is now the frame's short
  time-varying-facts bullet; `CUE_GUIDANCE_GROUNDED` re-phrased for the v5
  frame (XERK-120 one-sided evidence generosity preserved).
- `api/src/api/config.py` — `cue_disable_thinking` default flipped to `false`,
  new `cue_max_tokens = 2048`, new `translation_disable_thinking = true`
  (separate from the cue flag — translations stay thinking-off for
  per-utterance latency on the caption path).
- `docker-compose.yml` — `API_CUE_MAX_TOKENS`, `API_CUE_DISABLE_THINKING`,
  `API_TRANSLATION_DISABLE_THINKING` env knobs.

## Full production-set replay (pending)

A full 134-conversation production-set replay of v5+thinking
(`cues_full_v5think.json`) was still running at the time of writing; its
judged summary will be appended here as a generalization check beyond the
frozen 6-conversation set.
