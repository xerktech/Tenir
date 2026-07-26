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

## Iteration 1 — Qwen3.6-27B vs gpt-oss-20b vs Gemma 3 27B

_Pending deployment (DockerOps PR #99). To be filled in by the replay +
cross-family judge runs once :9404 and :9405 are healthy._

| model | cues | attempts | emit% | novelty | relevance | accuracy | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen3.6-27B-FP8 (current) | | | | | | | | | | | |
| gpt-oss-20b | | | | | | | | | | | |
| gemma-3-27b-it-FP8 | | | | | | | | | | | |

Notes:

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
