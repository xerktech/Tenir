# STT model selection

Research for [XERK-115](https://xerktech.atlassian.net/browse/XERK-115): why the Even
G2's built-in **Conversate** app transcribes faster and more accurately than Tenir,
what we can do about it on the model we have, and which model we should move to.

**The headline:** the gap is mostly *architectural*, not a bad model pick. Parakeet is
a strong ASR model that we are using in the one mode it is worst at — chunked
re-decoding — and published measurement puts the cost of that at **~46% relative WER**.
Conversate isn't beating us with a better GPU; it's a true streaming pipeline. The fix
that closes most of the gap is to stop re-decoding and start streaming.

---

## 1. What Conversate actually is

Even publish very little, so this is assembled from their support/privacy docs, a
third-party security teardown, and the community G2 STT app.

| Question | Finding |
|---|---|
| On-device or cloud? | **Cloud.** "Conversate requires an internet connection through your phone"; with the phone offline the feature is unavailable. |
| Who transcribes? | An unnamed third party. Even's privacy policy lists the recipient categories "**iOS ASR voice service provider**", "real-time translation service provider" and "AI service provider" without naming any of them. A March 2026 security review of the G2 flagged exactly this non-disclosure and could not identify the endpoints. |
| Audio retention | Original recordings are deleted after transcription; only transcripts and summaries persist on the phone. |
| Claimed latency/accuracy | **None published.** Their marketing page gives no latency, language list, WER or speaker-separation numbers. |

So there is no "Conversate model" to copy. What there *is* to copy is the **shape**:
a persistent WebSocket to a streaming ASR service that emits incremental tokens as
the audio arrives.

The community reference implementation for the same glasses — [`nickustinov/stt-even-g2`](https://github.com/nickustinov/stt-even-g2)
— does precisely this against **Soniox**: resample to 16 kHz S16LE mono PCM, push it
down a WebSocket as binary frames, render the returned tokens on the lens. That is the
same audio format Tenir already captures, which is worth noting — the gap is not in
our capture path.

For scale, a commercial streaming service of that class reports **~249 ms median
time-to-final-segment** (281 ms p95) and **1.25% semantic WER** across 60 languages.
That is the bar users are implicitly comparing us against.

### Is Even's advantage reproducible by us?

Partly, and the important part is. We are not going to beat a hyperscaler ASR service
on WER with a 0.6B self-hosted model, and we shouldn't try — Tenir's whole premise is
self-hosted, so cloud ASR is off the table by design. But **the latency advantage is
almost entirely architectural**, and that we can have.

---

## 2. Why our pipeline is slow

Today (`api/src/api/stt/streaming.py` + `parakeet-stt/server.py`):

```
mic → PCM → StreamingTranscriber buffers a turn
          → every 700 ms: encode the WHOLE in-flight turn as WAV
                        → HTTP POST → LiteLLM → Parakeet → full offline decode
          → on 700 ms of trailing silence: decode the whole turn AGAIN for the final
```

Four separate problems compound here:

**1. We re-decode the same audio over and over.** LocalAgreement-2 needs every
hypothesis anchored at the same audio start, so partials decode the *entire* in-flight
segment each cadence — not a trailing window. A 10-second turn at a 700 ms cadence
decodes ~14 times, and the *n*-th decode is *n* × 700 ms of audio. Cost per turn grows
quadratically, and partial latency grows with how long the speaker has been talking.
This is why it feels progressively laggier the longer someone speaks.

**2. Parakeet TDT is an offline model being used as a streaming one.** This is the big
accuracy cost, and it's measured. From *Pushing the Limits of On-Device Streaming ASR*
(arXiv 2604.14493), which benchmarked exactly this adaptation across 15+ chunking
configurations:

| Model | Mode | WER | Delay |
|---|---|---|---|
| Parakeet TDT 0.6B | batch (offline) | **6.32%** | — |
| Parakeet TDT 0.6B | best chunked config | **9.22%** | 2.4 s |
| Parakeet TDT 0.6B | worst chunked config | 16.46% | — |
| Nemotron (cache-aware streaming) | batch | 7.07% | — |
| Nemotron (cache-aware streaming) | streaming (7,10,7) | **7.28%** | **0.56 s** |

Parakeet loses **~46% relative** when chunked, and is wildly sensitive to the exact
chunk/context configuration. A model *built* for streaming loses 3% relative — and does
it at a quarter of the delay. The paper's conclusion is blunt: models not designed for
streaming "fundamentally struggle with streaming constraints rather than simply
requiring parameter tuning." We cannot tune our way out of this one.

**3. Every final pays a fixed 700 ms silence tax, then a full re-decode.** Time-to-final
is `700 ms (endpoint detection) + a from-scratch decode of the whole turn`. Against a
~250 ms streaming TTFS, the endpointing alone is already 3× over budget.

**4. The energy VAD had a hard failure mode in noise.** `stt_silence_rms` was an
*absolute* gate at 0.005 RMS (≈ −46 dBFS). In any room whose noise floor sits above
that — a café, a car, a fan, which is most of the places you'd wear these glasses — no
frame is ever classified as silent, so **no turn ever closes on a pause** and every
caption waits out the full `stt_max_segment_ms` (12 s). This is fixed in this ticket
(§4).

---

## 3. Model options

Open, self-hostable, in the size class we can run alongside the cue LLM.

| Model | Type | Streaming? | Accuracy | Latency | License |
|---|---|---|---|---|---|
| **`parakeet-tdt-0.6b-v3`** (today) | FastConformer-TDT | ✗ offline; chunked only | 6.32% batch / **9.22% chunked** | 2.4 s chunked | CC-BY-4.0 |
| **`nemotron-3.5-asr-streaming-0.6b`** | Cache-aware FastConformer-RNNT | ✓ **native** | 7.07% batch / **7.28% @ 0.56 s** | **80 ms – 1.12 s, runtime-selectable** | OpenMDW-1.1 |
| **`parakeet-unified-en-0.6b`** | Unified FastConformer-RNNT | ✓ native (+offline) | 5.91% offline / 6.14% @ 2.08 s / 8.44% @ 160 ms | 160 ms – 2.08 s | NVIDIA Open Model |
| Kyutai STT 1B / 2.6B | Delayed-streams | ✓ native | — | 1 s / 2.5 s | CC-BY |
| Canary-Qwen-2.5B | Speech-LLM | ✗ offline | Best-in-class English | RTFx 418 | CC-BY |
| Whisper large-v3-turbo | Encoder-decoder | ✗ offline | — | slow | MIT |

Ruled out quickly:

- **Canary-Qwen** — most accurate open English model, but offline-only and 6.5× slower
  than Parakeet. It's the wrong axis; we're latency-bound, not accuracy-bound.
- **Kyutai STT** — genuinely streaming and well-regarded, but **English and French
  only**. Tenir's Parakeet v3 already does ~25 European languages with auto-detection,
  and Spanish is a live requirement. Regression.
- **Whisper / distil-whisper** — no advantage over Parakeet on either axis.
- **Cloud (Soniox, Deepgram, AssemblyAI, Speechmatics)** — the fastest and most accurate
  option by a wide margin, and the one Even themselves use. Excluded by Tenir's
  self-hosted premise, not on merit. Worth revisiting only if that premise ever changes.

### Recommendation: `nvidia/nemotron-3.5-asr-streaming-0.6b`

- **Cache-aware FastConformer-RNNT**, 600M params — same size class and hardware
  footprint as what we run now, so it's a drop-in on the existing GPU.
- Encodes each audio frame **exactly once** and carries encoder state across chunks.
  This structurally eliminates problem #1: no re-decoding, and cost per turn becomes
  linear instead of quadratic.
- **Runtime-selectable latency** via `att_context_size=[left, right]` in 80 ms frames —
  `[56,0]`=80 ms, `[56,1]`=160 ms, `[56,3]`=320 ms, `[56,6]`=560 ms, `[56,13]`=1.12 s.
  No retraining to move along the latency/accuracy curve; it's a config value.
- **40 language-locales**, 19 transcription-ready (English, Spanish, French, German,
  Italian, Portuguese, Japanese, Korean, …) with native punctuation and capitalisation
  and `target_lang=auto`. Keeps the multilingual + auto-detect property we'd lose with
  Kyutai.
- **6–17× more concurrent streams** than Parakeet RNNT 1.1B on one H100.
- **OpenMDW-1.1**, cleared for commercial deployment.

Recommended starting point: `att_context_size=[56,6]` (560 ms). The paper's history
ablation is worth heeding — dropping left context from 5.6 s to 1.12 s cost 1.23 points
of WER (7.28% → 8.51%), so **keep left context long** and buy latency from the right
context only.

`parakeet-unified-en-0.6b` is the alternative if we ever ship an English-only mode: it's
more accurate than Nemotron in English (1.63% LibriSpeech test-clean) and does offline
and streaming in one model. It is English-only, so it can't be the default.

---

## 4. What this ticket changed

Implemented now, on the current Parakeet stack — none of it blocks or conflicts with the
migration in §5.

**`parakeet-stt/server.py`**
- **Warmup decode at startup.** The model now decodes a throwaway 2 s clip *before*
  `/health` reports ready, forcing CUDA context creation, cuDNN autotuning and NeMo's
  decoding setup. Previously the first real utterance of the day paid all of that.
- **`timestamps=false` support.** Word/segment timing is real decode work, and the api's
  *partials* — the large majority of requests — never read the `words` array. They now
  say so and skip it. Finals and `verbose_json` are unaffected.
- **Deterministic NeMo kwarg handling.** The old blind `except TypeError` retry dropped
  *every* kwarg on failure, silently un-pinning the caller's language as a side effect of
  an unrelated signature change. Support is now probed from the signature once at load,
  narrowed permanently (and loudly) if a call really does reject it.

**`api`**
- **Direct STT route** (`API_STT_ENDPOINT`). Captions post audio straight to the model
  server instead of through LiteLLM, removing a proxy round trip *and* its
  `json → verbose_json` rewrite from every partial. Falls back to the gateway when unset,
  so split-host deploys are untouched. Cues still route through LiteLLM.
- **Adaptive VAD** (`API_STT_VAD_ADAPTIVE`, on by default). Fixes problem #4 above. The
  threshold now tracks the measured background level over a trailing window
  (minimum-statistics style) instead of being a fixed absolute gate, with `stt_silence_rms`
  demoted to a floor. A ceiling at half the recent peak level stops the threshold ever
  climbing over the speaker during uniformly loud speech. In a quiet room the measured
  floor is ~0 and behaviour is bit-for-bit what it was.

**Researched and deliberately *not* changed:**
- **CUDA-graph / label-looping TDT decoding.** The documented big decode speedup, but
  [NeMo #15164](https://github.com/NVIDIA-NeMo/NeMo/issues/15164) reports
  `use_cuda_graph_decoder: True` *slowing down* TDT inference when timestamps are on.
  Now that partials skip timestamps it may well be a win on the hot path — but it needs
  measuring on the GPU host, which this change couldn't do.
- **bf16/fp16 autocast.** NeMo documents `compute_dtype=bfloat16` for Parakeet inference,
  but the kwargs are version-dependent and the mel preprocessor is precision-sensitive.
  Untestable without the GPU; worth an experiment on the host.
- **Local attention** (`rel_pos_local_attn`) — only matters past ~24 min of audio. Our
  windows are ≤12 s.
- **Lowering `stt_silence_ms`** from 700 ms. The single biggest remaining win on
  time-to-final, but it trades directly against cutting speakers off mid-sentence, and
  picking the number needs real audio rather than a guess.
- **`parakeet-tdt-0.6b-v2`** (English-only, more accurate in English than v3). Rejected:
  it would lose Spanish and auto-detection.

---

## 5. Recommended next step

A follow-up ticket to migrate to cache-aware streaming. It is a real piece of work —
a new model server and a persistent-stream engine — but it is the change that actually
closes the gap with Conversate, and everything above is scaffolding by comparison.

1. **New model server** alongside `parakeet-stt`, serving
   `nemotron-3.5-asr-streaming-0.6b` over a **WebSocket** rather than
   `POST /audio/transcriptions`. NeMo ships the cache-aware streaming inference path
   (`speech_to_text_cache_aware_streaming_infer.py`) to build on.
2. **A streaming engine behind the existing `WhisperEngine` seam.** The seam is already
   the right shape — `StreamingTranscriber` owns windowing/VAD, the engine owns the
   model — but a true streaming engine inverts the relationship: it holds an open
   connection and emits tokens, rather than answering one decode at a time. Expect to
   split the seam rather than reuse it as-is.
3. **Retire the re-decode loop.** LocalAgreement-2 exists to paper over hypothesis churn
   from repeated whole-segment decodes. A cache-aware model's output only ever *grows*,
   so committed text is stable by construction and LocalAgreement can go.
4. **Keep the endpointing.** VAD-based turn closing is still needed for segment
   boundaries and the transcript; it just stops being on the critical path for captions.
5. **Measure before and after.** `stage.stt.partial_latency_ms` / `final_latency_ms`
   already exist in `/metrics`. Add a time-to-final measurement so we have a number
   comparable to the ~250 ms cloud figure.

Rough expectation, from the numbers in §2: partial latency bounded at the chosen chunk
size (~560 ms) instead of growing with turn length, WER back near the model's batch
number instead of ~46% worse, and GPU headroom for many more concurrent sessions.

---

## Sources

- [Even Realities — Conversate](https://www.evenrealities.com/conversate)
- [Even Support Center — Privacy Policy (Device & APP)](https://support.evenrealities.com/hc/en-us/articles/14270525749519-Privacy-Policy-Device-APP)
- [Internal Analysis: Even Realities G2 Smart Glasses Security & Privacy Investigation](https://securityboulevard.com/2026/03/internal-analysis-even-realities-g2-smart-glasses-security-privacy-investigation/)
- [`nickustinov/stt-even-g2` — real-time STT via Soniox for the G2](https://github.com/nickustinov/stt-even-g2)
- [Pushing the Limits of On-Device Streaming ASR (arXiv 2604.14493)](https://arxiv.org/html/2604.14493v1)
- [nvidia/nemotron-3.5-asr-streaming-0.6b](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)
- [Scaling Real-Time Voice Agents with Cache-Aware Streaming ASR](https://huggingface.co/blog/nvidia/nemotron-speech-asr-scaling-voice-agents)
- [nvidia/parakeet-unified-en-0.6b](https://huggingface.co/nvidia/parakeet-unified-en-0.6b)
- [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
- [NeMo #15164 — TDT inference slower with `use_cuda_graph_decoder`](https://github.com/NVIDIA-NeMo/NeMo/issues/15164)
- [Soniox — speech-to-text benchmarks](https://soniox.com/benchmarks)
- [Best Open Speech Recognition (ASR) Models in 2026 — MarkTechPost](https://www.marktechpost.com/2026/07/23/best-open-speech-recognition-asr-models-in-2026-wer-languages-latency-and-license-compared/)
