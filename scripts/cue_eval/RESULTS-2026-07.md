# Cue-model eval — July 2026 (running results log)

Side-by-side comparison of the production cue model against candidate
replacements, replayed on the frozen 12-conversation deployment dataset with
the shipped enrichment prompt (`scripts/cue_eval/replay.py`, temperature 0.0).
This file is the durable record of the testing session — every iteration's
numbers land here as they are produced, so the comparison never depends on any
one session's memory. Deployment side: DockerOps PR #99 (`compose/tenir-gpu.yaml`).

## Eval setup

- **Dataset**: 12 recorded conversations (676 non-empty turns, ~63 talk-min)
  exported from the production DB 2026-07-26 — entity-dense media audio, an
  engineering release discussion, direct questions, and two low-signal
  controls (movie audio `87e2acbe`, garbled-STT `ed1d330d`) where more cues
  would be a defect, not a win. Conversation ids in `replay.py`'s history:
  `f7f4b10c 363a125e 77f7414d d3b5acc9 8d0cac50 87e2acbe 9a84d209 48de05be
  36692e0a 1cf67366 bbead742 ed1d330d`.
- **Replay**: exact session gating (8-turn window, one attempt in flight
  modelled as 2.5 s, 1.5 s min interval, title + substance dedupe), ungrounded
  (no retrieval evidence).
- **Judging**: LLM judge scores each cue 0–2 on novelty / relevance /
  accuracy + duplicate flag. Iteration 1 onward judges with a **cross-family
  model** (Gemma 3 27B) to remove the judge-shares-generator-weights blind
  spot that capped the July prompt-calibration numbers; those earlier numbers
  (judged by Qwen itself) are marked (qj).
- **Latency**: mean wall-clock per model call, measured by the replay run —
  it bounds cue frequency directly (attempts are serialized one-in-flight).

## Candidates

| iteration | endpoint | model | why |
|---|---|---|---|
| baseline | 10.10.10.22:9402 | Qwen/Qwen3.6-27B-FP8 | current production cue model |
| 1 | 10.10.10.22:9404 | openai/gpt-oss-20b | frequency lever: 3.6B active params, ~3–5× faster |
| 1 | 10.10.10.22:9405 | RedHatAI/gemma-3-27b-it-FP8-dynamic | like-for-like size; factual QA + Spanish; cross-family judge |
| 2 | 10.10.10.22:9406 | openai/gpt-oss-120b | quality lever: ~4× knowledge at near-27B latency (staged, commented in compose) |

Iteration 2 requires swapping iteration 1 out (VRAM); its Qwen comparison
column reuses the iteration-1 Qwen run on the same frozen dataset.

## Prompt-calibration baselines (2026-07-26, Qwen-judged (qj))

From the enrichment-prompt calibration (Tenir PR #80), same dataset:

| run | cues | attempts | novelty | relevance | accuracy | restates | wrong | dups |
|---|---|---|---|---|---|---|---|---|
| production cues (old prompts + grounding) | 61 | — | 1.49 (qj) | 1.90 | 1.74 | 8 | 6 | 8 |
| old prompt, replayed ungrounded, t=0.2 | 8 | 566 | 1.88 (qj) | 2.00 | 2.00 | 0 | 0 | 0 |
| enrichment prompt, t=0.2 | 29 | 675 | 1.83 (qj) | 1.86 | 1.52 | 1 | 5 | 2 |
| enrichment prompt, t=0.0 (shipped) | 27 | 675 | 1.78 (qj) | 1.93 | 1.93 | 2 | 1 | 0 |

Known noise floor: re-running the same variant swings cue counts ~±15%
(sampling at t=0.2; window alignment at t=0.0), so treat single-run deltas
under ~4 cues as noise. Residual wrong-cue class to watch: confident
specifics at the knowledge frontier (niche chemistry, hardware pinouts,
Docker internals) — the main thing a stronger model should fix.

## Iteration 1 — Qwen3.6-27B vs gpt-oss-20b vs Gemma 3 27B (run 2026-07-26)

All replays on the frozen 12-conversation set (675 attempts each), shipped
enrichment prompt, t=0.0, ungrounded. Cross-family judging: the Qwen run was
judged by gpt-oss-20b; the gpt-oss runs by Qwen; Gemma by **both** (the two
judges agreed closely — wrong 40 vs 38, dups 87 vs 74 — which validates the
judge protocol). "perfect" = novelty 2 + relevance 2 + accuracy 2 + not a
duplicate. gpt-oss was run at two reasoning efforts since that dial sets its
speed/selectivity trade.

| model | cues | emit% | novelty | relevance | accuracy | perfect | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen3.6-27B-FP8 (current) | 22 | 3% | 1.59 | 1.95 | 1.86 | 14 | 3 | 1 | 1 | 0 | 1.30 |
| gpt-oss-20b, reasoning medium | 32 | 5% | 1.97 | 1.62 | 1.88 | 20 | 0 | 1 | 0 | 6 | 1.98 |
| gpt-oss-20b, reasoning low | 251 | 37% | 1.76 | 1.65 | 1.76 | 125 | 20 | 22 | 23 | 36 | 0.83 |
| gemma-3-27b-it-FP8 (qwen judge) | 448 | 66% | 1.57 | 1.58 | 1.77 | 179 | 55 | 40 | 87 | 95 | 1.66 |
| gemma-3-27b-it-FP8 (gpt-oss judge) | 448 | 66% | 1.62 | 1.71 | 1.68 | — | 70 | 38 | 74 | 95 | 1.66 |

### Findings (with hand review of flagged cues)

- **The prompt's emission calibration is model-specific.** It was tuned on
  Qwen and holds there (3% emit, controls silent). Gemma ignores it almost
  completely (66% emit, 28 cues on the garbled-STT control, 87 rephrased
  duplicates) — disqualified as a generator without its own calibration
  round. gpt-oss respects it at medium reasoning and abandons it at low.
- **The dataset holds far more good cues than the current model surfaces.**
  gpt-oss-low found ~125 judged-perfect cues (9x Qwen's 14) — volume is
  limited by the emission bar, not by available material. But it paid with
  22 confidently-wrong cues (invented display specs, wrong Pi 5 core count,
  wrong drone prices) — the exact failure class the accuracy rules exist for,
  at ~9% of output. Not shippable as-is.
- **gpt-oss-20b at medium is the honest challenger**: 20 perfect cues vs
  Qwen's 14 at the same wrong-cue count (1 each). Its 6 "control" cues are
  mostly *legitimate on hand review* (accurate definitions — carapace, Dolly
  the sheep, Red Skull — during a movie the household was watching; the judge
  docked relevance, a listener might not). Two real downsides: judged
  relevance 1.62 (it enriches tangentially more often) and 1.98 s/call vs
  Qwen's 1.30 s — the "frequency lever" thesis FAILS at medium effort because
  the reasoning tokens eat the speed advantage; the lever only exists at low
  effort, where accuracy collapses.
- **Judge-harshness caveat**: the gpt-oss judge marked Qwen's "32+32=64" cue
  novelty-0 ("restates the answer") — but the transcript *asked* the
  question aloud; answering is the product working. A couple of per-model
  points in the novelty/restate columns are judge artifacts of this shape.

### Iteration-1 verdict

Qwen stays ahead on precision-per-cue; gpt-oss-20b-medium edges it on good-cue
yield at equal accuracy but is no faster and noisier on relevance; neither
candidate delivers "more volume at maintained accuracy" cleanly. The
hypothesis that remains untested is that a *stronger* model can hold Qwen-like
discipline at a higher emit rate — exactly the iteration-2 gpt-oss-120b test
(~4x knowledge, ~5B active params). **Proceed to iteration 2.**

## Iteration 2 — gpt-oss-120b

_Pending iteration-1 results and the compose swap (uncomment eval-c,
comment out eval-a/b and tenir-vllm-cue)._

| model | cues | attempts | emit% | novelty | relevance | accuracy | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-oss-120b | | | | | | | | | | | |

Notes:

## Decision

_To be written when testing concludes: which model takes the `qwen3-llm`
alias (or keeps it), and why._
