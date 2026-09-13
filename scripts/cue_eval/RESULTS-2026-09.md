# Cue-model eval — September 2026 (Qwen3-30B-A3B FP8 candidate spike)

Investigated replacing **Qwen3.8-27B** (the shipped cue + translation model) with
**Qwen3-30B-A3B** at 8-bit (FP8) — a 30B-total / 3B-active MoE, attractive for its
speed. Tested the three public A3B FP8 checkpoints against the 27B baseline on the
same frozen set. **Result: no A3B variant reaches 27B cue accuracy; recommend
against cutover.** The August round (27B retune) is `RESULTS-2026-08.md`.

## Eval setup

- **Dataset**: the same frozen 6-conversation set / 795 gated attempts as
  `RESULTS-2026-08.md` (re-baselined this session — never compare against an old
  export).
- **Replay**: `cue_replay_prompt.py --variant v5` (the shipped emission-first
  frame), t=0.0, ungrounded, exact session gating. Thinking on where the model
  supports it (see per-row), `--max-tokens 2048`.
- **Judge**: **fixed = qwen3.8-27b** for every row (`judge.py`, thinking off).
  Candidates do not share the judge's weights — a cleaner judge than self-grading,
  but absolute scores are still comparative, not ground truth. All acc=0 cues were
  hand-read to confirm the regression is real (see Findings).
- **Serving**: SGLang (same Blackwell dev image as prod), FP8 weights (auto),
  `--kv-cache-dtype fp8_e4m3`, single RTX PRO 6000, `--mem-fraction-static 0.75`,
  `--context-length 32768`. **No DFlash** — no A3B speculative-draft exists, so the
  A3B thinking rows run without the spec-decode the 27B baseline enjoys.

## Results (frozen 6-conv, 795 attempts, v5, t=0)

| model | thinking | cues | emit% | accuracy | wrong (acc0) | partial (acc1) | novelty | mean call |
|---|---|---|---|---|---|---|---|---|
| **qwen3.8-27b (baseline, +DFlash)** | on | 144 | 18.1% | **1.917** | 4 (2.8%) | 4 | 1.646 | 7.55s |
| Qwen3-30B-A3B-Thinking-2507-FP8 | on | 83 | 10.4% | 1.759 | 9 (10.8%) | 2 | 1.735 | 12.43s |
| Qwen3-30B-A3B-FP8 (hybrid) | on | 114 | 14.3% | 1.544 | 19 (16.7%) | 14 | 1.561 | 4.98s |
| Qwen3-30B-A3B-Instruct-2507-FP8 | off | 163 | 20.5% | 1.417 | 30 (18.4%) | 35 | 1.239 | 0.35s |

## Findings

- **A3B is dramatically faster but far less factually accurate on cues.**
  Instruct-2507 (no-think) emits the most cues (163) at ~20x the speed
  (0.35s vs 7.55s), but **18% are wrong** vs 2.8% for the baseline. Wrong-cue rate
  is the metric that matters most for this product — a confident wrong cue is the
  worst failure — and every A3B variant is 4–7x worse than the 27B.
- **The failures are genuine confabulations, not judge artifacts** (hand-verified).
  The 3B active-parameter MoE recognizes the topic but invents specifics: fabricated
  products ("Gorilla Glass Victus 3", a non-existent "NyxOS" from misheard "NixOS",
  "LP CAM / Low-Power Compound Memory" from "LPDDR5X"), specs contradicting the
  transcript ("Apple M3" when the speaker said an Intel Core Ultra), and invented
  "corrections" of numbers the speaker stated. These are exactly the failure classes
  the 27B + thinking + worked-examples frame controls.
- **Thinking narrows the gap but does not close it, and costs volume + latency.**
  Thinking-2507 is the most accurate A3B (1.759, 10.8% wrong) but under-emits (83
  cues, below the 144 baseline) and is the slowest tested (12.4s — A3B thinking with
  no DFlash). It reasons itself out of cues; adding an emissive tail might restore
  volume but the ~11% wrong-rate ceiling is a capability limit, not a framing one.
- **Quantization is not the cause.** FP8 (vs the 27B's NVFP4) is the *higher*
  precision here; the gap is the 3B active-parameter budget, which prompt/quant tuning
  will not overcome to reach 2.8% wrong.

## Latency (from multi-threaded replay, mean call)

27B+DFlash 7.55s · A3B-Instruct(no-think) **0.35s** · A3B-hybrid(think) 4.98s ·
A3B-Thinking-2507 12.43s. A3B's speed win is real and large, but it buys throughput
at an accuracy cost the cue path cannot absorb.

## Decision

**Do not replace Qwen3.8-27B for cues with Qwen3-30B-A3B (any FP8 variant).** No
config change shipped; the shipped cue model/prompt are unchanged. If cue throughput
ever becomes the binding constraint, the only A3B worth revisiting is Thinking-2507
paired with a more-emissive frame — and even then it trades ~4x the wrong-cue rate
for speed, so it needs an explicit product decision, not a drop-in swap. Translation
findings: `../translation_eval/RESULTS-2026-09.md`.
