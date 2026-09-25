# Cue-model eval — September 2026 shoot-out (model, VRAM and latency)

Four rounds, 2026-09-23/24, on the MaxAI RTX PRO 6000 (97.9 GB, sm_120). The questions:

1. How much VRAM does Tenir's LLM actually need?
2. Is gpt-oss-120b or another model better than Qwen3.8-27B for cues?
3. Can time-to-cue drop from ~7–9 s without losing accuracy?
4. Can cues + translation + Parakeet move to a 24–48 GB card?

The previous round is `RESULTS-2026-09.md` (the A3B spike); July/August are `RESULTS-2026-07.md` / `-08.md`.

## Method

- **Data**: the frozen 6-conversation set (795 gated attempts at the stock 2.5 s spacing) and the 1,271-utterance translation set, same exports as `RESULTS-2026-08.md`.
- **Replay** (`replay.py`, the shipped payload):
  - Rounds 1–3 ran ungrounded.
  - Round 4 added `--grounded` (Tenir's `LiveEvidenceRetriever`: Wikipedia → Kiwix fallback, SearXNG; no news FTS). Evidence was cached per transcript window, so every model saw identical evidence. 760/917 windows had evidence.
  - Round 4 also added `--realtime`: attempts are spaced by measured call latency, one in flight, as a live session does. The stock fixed 2.5 s spacing flatters slow models; a 9 s model gets ~500 attempts on this set and a 2.5 s pipeline ~820.
- **Latency**: `latency_probe.py`, one request at a time on an otherwise idle server. Multi-worker replay call times include queueing.
- **Judging**: blind cross-family review (`blind_judge.py`, a Claude subagent per pack, model identity stripped and shuffled) is the primary signal.
  - The model judges (`judge.py` with Qwen3.8-27B or gpt-oss-120b) miss their own error class. gpt-oss judged 3 of its own 273 cues wrong where the blind review found 37. Hand-checks of sampled blind verdicts held up.
  - Every batch re-judged an anchor run. Per-run wrong-rates on ~120 cues carry ±3 pts of noise between batches; "perfect" (novelty 2, relevance 2, accuracy 2, not a duplicate) is the steadier signal.
- **VRAM**: steady-state `nvidia-smi` delta over the idle card.
- **SGLang image**: `lmsysorg/sglang:dev-qwen38-27b-dflash2` unless noted.
- **Known limits of these numbers** (found by QA after the runs; the harness now guards each):
  - The blind packs showed reviewers one transcript line spoken *after* each cue. The same bias applied to every run in a batch, so comparisons hold, but absolute novelty is slightly understated. `blind_judge.py` now stops at the cue's own segment.
  - The 157/917 evidence-less windows include Wikipedia 429s cached as empty, not only genuine no-hit windows. Every model saw the same cache, so the comparison holds. `replay.py` now reports empty-evidence counts and can `--refetch-empty`.
  - Round 4's realtime spacing left retrieval latency out of the in-flight window, so grounded "time to cue" and attempt counts are slightly optimistic for every config alike. It now charges the fetch time.
  - Verifier failures were counted as unsafe verdicts. No gpt-oss/Mistral path was verified, but a malformed Qwen verdict would have been indistinguishable from a strict one. They are now counted separately as `verify_errors`.

## Round 1 — VRAM right-sizing, and Qwen3.8-27B vs gpt-oss-120b

The production compose (262K context, 256 requests, mem-fraction 0.75) holds **78.0 GB**:
- weights 21.8 GB + DFlash draft 3.7
- linear-attention (mamba) state for 52 slots ~20
- KV 18 + draft KV 5.6
- CUDA graphs ~5
- ~13.5 GB idle headroom

Qwen3.8 is a hybrid model (16 full-attention + 48 Gated-DeltaNet layers):
- KV is cheap (~32 KB/token).
- Each concurrent request costs ~2.15 GB of recurrent state.
- The worst-case cue prompt is 3,684 tokens.

| Qwen3.8-27B config | VRAM | notes |
|---|---|---|
| production | 78.0 GB | |
| 16K ctx, 4 req, 64K-token KV, prefill graphs ≤512 tok | **41.5 GB** | same latency and accuracy; translations 132 ms |
| same, 2 req | 35.7 GB | a 3rd concurrent call queues |
| same, no DFlash | 30.4 GB | cue p90 36 s > the api's 30 s timeout — rejected |

gpt-oss-120b:
- On SGLang it holds 71–73 GB: the MXFP4 MoE kernels repack/upcast the experts on SM120.
- Its scheduler crashed twice under 4 concurrent cue requests: a hybrid-SWA pool "Out of memory", then a false `pool memory leak detected`.
- On llama.cpp (MXFP4 GGUF, 4×16K slots) it holds **64.9 GB** with 0 errors.

| | Qwen3.8-27B (41.5 GB) | gpt-oss-120b, llama.cpp (64.9 GB) |
|---|---|---|
| cue latency mean / p90 | 7.0 s / 14.2 s | **1.5 s / 2.4 s** |
| cues (795 attempts) | 180 | 273 |
| wrong (blind) | **6 (3.3%)** | 37 (13.6%) |
| perfect | **119** | 65 |
| off-topic | **1** | 40 |
| translation mean | **132 ms** | 433 ms |
| translation blind A/B (150 differing pairs) | adequacy tie, fluency 1.76 vs 1.55, preferred 34× | preferred 17× (99 ties) |

- gpt-oss's errors are the July failure classes at higher volume: sibling-generation specs pasted onto a newer product, false "corrections" of firsthand specs, invented expansions of misheard internal acronyms.
- Its July prompt (600 tokens) gave 137 cues at 16.8% wrong.
- **Truncation**: 12–17% of Qwen cue calls hit `max_tokens` = 2048 mid-thought (`finish_reason: length`, empty content) and are silently dropped. That is XERK-977. 4096 tokens gave +19% cues at the same wrong-rate, but a max of 29.9 s against the 30 s timeout.

## Round 2 — cutting Qwen3.8-27B's time-to-cue

Latency is almost all reasoning: ~950 thinking tokens at ~130 tok/s.

| lever | cue mean / p90 | cues | wrong (blind) | perfect |
|---|---|---|---|---|
| uncapped (baseline in this batch) | 6.9 s / 14.0 s | 180 | 5.6% | **113** |
| thinking cap 512 | 3.5 s / 5.0 s | 181 | 9.9% | 90 |
| thinking cap 256 | 2.0 s / 3.0 s | 152 | 7.2% | 66 |
| thinking cap 128 | 1.1 s / 1.8 s | 140 | 13.6% | 49 |
| thinking off | 0.2 s / 0.5 s | 49 | 26.5% | 13 |

How the cap works:
- It is SGLang's grammar-enforced thinking budget, applied because the cue call uses `response_format: json_object`.
- Server-wide: env `SGLANG_MAX_THINK_TOKENS` + `--enable-strict-thinking`.
- Per request: `custom_params.thinking_budget`. LiteLLM's `drop_params` would strip the per-request form.
- A cap never truncates, so it also removes the XERK-977 silent drops.

Dead ends:
- "Reason briefly" prompt text made the model *slower* (8.3–8.7 s).
- DFlash with 4 or 16 draft tokens is slower than the default 8 (88 / 109 vs ~130 tok/s).
- Qwen3.6-35B-A3B-FP8 + MTP (45 GB) thinks ~1,700 tokens, so it averaged 8.4 s with 21/40 truncated.

## Round 3 — Qwen/Qwen3-30B-A3B (FP8)

- Qwen/Qwen3-30B-A3B-FP8 on the corrected harness: 35.2 GB, standard-attention MoE.
- Anchors re-judged in the same batch matched round 1 (27B 3.9%, gpt-oss 13.6%).

| thinking | mean / p90 | cues | wrong | perfect |
|---|---|---|---|---|
| uncapped | 3.0 s / 4.4 s | 61 | 18.0% | 11 |
| cap 256 | 1.6 s / 1.8 s | 152 | 23.0% | 20 |
| off | 0.16 s / 0.31 s | 103 | 28.2% | 11 |

- An EAGLE3 draft (`zhuyksir/EAGLE3-Qwen3-30B-A3B-DenseHead`) made it slower.
- Translations: 112 ms. Blind A/B vs the 27B: adequacy 1.61 vs 1.67, fluency 1.40 vs 1.76; 27B preferred 46×, A3B 9×.
- Its errors are invented meanings for misheard names and false corrections — a 3B-active capability limit that prompt tuning will not close.

## Round 4 — a fast, accurate system for a 24–48 GB card

### Candidates that failed (grounded replay, blind)

| candidate | VRAM | cue mean | wrong | perfect | anchor (27B xhigh, same batch) |
|---|---|---|---|---|---|
| Qwen3.8-27B `reasoning_effort=low` (template default is `xhigh`) | — | 3.3 s | 19.5% | 50 | 3.4%, 99 perfect |
| IBM Granite 4.2 30B NVFP4, effort low | 23.8 GB | 1.3 s | 11.8% | 16 | 4.8%, 105 |
| Granite 4.2 30B, thinking off | 23.8 GB | 0.28 s | 17.4% | 1 | 4.8%, 105 |
| Nemotron 3.5 Lightning 30B-A3B NVFP4 + MTP, thinking off | 28.7 GB | 0.19 s | 23.6% | 7 | 4.8%, 105 |

- Low effort skips the model's self-checking. Its errors are confident wrong specifics: bad arithmetic, wrong specs, "corrections" of what the speaker is looking at.
- Granite with full thinking averages 29 s (53 tok/s dense, no drafter).
- Nemotron with thinking on runs ~1,700 tokens at 360 tok/s (≈5 s), and SGLang's thinking budget is not honoured by its template.
- HY-MT1.5-1.8B-FP8 as a separate translator crashed on this SGLang build (ragged-prefill shape error). It would save ~nothing vs one extra 27B slot, so it was dropped.

### What works: the compact 27B + a self-check

- **Compact checkpoint**: `gittensor-model-hub/Qwen3.8-27B-NVFP4-RTX5090` (17.9 GB: NVFP4 `lm_head`, MTP head removed) + its `Qwen3.8-27B-DSpark-NVFP4` drafter (1.4 GB), SGLang `--speculative-algorithm DSPARK`.
  - Same decode speed as RadixArk NVFP4 + DFlash.
  - 29.6 GB at 2 slots; **27.8 GB lean** (prefill CUDA graphs off, 20K-token KV). The lean config costs translations +65 ms (221 ms mean).
- **Self-check** (`replay.py --verify off`): after a cue is generated, the same model (thinking off, ~0.3 s) decides whether every claim is correct, consistent with what the speakers said, and about a real in-conversation subject. Unsafe cues are dropped.

Latency-realistic grounded replay (`--grounded --realtime --workers 1`), all four in one blind batch:

| config | time to cue mean / p90 | cues | wrong | perfect |
|---|---|---|---|---|
| xhigh (today's behaviour) | 9.1 s / 16.5 s | 124 | 8.9% | **73** |
| **xhigh + self-check** | 9.1 s / 16.7 s | 106 | **2.8%** | 59 |
| **thinking cap 512 + self-check** | **4.4 s / 5.5 s** | 115 | 9.6% | 62 |
| thinking cap 256 + self-check | 2.5 s / 3.2 s | 112 | 14.3% | 48 |

- The self-check removes coarse errors: misheard-name inventions, false corrections, off-topic cues.
- It cannot catch subtle spec errors (a wattage, a BIOS key) without thinking, which is why it helps full-thinking output most.
- The accuracy the 27B gets from long reasoning has no cheap substitute in this model set.

### VRAM for a smaller card (measured)

| component | GB |
|---|---|
| compact 27B + DSpark, 2 slots, lean | 27.8 |
| Parakeet TDT 0.6B v3, fp32 / fp16 (`model.half()`, transcription accuracy unverified) | 3.4 / 2.1 |
| translation | 0 extra (stays on the 27B, thinking off) |

- **48 GB card**: comfortable.
- **32 GB card**: ~29.9 GB with fp16 Parakeet. It fits only with the lean config and needs an STT check of fp16 first.
- **24 GB card**: nothing that fits (≤18 GB left beside Parakeet) was accurate enough; every candidate measured 12–28% wrong.

## Decisions open

- Full thinking + self-check (most accurate, ~9 s) vs thinking cap 512 + self-check (today's accuracy, ~4.4 s).
- 32 GB card (lean config + fp16 Parakeet, pending an STT check) vs 48 GB.
- Shipping either needs:
  - the self-check in the api's cue path;
  - the thinking budget as a server env;
  - a fp16 option in `parakeet-stt/server.py`;
  - the compose for the new card.
