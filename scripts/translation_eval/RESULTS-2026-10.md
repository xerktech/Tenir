# Translation eval — October 2026 (dedicated translation models)

Question: can a small dedicated translation model replace Qwen3.8-27B for Spanish → English
live translation, at equal or better accuracy? Candidates: Tencent **Hy-MT2** (1.8B, 7B),
Xiaomi **MiLMMT-46 v1.0** (1B, 4B, 12B), Google **TranslateGemma** (4B, 12B).

**Answer: yes. MiLMMT-46-4B beats the Qwen baseline (+0.54 COMET, 95% CI excludes 0) in
10.9 GB at ~100 ms per turn; MiLMMT-46-1B is at parity (−0.12, CI spans 0) in 5.0 GB at
~40 ms.** Neither drops in without code: both need a native-prompt adapter in
`api/src/api/translate/openai.py` (they cannot follow the shipped JSON-envelope prompt).
Hy-MT2-1.8B is the only true drop-in (shipped prompt, 0 parse failures) but is measurably
less accurate (−0.96 with the shipped prompt).

## Setup

- **Hardware**: RTX PRO 6000 Blackwell (97,887 MiB) on talos04, whole card via a DRA claim.
- **Serving**: vLLM 0.30.0 (`vllm/vllm-openai:v0.30.0-cu129`, torch pinned to 2.13.0+cu129),
  BF16, `--max-model-len 4096 --max-num-seqs 8`, **fixed 2 GiB KV cache for every model**,
  CUDA graphs on. Gemma3-based models run `--language-model-only` (text only).
- **Baseline**: Qwen3.8-27B FP8 on the same card and stack (2 seqs, 8 GiB KV — it is a
  hybrid model). Production ran NVFP4 + DFlash on SGLang (41.5 GB right-sized, 132 ms
  translations, `../cue_eval/RESULTS-2026-09-shootout.md`) and is disabled as of this round.
- **The app**: the production Tenir image in the same pod (memory persistence, cues/music
  off), real STT via the production `tenir-parakeet` service, translation pointed at a
  prompt-adapter shim in front of vLLM. Everything goes through the shipped
  `OpenAITranslator` payload and `_parse`.
- **Data** (public only; frozen and seeded before any model ran):
  - `fleurs_gold`: 200 FLEURS es_419 test sentences, gold Spanish text → English refs.
  - `fleurs_asr`: the same 200 clips' audio streamed through the app; the app's own
    Parakeet finals and language tags are captured once and replayed identically to every
    model, with the production translate trigger (349 finals from 177 clips; the other 23
    produced no final — XERK-1349).
  - `fleurs_e2e`: each model live end to end — clips over the WebSocket, the app
    translates, `translation` messages scored (STT re-runs per model).
  - `opus_conv`: 300 short conversational OPUS-100 es→en lines (subtitle-style; refs are
    noisy). Tatoeba needs HF auth from the cluster, so it was replaced.
  - `passthrough`: 150 English FLEURS sentences sent as an inherited (untagged) turn; the
    right answer is the input unchanged.
- **Metrics**: COMET-22 (`Unbabel/wmt22-comet-da`), chrF++, BLEU; latency = one request in
  flight, warmed; e2e latency = `caption.final` → `translation` as the client receives it.
- **Prompts**: each model was run with the shipped prompt (fixed ~410-item stratified
  subset) and its documented native prompt (full set); the better one drove the e2e run.
- **Weights**: TranslateGemma via the ungated `Infomaniak-AI/vllm-translategemma-*` repack
  (the cluster has no HF token for Google's gated repo; byte sizes match Google's shards,
  hashes are unverifiable while gated).

## Results

Totals are the card's `memory.used` after warm-up with the fixed 2 GiB KV cache; peak is
the max of a 500 ms trace across the whole run (≤ 6 MiB above the total for every model).
"vs Qwen" is a paired bootstrap on segment-level COMET over the 260 shared units.

| model | VRAM total (weights) | vs Qwen, COMET Δ [95% CI] | gold | opus | asr | e2e | ms/turn asr (p90) | e2e ms (p90) | shipped prompt |
|---|---|---|---|---|---|---|---|---|---|
| **MiLMMT-46-1B** | **5,020 MiB** (1.96 GiB) | −0.12 [−0.55, +0.29] | 87.22 | 83.80 | 77.66 | 79.03 | **36** (55) | **44** (62) | ✗ 743/999 unparseable |
| Hy-MT2-1.8B | 6,336 MiB (3.34 GiB) | −0.57 [−1.02, −0.12] | 86.91 | 82.90 | 77.33 | 78.49 | 55 (86) | 64 (93) | ✓ 0 fails, Δ −0.96 |
| **MiLMMT-46-4B** | **10,946 MiB** (7.82 GiB) | **+0.54 [+0.17, +0.95]** | 87.60 | 84.52 | 78.25 | 79.74 | 98 (155) | 107 (159) | ✗ 357/411 unparseable |
| TranslateGemma-4B | 10,946 MiB (7.82 GiB) | −0.46 [−0.94, +0.03] | 87.08 | 82.08 | 78.09 | 78.92 | 103 (157) | 108 (165) | ✗ HTTP 400 (template) |
| Hy-MT2-7B | 18,212 MiB (13.98 GiB) | +0.26 [−0.13, +0.62] | 87.47 | 83.85 | 78.27 | 79.74 | 172 (272) | 179 (270) | ✓ 0 fails |
| MiLMMT-46-12B | 26,132 MiB (22.62 GiB) | +0.44 [+0.07, +0.82] | 87.73 | 84.74 | 78.46 | 80.00 | 278 (439) | 286 (431) | ✗ 88/411 unparseable |
| TranslateGemma-12B | 26,132 MiB (22.62 GiB) | — | 87.29 | 83.64 | 78.34 | 79.55 | 295 (458) | 298 (465) | ✗ HTTP 400 (template) |
| Qwen3.8-27B FP8 (baseline) | 39,728 MiB (27.61 GiB) | 0 | 87.38* | 83.51* | 77.59* | 79.14 | 452 (639)* | 475 (658) | ✓ (it is the prompt) |

\* Qwen ran the shipped prompt on the subset only; its subset scores compare against each
candidate's native output on the same items (bootstrap column). The other text columns
are each model's native prompt on the full set.

English passthrough (150 English sentences sent as an inherited turn):

| model | returned unchanged | chrF vs input |
|---|---|---|
| Qwen3.8-27B (subset) | 74% | 98.2 |
| MiLMMT-4B / 12B / 1B | 57% / 57% / 49% | 96.5 / 96.7 / 96.4 |
| Hy-MT2-7B / 1.8B | 31% / 9% | 91.1 / 83.1 |
| TranslateGemma-12B / 4B | 5% / 3% | 76.9 / 75.8 |

## Findings

- **Size buys almost nothing past 4B.** Every candidate lands within ±1 COMET of Qwen;
  MiLMMT-4B → 12B adds +0.1–0.3 at 2.4× the VRAM and 2.8× the latency. The cheapest model
  that is *better* than Qwen is MiLMMT-46-4B; the cheapest at *parity* is MiLMMT-46-1B.
- **Latency collapses.** MiLMMT-1B is ~11× and MiLMMT-4B ~4.5× faster than the FP8 Qwen
  baseline on the same card (and 3.7× / 1.3× faster than production's NVFP4+DFlash 132 ms).
- **No dedicated model is a zero-code swap except Hy-MT2.** MiLMMT is a completion-format
  model: under the shipped system prompt + `json_object` it rambles to the 1,000-token cap
  (1.2 s/call, 74% unparseable at 1B). TranslateGemma's chat template rejects the shipped
  request outright. An adapter (`public/shim.py` is a working one) is required.
- **The native adapters need the source language, and Tenir's tags are sometimes wrong.**
  7/100 frozen clips carry a non-`es` tag on Spanish speech (pt/fr/it on short turns).
  MiLMMT then translates "from Portuguese" — e.g. *"Hielo o polvo"* → *"Ice the octopus"*
  (*polvo* = octopus in pt). On those clips MiLMMT-4B loses ~0.2 COMET vs Qwen, Hy-MT2-7B
  (no source language in its prompt) gains ~2.6. Small overall, but it is the one failure
  class Qwen does not have.
- **Passthrough is the weak spot, but it is paraphrase, not mangling.** Told an English
  sentence is Spanish, MiLMMT rewords it ("It had been scheduled" → "It was scheduled")
  rather than corrupting it (contrast the Sept A3B "Um" → "One"). An adapter should skip
  the call when the turn's text is English (`api/src/api/stt/langid.py` already decides
  that) instead of claiming a source language.
- **Hy-MT2 on vLLM 0.30 needs a RoPE override to be fast.** vLLM serves
  `HunYuanDenseV1ForCausalLM` only through its Transformers backend, whose "dynamic" RoPE
  wrapper breaks CUDA-graph capture (eager: 333 ms/turn at 1.8B). Rewriting it as
  `rope_type: default` with the equivalent static base is exact (40/40 token-identical vs
  stock, eager vs eager) and gives 119 ms. See `.claude/rules/translation-eval.md`.

## Recommendation

Adopt **MiLMMT-46-4B** as the dedicated translation model (smallest model that beats the
baseline with a significant margin; 10.9 GB, ~100 ms). If VRAM is the binding constraint,
**MiLMMT-46-1B** (5.0 GB, ~40 ms) is statistically indistinguishable from Qwen. Either
needs, in the translator:

1. a native prompt mode (`Translate this from <Src> to English:\n<Src>: …\nEnglish:`,
   `/v1/completions`, stop at newline, greedy) instead of the JSON envelope;
2. no call for English-detected text (passthrough), and the run's language — not "Spanish"
   — for inherited turns.

Not yet measured: FP8/INT8 quantization of the 4B (would roughly halve its weights), other
source languages (MiLMMT covers 46; this round is Spanish only), and real household
conversations (the private-export judge round in `README.md` §1–3) — run that on the
chosen model before cutover.

## Limits

- FLEURS is read Wikipedia-style speech; OPUS refs are noisy. Conversational quality is
  approximated by `opus_conv`, not measured on household audio.
- COMET differences under ~0.4 are inside the bootstrap noise; the table's rank order
  among the four mid-size models is not stable.
- `fleurs_e2e` re-runs STT per model (untranslated turns vary 34–51), so it is noisier
  than the frozen `fleurs_asr` comparison.
- The app's STT dropped ~9% of Spanish turns during capture (23/200 clips with no final in
  one pass) — XERK-1349; identical for every model in the frozen set.
