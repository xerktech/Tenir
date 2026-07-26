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

## Iteration 2 — gpt-oss-120b (run 2026-07-26, via Ollama)

**Serving note**: vLLM cannot load gpt-oss-120b on this host — SM120 gets the
Marlin MXFP4 fallback and its load-time repack transient OOMs the 96 GB card
every time (four attempts across shares 0.70/0.85, `--enforce-eager`, and
allocator tuning; WSL2 rules out the VMM-based mitigations — DockerOps
#104-#108). It runs cleanly on **Ollama/llama.cpp** (`gpt-oss:120b`, ~65 GB
GGUF, streams with no repack), which the eval used. Ollama latency is NOT
runtime-comparable to the vLLM rows (single-request stream; replayed with
`--workers 1`). Judged cross-family by Qwen.

| model | cues | emit% | novelty | relevance | accuracy | perfect | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-oss-120b, reasoning medium | 234 | 35% | 1.81 | 1.71 | **1.96** | **152** | 12 | **4** | 14 | 43 | 1.98 |
| gpt-oss-120b, reasoning low | 362 | 54% | 1.59 | 1.73 | 1.92 | 189 | 48 | 10 | 69 | 50 | 1.12 |

**The scale hypothesis held.** At medium effort the 120b emits 10x the current
model's volume at HALF its wrong-rate (1.7% vs 4.5%) — 152 judged-perfect cues
vs Qwen's 14 on the same replay. Hand review of all four wrong cues shows a
single failure class: confidently defining a misheard STT token ("GPX",
"CQV", "Yonka") instead of skipping it — the garbled-name rule holds less
firmly at this emit rate, a future prompt-tuning target. Its 43
control-conversation cues are mostly accurate definitions (carapace, Dolly the
sheep, SCNT) the judge docked on relevance, same pattern as the 20b in round
1. Low effort adds volume but degrades novelty (48 restates) and duplicates
(69 caught by the substance backstop) — medium is the shipping setting.

## Round 3 — Mistral Small 3.2 24B FP8 (run 2026-07-26)

vLLM note: Mistral's tokenizer mode rejects `chat_template_kwargs` with a 400
— replayed with `--no-template-kwargs` (harness flag added for this).

| model | cues | emit% | novelty | relevance | accuracy | perfect | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| mistral-small-3.2-24b FP8 | 207 | 31% | 1.36 | 1.79 | 1.61 | 73 | 33 | **30** | 35 | 18 | **0.71** |

Fastest runtime of any candidate (0.71 s/call) and an emissive posture, but
the worst calibrated-run precision: 14.5% of its cues are judged wrong and 16%
restate the transcript. Same knowledge-frontier confabulation class as the
other small models, at higher volume. Eliminated.

## Decision

**Recommend gpt-oss-120b at reasoning-effort medium as the cue model.** Final
standings across every calibrated run (frozen 12-conversation replay, shipped
enrichment prompt, cross-family judged):

| model | perfect cues | wrong rate | emit% | runtime |
|---|---|---|---|---|
| **gpt-oss-120b, medium** | **152** | **1.7%** | 35% | Ollama, 1.98 s/call |
| gpt-oss-120b, low | 189 | 2.8% | 54% | Ollama, 1.12 s/call |
| gemma-3-27b | 179 | ~9% | 66% | vLLM, 1.66 s/call |
| gpt-oss-20b, low | 125 | 8.8% | 37% | vLLM, 0.83 s/call |
| mistral-small-3.2 | 73 | 14.5% | 31% | vLLM, 0.71 s/call |
| gpt-oss-20b, medium | 20 | 3.1% | 5% | vLLM, 1.98 s/call |
| **Qwen3.6-27B (current)** | 14 | 4.5% | 3% | vLLM, 1.30 s/call |

The 120b at medium is the only candidate that delivers the product goal —
substantially more cues at maintained-or-better accuracy. It is also the only
one whose volume comes from knowledge depth rather than loosened discipline:
every other high-volume run paid 9-15% wrong cues.

Open items for productionizing (not part of this eval):

1. **Serving**: production cues would ride Ollama (:9406) until vLLM's SM120
   MXFP4 path can load the model (upstream work exists). LiteLLM can alias an
   Ollama endpoint, so the api needs no change beyond the alias target and
   `reasoning_effort: medium` on the request (a small `api` config addition).
2. **Concurrency**: Ollama serves the eval fine single-stream; cue attempts
   are serialized per session anyway. Multi-session households should verify
   `OLLAMA_NUM_PARALLEL` covers their session count.
3. **Prompt fine-tune**: the misheard-name failure class (4 cues) suggests
   strengthening the garbled-name rule for this model; the relevance dings on
   ambient-media definitions may also warrant a "only cue what the LISTENER
   is engaged with" nudge. Both are small calibration passes with this same
   harness.
4. **VRAM**: 120b (~65 GB) + both STT models (~5 GB) fit the 96 GB card with
   room to spare once the eval services are torn down; Qwen would retire.

## Deployment verification & final tuning (2026-07-26, post-promotion)

gpt-oss-120b was promoted (DockerOps #110) and wired through LiteLLM. Final
verification against the deployed layout:

- **Gateway overhead is negligible.** Identical cue-style calls, 12 each,
  warm: direct Ollama **1.440 s**, LiteLLM via LAN **1.424 s**, LiteLLM via
  the Cloudflare tunnel (the api's actual path) **1.460 s**. Routing through
  the gateway — even the tunnel — costs ~1–2%. No endpoint change needed.
- **Gateway entry must use OpenAI-compat passthrough.** A LiteLLM entry using
  the native `ollama` provider returns EMPTY content whenever the request
  carries `response_format: {"type": "json_object"}` — which the api sends on
  every cue call. Isolated by ablation: same payload works with
  response_format removed, and works with response_format against Ollama's
  own /v1. The entry must be `model: openai/gpt-oss:120b` with
  `api_base: http://GPU_HOST:9402/v1` (the surface this whole eval
  validated), plus `reasoning_effort: medium` pinned.
- **Also fixed**: the api's `API_LLM_MODEL` default now names the gateway
  entry directly (`gpt-oss:120b`, DockerOps #111) — the retired `qwen3-llm`
  alias 403s.
- **Prompt v6 (shipped in this PR)**: the misheard-name rule now includes a
  domain-fit check (a famous meaning from the wrong domain = a mishearing —
  skip the name). Full replay at medium effort: garbled-name cues **5 → 3**
  with volume **up** (234 → 243) and controls unchanged. The residual ~1% is
  boundary noise — single-window A/Bs flip run to run — and is best cleaned
  up by the live retrieval evidence in production (a misheard name retrieves
  nothing that supports the wrong definition), not by more prompt text.
