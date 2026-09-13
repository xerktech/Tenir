# Translation eval — September 2026 (Qwen3-30B-A3B FP8 candidate spike)

Companion to `../cue_eval/RESULTS-2026-09.md`: the same Qwen3-30B-A3B FP8 spike,
measured on the live-translation path. **Result: A3B is faster but regresses
adequacy — most visibly on misdetected-English passthrough; recommend against
cutover.** The XERK-180 baseline round is `RESULTS-2026-08.md`.

## Eval setup

- **Dataset**: the full frozen `eval_set.json` (1,271 items) from `select_data.py`,
  re-baselined this session.
- **Replay**: `replay.py` with the **shipped `OpenAITranslator` payload** (thinking
  off — `translation_disable_thinking=true`), `--workers 4`, t=0.
- **Judge**: **fixed = qwen3.8-27b** (`judge.py`), same judge for every row; grades
  adequacy/fluency and classifies what the source actually is (english / foreign /
  mixed) without seeing the langid tag. ~1,140/1,271 items returned a parseable
  verdict per row (consistent across rows, so cross-row deltas hold). Spot-read by
  hand.
- **Serving**: as in the cue results — SGLang, FP8 weights, `fp8_e4m3` KV, single
  RTX PRO 6000; Thinking-2507 is **not** a translation candidate (no no-think mode →
  latency-disqualified for the caption path).

## Results (full eval set, thinking off, t=0)

| model | adequacy | fluency | english ad | foreign ad | eng<2 items | lat mean | lat p90 |
|---|---|---|---|---|---|---|---|
| **qwen3.8-27b (baseline)** | **1.910** | 1.862 | 1.97 | 1.76 | 24 | 200ms | 316ms |
| Qwen3-30B-A3B-FP8 (hybrid) | 1.802 | 1.805 | 1.84 | 1.70 | 78 | 217ms | 346ms |
| Qwen3-30B-A3B-Instruct-2507-FP8 | 1.783 | 1.832 | 1.81 | 1.70 | 97 | **177ms** | 277ms |

All rows: 1271/1271 attempted, 0 request errors, 0 JSON-envelope parse fails — the
A3B models emit the shipped JSON envelope reliably.

## Findings

- **The adequacy gap is smaller than on cues but still a regression** (~0.11–0.13):
  1.91 → 1.78–1.80. Foreign-source adequacy drops from 1.76 to 1.70.
- **The sharpest, most product-relevant loss is English passthrough.** Production
  translates misdetected-English utterances too, and is graded on returning them
  unchanged. The A3B models mangle these 3–4x more often than the baseline (78 / 97
  english items scored <2 vs 24): they rewrite fragments and substitute words
  (e.g. the filler "Um" rendered as "One"). For a mostly-English household this is
  the failure a user would notice first.
- **Instruct-2507 is the closest and fastest candidate** (ADQ 1.783, fluency 1.832,
  177ms — faster than the baseline's 200ms), but it is not at parity and its
  English-passthrough is the weakest of the three.

## Decision

**Do not replace Qwen3.8-27B for translations with Qwen3-30B-A3B.** No config change
shipped. If translation latency/throughput ever becomes the binding constraint,
Instruct-2507-FP8 is the candidate to revisit (fastest, closest adequacy), but the
adequacy and English-passthrough regressions make it a deliberate speed-for-accuracy
trade, not a free swap — and it must be re-baselined on a current export first.
