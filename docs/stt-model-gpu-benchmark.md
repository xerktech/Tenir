# STT model selection — GPU benchmark results

Empirical follow-up to `stt-model-selection.md` (PR #66 / XERK-115). That doc's
model recommendation was reasoned from published numbers and explicitly flagged as
"not verified — anything needing the GPU host." This is that verification, run on
the RTX PRO 6000 Blackwell here.

**TL;DR.** The migration to a cache-aware streaming model buys **latency and
concurrency, not accuracy**. On identical audio, `nemotron-3.5-asr-streaming-0.6b`
in streaming mode is **1.5–3.4 WER points *worse*** than the Parakeet-TDT-v3 we run
today in its offline mode — because Tenir already gets offline accuracy on its
*finals* (it re-decodes the whole turn). What Nemotron fixes is responsiveness:
incremental, bounded, constant-latency partials that don't lag as a turn grows, plus
far more concurrent streams. The doc's framing ("Parakeet loses ~46% when chunked,
so Nemotron is more accurate") does not hold for how Tenir actually uses Parakeet.
The right move is a **hybrid** (Nemotron partials + Parakeet final), and if a single
model is required, Nemotron at **320 ms** (`att_context_size=[56,3]`), not the 560 ms
the doc suggested.

---

## Setup

| | |
|---|---|
| GPU | NVIDIA RTX PRO 6000 Blackwell (sm_120), 97 GB |
| Stack | torch 2.11.0+cu128, NeMo `main` (3.1.0+fb20f06), fp32 |
| Eval set | FLEURS `test` — English (`en_us`) and Spanish (`es_419`) |
| Samples | batch/offline: 200 utt/lang; streaming: 100 utt/lang |
| WER | jiwer, both sides lower-cased, punctuation + trailing lang-tag stripped |
| Precision | fp32 for all models (mel preprocessor is precision-sensitive; a blanket `.half()` corrupts it) |

Note on absolutes: my Nemotron FLEURS numbers land ~1–1.5 pts above NVIDIA's
published card (100-sample subset + a different text normalizer). Treat Nemotron's
absolute WER here as mildly *pessimistic*; the **relative ranking is on identical
audio and is what the decision rests on**. Method was validated against Parakeet's
published ~6.3 % batch: measured EN 5.87 %, ES 2.97 % — in line.

A NeMo `model.transcribe()` call triggers a `CUDA driver error: unknown error` under
WSL (its Lhotse dataloader uses CUDA off the main process). All numbers below come
from driving `preprocessor → encoder → decoding` (and `conformer_stream_step` for
streaming) directly, which matches `transcribe()` output verbatim and avoids the bug.

---

## 1. Offline / batch accuracy (the ceiling)

| Model | EN WER | ES WER | RTFx (fp32) | Notes |
|---|---|---|---|---|
| **parakeet-tdt-0.6b-v3** (today) | **5.87 %** | **2.97 %** | 610 / 500 | 25 langs, auto-detect |
| parakeet-unified-en-0.6b | **4.51 %** | — | 291 | most accurate in EN, **English-only** |
| nemotron-3.5-asr-streaming-0.6b | 10.59 %* | 6.68 %* | — | *= its `[56,3]` single-shot, not a true full-context run |

Parakeet-TDT-v3 offline is the accuracy ceiling for the multilingual default, and
Tenir already reaches it on finals. Parakeet-unified is more accurate still in
English but can't be the default (English-only); keep it in mind only for a future
English mode.

## 2. Nemotron cache-aware streaming — accuracy vs latency

`att_context_size = [56, R]`, left context fixed at 56 frames (≈4.5 s), R = right
lookahead. Chunk compute is single-stream, batch = 1, on this GPU.

| `[56,R]` | Algo latency | EN WER | ES WER | Chunk compute (med / p95) |
|---|---|---|---|---|
| [56,0] | 80 ms | 11.80 % | 7.32 % | 17 / 23 ms |
| [56,1] | 160 ms | 11.66 % | 7.39 % | 19 / 25 ms |
| **[56,3]** | **320 ms** | **10.59 %** | **6.68 %** | 19 / 25 ms |
| [56,6] | 560 ms | 10.31 % | 6.92 % | 20 / 26 ms |
| [56,13] | 1120 ms | 9.28 % | 6.45 % | 20 / 28 ms |

Two things fall out of this that the doc didn't have:

- **Chunk compute is flat (~20 ms) across every latency setting and independent of
  turn length.** This is the core architectural win, and it's real: the model
  encodes each frame once and carries cached state. Time-to-emit ≈ chunk lookahead +
  ~20 ms.
- **320 ms is the sweet spot, not 560 ms.** EN is nearly flat from 320→560 ms
  (10.59→10.31) and ES is actually *best* at 320 ms. Only jumping to 1120 ms buys
  real EN accuracy (−1.3 pts), at 3.5× the latency. The doc's `[56,6]`/560 ms default
  costs latency for no measurable accuracy over `[56,3]`.

## 3. Parakeet re-decode cost (Tenir's problem #1, measured)

Tenir re-decodes the *entire* in-flight turn every 700 ms. Simulated on a 21 s turn:

| In-flight turn | This partial | Cumulative compute / turn |
|---|---|---|
| 0.7 s | 18.6 ms | 18.6 ms |
| 7.0 s | 24.6 ms | 212 ms |
| 14.0 s | 29.6 ms | 500 ms |
| 21.0 s | 31.6 ms | 823 ms |

Per-partial latency **does** grow with turn length (18→32 ms, ~1.7×) and per-turn
compute is super-linear — the doc's directional claim is confirmed. **But on this
GPU the absolute cost is tens of milliseconds.** Re-decode compute is *not* Tenir's
user-facing bottleneck on capable hardware; the 700 ms endpoint tax and the coarse
700 ms partial cadence are. (On a weak/loaded GPU the quadratic term would bite
harder — it's a scaling risk, not a today problem here.)

---

## 4. What this means for the decision

**Reframing the gap.** Tenir's finals already get Parakeet's offline accuracy
(5.87 / 2.97) because it re-decodes the whole turn at the endpoint. So Parakeet is
*not* accuracy-limited the way the doc implies — it's **responsiveness-limited**:
coarse 700 ms partials, a 700 ms endpoint tax, and partials that lag as the speaker
talks. Nemotron does not beat Parakeet on accuracy; measured on the same audio it's
1.5–3.4 pts worse in streaming. It wins decisively on *latency shape* (fine-grained,
bounded, constant partials) and on *concurrency* (cache-aware ⇒ many more streams).

**So "which fits best" depends on the priority — and the two aren't exclusive:**

- **Recommended — hybrid.** Nemotron `[56,3]` (320 ms) for the live streaming
  partials, Parakeet-TDT-v3 offline re-decode for the committed final at the
  endpoint. This delivers Conversate-class responsiveness on the live caption **and**
  keeps Parakeet's better final accuracy (5.87 / 2.97) for the stored transcript,
  making the streaming accuracy regression irrelevant to what's saved. Both are 0.6 B;
  together they fit in <5 GB and run side by side. This matches the doc's own §5.4
  "keep the endpointing" instinct — it just keeps the accurate decoder too.

- **If one model only, and latency is the priority:** migrate to Nemotron at
  `[56,3]` / 320 ms (not 560 ms). Accept ~10.6 % EN / 6.7 % ES streaming WER.

- **If final-transcript accuracy is paramount and current latency is tolerable:**
  stay on Parakeet-TDT-v3 and instead attack latency directly — lower the 700 ms
  silence timeout and partial cadence (the doc's own "biggest remaining win"), which
  needs none of this migration.

**Not the default, but noted:** `parakeet-unified-en` is the most accurate English
option (4.51 %) and does offline+streaming in one model — the right pick *only* if an
English-only mode ships. Its streaming path is chunk-based, not cache-aware `[56,R]`,
so it wasn't on the same curve here.

## 5. Corrections to `stt-model-selection.md`

- The "~46 % relative when chunked" penalty is real for *fixed-chunk* Parakeet, but
  it does **not** describe Tenir, which re-decodes the whole turn and gets offline
  accuracy on finals. The doc's implication that Nemotron is *more accurate* in
  practice is not supported — measured, Nemotron streaming is **less** accurate than
  Parakeet on the same audio.
- Recommended operating point: **`[56,3]` / 320 ms**, not `[56,6]` / 560 ms — the
  accuracy curve is flat between them here.
- The re-decode quadratic cost is confirmed but is **milliseconds** on this GPU, not
  a headline latency source; the endpoint/cadence tax is the real target.

## 6. Implementation

The hybrid is implemented (`API_STT_BACKEND=hybrid`, the compose default):

- **`nemotron-stt/`** — a new GPU model server serving `nemotron-3.5-asr-streaming-0.6b`
  over a WebSocket (`/v1/audio/stream`), one persistent cache-aware stream per session,
  at `att_context_size=[56,3]` (320 ms). Compose service `nemotron` (`:9402`).
- **`api/src/api/stt/streaming_engine.py`** — an async streaming-engine seam + the
  Nemotron WS client.
- **`api/src/api/stt/streaming.py`** — `StreamingTranscriber` gains an optional
  `stream_engine`: partials come from the live stream, finals still decode the whole
  turn on the offline Parakeet engine. Falls back to Parakeet-only re-decode partials
  if the stream is unavailable.

Verified: the api↔server wire protocol end to end over a real socket, the hybrid
transcriber logic, and the server transport — all against fakes, so no GPU in CI.
Not verified here: the NeMo cache-aware decode inside `NemotronStreamDecoder` (needs
the GPU host, like `parakeet-stt`'s own model path).
