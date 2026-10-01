# Translation eval — October 2026 (dedicated translation models)

Question: can a small dedicated translation model replace Qwen3.8-27B for Spanish → English
live translation without losing accuracy? Candidates: Tencent **Hy-MT2** (1.8B, 7B),
Xiaomi **MiLMMT-46 v1.0** (1B, 4B, 12B), Google **TranslateGemma** (4B, 12B).

**Answer: yes, at parity — not better.** On Spanish speech (FLEURS, gold text and the app's
own STT), no candidate is significantly better than Qwen; five are statistically
indistinguishable from it. The smallest of those are **MiLMMT-46-4B** and TranslateGemma-4B
(both 10.9 GB). **MiLMMT-46-4B is the pick**: highest of the two on speech (+0.14, CI spans
0), significantly better on conversational subtitles (OPUS-100, +1.18), far better English
passthrough, ~100 ms per turn vs 475 ms. It needs a native-prompt mode in the translator
(it cannot follow the shipped JSON prompt). **Hy-MT2-7B is the zero-code option** — it
survives the shipped prompt and is at parity (−0.02) — at 18 GB and ~260 ms. The 1B/1.8B
models are significantly *worse* on app STT.

## Setup

- **Hardware**: RTX PRO 6000 Blackwell (97,887 MiB) on talos04, whole card via a DRA claim.
- **Serving**: vLLM 0.30.0 (`vllm/vllm-openai:v0.30.0-cu129`, torch pinned to 2.13.0+cu129),
  BF16, `--max-model-len 4096 --max-num-seqs 8`, **fixed 2 GiB KV cache for every model**,
  CUDA graphs on. Gemma3-based models run `--language-model-only` (text only).
- **Baseline**: Qwen3.8-27B FP8 on the same card and stack (2 seqs, 8 GiB KV — a hybrid
  model). Production ran NVFP4 + DFlash on SGLang (41.5 GB right-sized, 132 ms
  translations, `../cue_eval/RESULTS-2026-09-shootout.md`); it is disabled this round.
- **The app**: the production Tenir image in the same pod (memory persistence, cues/music
  off), real STT via the production `tenir-parakeet` service, translation pointed at a
  prompt-adapter shim in front of vLLM. Everything goes through the shipped
  `OpenAITranslator` payload and `_parse`.
- **Data** (public only; frozen and seeded before any model ran):
  - `fleurs_gold`: 200 FLEURS es_419 test sentences, gold Spanish text → English refs.
  - `fleurs_asr`: the same clips' audio through the app; the app's own Parakeet finals and
    language tags captured once and replayed identically to every model with the
    production translate trigger (349 finals from 177 clips). The capture came back with
    no finals for 23 clips that every e2e run captured, and covers fewer reference words
    than the e2e runs (cause not isolated — an old-driver rerun outside the pod got all 23;
    the harness now waits a fixed 20 s). Identical for every model, so comparisons hold.
  - `fleurs_e2e`: each model live end to end — 200 clips over the WebSocket, the app
    translates, `translation` messages scored (STT re-runs per model; 0/200 clips lost).
  - `opus_conv`: 300 short conversational OPUS-100 es→en lines (subtitle-style; refs are
    noisy). Tatoeba needs HF auth from the cluster, so it was replaced.
  - `passthrough`: 150 English FLEURS sentences sent as an inherited (untagged) turn.
- **Metrics**: COMET-22 (`Unbabel/wmt22-comet-da`), chrF++, BLEU; latency = one request in
  flight, warmed; e2e latency = `caption.final` → `translation` as the client receives it.
- **Prompts**: each model ran the shipped prompt (fixed 411-item stratified subset) and its
  documented native prompt (full set); the better one drove the e2e run.
- **Weights**: TranslateGemma via the ungated `Infomaniak-AI/vllm-translategemma-*` repack
  (no HF token for Google's gated repo; byte sizes match Google's shards, hashes are
  unverifiable while gated).

## Accuracy vs Qwen (the decision table)

Paired bootstrap on segment-level COMET-22 over identical units (Qwen's subset: 60 gold,
100 asr clips, 100 opus), Δ = candidate − Qwen, 95% CI (`public/boot.py`). **Bold** = CI
excludes 0. Seven candidates and five groups: expect the odd marginal CI by chance.

| model | fleurs_gold | fleurs_asr | **speech (gold+asr)** | opus_conv | all |
|---|---|---|---|---|---|
| MiLMMT-46-1B | −0.22 [−0.71, +0.25] | **−0.74** [−1.55, −0.02] | **−0.55** [−1.08, −0.07] | +0.57 [−0.12, +1.23] | −0.12 [−0.53, +0.30] |
| Hy-MT2-1.8B | **−0.68** [−1.18, −0.16] | **−0.81** [−1.69, −0.02] | **−0.76** [−1.33, −0.21] | −0.25 [−0.98, +0.50] | **−0.57** |
| Hy-MT2-1.8B, shipped prompt | **−0.83** | **−0.84** | **−0.84** [−1.40, −0.31] | **−1.14** | **−0.96** |
| **MiLMMT-46-4B** | −0.00 [−0.42, +0.46] | +0.22 [−0.46, +0.88] | +0.14 [−0.28, +0.57] | **+1.18** [+0.51, +1.93] | **+0.54** [+0.16, +0.91] |
| TranslateGemma-4B | −0.25 [−0.77, +0.23] | −0.13 [−0.81, +0.53] | −0.18 [−0.65, +0.27] | −0.91 [−2.00, +0.10] | −0.46 [−0.94, +0.01] |
| Hy-MT2-7B | −0.10 [−0.66, +0.47] | +0.12 [−0.45, +0.67] | +0.04 [−0.36, +0.44] | +0.60 [−0.16, +1.29] | +0.26 [−0.11, +0.63] |
| Hy-MT2-7B, shipped prompt | −0.01 [−0.53, +0.53] | −0.02 [−0.84, +0.73] | −0.02 [−0.57, +0.52] | −0.08 [−1.20, +0.88] | −0.04 [−0.62, +0.47] |
| MiLMMT-46-12B | +0.08 [−0.31, +0.47] | +0.25 [−0.37, +0.83] | +0.19 [−0.21, +0.58] | **+0.83** [+0.16, +1.53] | **+0.44** |
| TranslateGemma-12B | +0.13 [−0.39, +0.63] | +0.51 [−0.14, +1.20] | +0.37 [−0.08, +0.83] | +0.15 [−0.71, +1.00] | +0.28 [−0.18, +0.70] |

The "all" column is dominated by `opus_conv`: MiLMMT-4B's pooled +0.54 is ~84% OPUS.
Speech (gold+asr) is the product-relevant column.

## Cost, speed and absolute scores

Totals are the card's `memory.used` after warm-up with the fixed 2 GiB KV cache; peak (a
500 ms trace over the whole run) is ≤ 6 MiB above it for every model. Absolute COMET is
each model's native prompt on the full sets (Qwen: the subset).

| model | VRAM total (weights) | gold | opus | asr | e2e | ms/turn asr (p90) | e2e ms (p90) | shipped prompt |
|---|---|---|---|---|---|---|---|---|
| MiLMMT-46-1B | 5,020 MiB (1.96 GiB) | 87.22 | 83.80 | 77.66 | 79.03 | 36 (55) | 44 (62) | ✗ 743/999 unparseable (rambles to the cap) |
| Hy-MT2-1.8B | 6,336 MiB (3.34 GiB) | 86.91 | 82.90 | 77.33 | 78.49 | 55 (86) | 64 (93) | ✓ 0 fails |
| **MiLMMT-46-4B** | **10,946 MiB** (7.82 GiB) | 87.60 | 84.52 | 78.25 | 79.74 | **98** (155) | **107** (159) | ✗ 357/411 unparseable (mostly `{}`) |
| TranslateGemma-4B | 10,946 MiB (7.82 GiB) | 87.08 | 82.08 | 78.09 | 78.92 | 103 (157) | 108 (165) | ✗ HTTP 400 (template) |
| Hy-MT2-7B | 18,212 MiB (13.98 GiB) | 87.47 | 83.85 | 78.27 | 79.74 | 172 (272) | 179 (270) | ✓ 0 fails, 257 ms |
| MiLMMT-46-12B | 26,132 MiB (22.62 GiB) | 87.73 | 84.74 | 78.46 | 80.00 | 278 (439) | 286 (431) | ✗ 88/411 unparseable |
| TranslateGemma-12B | 26,132 MiB (22.62 GiB) | 87.29 | 83.64 | 78.34 | 79.55 | 295 (458) | 298 (465) | ✗ HTTP 400 (template) |
| Qwen3.8-27B FP8 (baseline) | 39,728 MiB (27.61 GiB) | 87.38* | 83.51* | 77.59* | 79.14 | 452 (639)* | 475 (658) | ✓ (it is the prompt) |

\* subset. On a 3090 (where production runs it, ArgoCD `ai/tenir/milmmt.yaml`) MiLMMT-4B
uses 10,478 MiB and takes 187 ms mean / 295 ms p90 per turn.

English passthrough (150 English sentences sent as an inherited turn; the native shims
were told "Spanish", Qwen gets no source clause):

| model | returned unchanged | chrF vs input |
|---|---|---|
| Qwen3.8-27B (subset) | 74% | 98.2 |
| MiLMMT-4B / 12B / 1B | 57% / 57% / 49% | 96.5 / 96.7 / 96.4 |
| Hy-MT2-7B / 1.8B | 31% / 9% | 91.1 / 83.1 |
| TranslateGemma-12B / 4B | 5% / 3% | 76.9 / 75.8 |

This mostly measures a path production doesn't take: `detect_lang` tags 145/150 of these
sentences `en`, and the app never sends an `en` turn. It matters for the ~3% of English
turns langid can't call inside a run.

## Findings

- **Parity, not a win.** On speech every candidate from 4B up sits within ±0.4 COMET of
  Qwen, none significantly better. MiLMMT-4B/12B are significantly ahead on OPUS (Hy-MT2-7B
  +0.60, CI spans 0), whose refs are noisy — treat that as "at least as good on short
  conversational lines", not as proof.
- **Small models lose on speech.** MiLMMT-1B and Hy-MT2-1.8B are significantly worse on
  the app's STT output; their parity on clean gold text doesn't survive recognition errors.
- **Size buys little past 4B.** MiLMMT-4B → 12B: +0.05 on speech at 2.4× the VRAM and 2.8×
  the latency.
- **Latency collapses.** MiLMMT-4B is ~4.5× faster than the FP8 Qwen baseline on the same
  card (98 vs 452 ms). Deployed on a 3090 it measured 187 ms — slower than production
  Qwen's NVFP4+DFlash 132 ms on the RTX PRO 6000, but at ~11 GB instead of 41.5 GB.
- **Only Hy-MT2 survives the shipped prompt** (1.8B and 7B, 0 parse failures). MiLMMT-1B
  rambles to the 1,000-token cap (1.2 s/call); MiLMMT-4B mostly answers `{}` (12B: 40% `{}`);
  TranslateGemma's chat template rejects the request. Everything else needs an adapter
  (`public/shim.py` is a working reference; Tenir's is XERK-1354).
- **Source-language prompts can inherit langid errors, model-dependently.** 7/100 frozen
  clips carry a non-`es` tag on Spanish speech (fr/pt on short turns). On those clips
  MiLMMT loses vs Qwen (4B −0.2, 1B −0.9, 12B −1.3 COMET); TranslateGemma, also told the
  wrong language, gains (+0.6 / +1.0); Hy-MT2-7B, told none, gains +2.6. Seen e2e: "Hielo o
  polvo" tagged pt → MiLMMT "Ice the octopus" (*polvo* = octopus in pt). Langid's recall on
  Spanish is the bigger problem: XERK-1349.
- **Hy-MT2 on vLLM 0.30 needs a RoPE override to be fast.** vLLM serves
  `HunYuanDenseV1ForCausalLM` only through its Transformers backend, whose "dynamic" RoPE
  wrapper breaks CUDA-graph capture (eager: 333 ms/turn at 1.8B). `rope_type: default` with
  the equivalent static base is exact (eager stock vs eager override: 40/40
  token-identical); served with CUDA graphs it matches stock on 35/40 (kernel drift) at
  119 ms. See `.claude/rules/translation-eval.md`.

## Recommendation

Adopt **MiLMMT-46-4B** (smallest model at parity on speech, ahead on conversational lines,
~11 GB, ~100–190 ms), with a native completion-prompt mode in the translator that
translates inherited turns from the run's language and skips clearly-English ones
(XERK-1354; served by ArgoCD `ai/tenir/milmmt.yaml`, XERK-1355). If a zero-code swap is
worth 7 GB more VRAM and ~2× the latency, **Hy-MT2-7B** with the shipped prompt is at
parity. Don't use the 1B/1.8B models.

Not yet measured: FP8/INT8 quantization of the 4B, other source languages (MiLMMT covers
46; this round is Spanish only), and real household conversations (the private-export judge
round in `README.md` §1–3) — run that on the chosen model before relying on it.

## Limits

- FLEURS is read Wikipedia-style speech; OPUS refs are noisy. Conversational quality is
  approximated by `opus_conv`, not measured on household audio.
- Bootstrap CIs are per comparison; with 7 candidates × 5 groups, one marginal CI is
  expected by chance. The speech column's conclusion (no significant win, two clear
  losses) does not hinge on any single marginal interval.
- `fleurs_e2e` re-runs STT per model (untranslated turns vary 34–51), so it is noisier
  than the frozen `fleurs_asr` comparison; the frozen set itself under-covers (see Data).
- The harness's `app_trigger` mirrors the session's translate trigger but not the 3 s hold
  expiry or echo drops; neither affects the scores.
