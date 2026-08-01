# STT on a small GPU — RTX 4060 deployment + benchmark vs the RTX 6000 Pro

Follow-up to `stt-model-gpu-benchmark.md` (XERK-115). Both STT servers
(`tenir-parakeet-stt`, `tenir-nemotron-stt`) were deployed to an 8 GB RTX 4060
(TrueNAS, driver 570.x/CUDA 12.8) and benchmarked against the production
deployment on the 96 GB RTX PRO 6000 Blackwell (Windows/WSL2 Docker Desktop,
`10.10.10.22`). Everything here ran from the same client host in one sitting;
the fixes this exercise forced are in `parakeet-stt/server.py` and
`nemotron-stt/server.py` (same PR as this doc).

**TL;DR.** Both 0.6B models fit an 8 GB card together (~5.2 GB resident) once
the servers restore weights via CPU. The 4060 — a ~$300 consumer card — decodes
Parakeet finals **~5.5× faster end-to-end than the production 6000 Pro**
(median 114 ms vs 630 ms per final), with 99.6 % identical transcripts, because
production pays a **fixed ~610 ms per-request cost inside `model.transcribe()`**
that the 4060 configuration avoids. Nemotron streaming partials run at ~39 ms
per 320 ms chunk on the 4060 — comfortably real-time. Two production bugs fell
out: the deployed Nemotron server rejects every WebSocket handshake (hybrid
partials have been silently degraded to re-decode partials), and the shipped
streaming decoder produced empty/garbage partials even when transport worked.

---

## 1. Parakeet finals (the production hot path)

Method: `scripts/stt_eval/transcribe.py` over the frozen 2,465-segment eval set
(real retained session audio), serialized requests, timestamps on, language
auto — production-shaped. Same client host, same sitting, both endpoints.

| | RTX PRO 6000 (prod) | RTX 4060 |
|---|---|---|
| median wall / decode | 629.7 ms | **113.8 ms** |
| mean | 663.3 ms | 124.1 ms |
| p95 | 776.1 ms | 178.5 ms |
| RTFx (audio/wall) | 10.7 | **57.1** |
| identical hypotheses | — | 2,455 / 2,465 (99.6 %) |

The 10 differing segments each differ by 1–2 word-level edits (numeric decoder
noise, not an accuracy shift).

**The 6000's cost is flat, the 4060's scales.** Median wall by audio length:

| audio | 6000 Pro | 4060 |
|---|---|---|
| 0–5 s | 624.8 ms | 96.1 ms |
| 5–10 s | 628.1 ms | 117.0 ms |
| 10–15 s | 633.8 ms | 147.0 ms |

Server-side logs confirm the ~610 ms sits **inside `model.transcribe()`** on the
6000 (network/WSL transport is not the cause). The 4060 runs with
`PARAKEET_CUDA_GRAPHS=0` (its driver cannot run NeMo's CUDA-graph TDT decoder at
all — NVRTC emits PTX the older driver rejects, CUDA error 222), which forces
the plain decode path; the flat ~610 ms on the 6000 is consistent with the
CUDA-graph label-looping decoder re-preparing per request under `transcribe()`.
That hypothesis is unverified on the 6000 itself (needs a one-off container on
the GPU host): **try `PARAKEET_CUDA_GRAPHS=0` there** — if it reproduces the
4060's profile, production finals drop from ~630 ms to ~150 ms on existing
hardware. For this serialized one-request-at-a-time server, CUDA graphs buy
nothing that shows up in these numbers.

## 2. Nemotron streaming partials

Method: real speech utterances (12 × ~12 s) streamed over the production WS
protocol in 320 ms PCM frames; per-chunk round-trip = send → partial reply.

| | RTX PRO 6000 (prod) | RTX 4060 |
|---|---|---|
| median chunk RTT | *unmeasurable — see below* | **39.1 ms** |
| p95 / max | — | 46.5 / 63.8 ms |
| reset (turn boundary) | — | 0.5 ms |

39 ms against a 320 ms real-time budget leaves ~8× headroom; the fixed
per-chunk cost is the architectural point of the cache-aware model and it holds
on the small card. (The earlier doc's ~20 ms figure for the 6000 was measured
in-process without WS/mel overhead — not comparable.)

**Production is down.** The deployed `Tenir-Nemotron-STT` on `10.10.10.22:9403`
rejects every `/v1/audio/stream` handshake with HTTP 400 (tried `websockets`
13.1/15/17 clients; the api's own opens fail too — its logs show
`stream open failed; using re-decode partials` on live sessions). `/health`
stays 200, so nothing alerted; the hybrid path has been silently running
Parakeet-only re-decode partials. On the 4060 the same 400-everything state was
reproduced after a decode step raised (CUDA-graph capture failure) — a decode
exception poisons uvicorn's WS accept path while HTTP keeps working. Fix
shipped alongside this doc; production needs the next image.

**The shipped streaming decoder never produced valid partials.** GPU-verified
on the 4060 (the earlier benchmark explicitly could not verify this path):

- `feed()` passed `stream_id=-1` on every `append_audio`, so each 320 ms piece
  became a *new* stream in NeMo's buffer — after the first feed, nothing was
  ever decoded again.
- Worse, preprocessing each arriving piece independently corrupts the mel
  frames at every piece boundary (the STFT pads each piece separately). Even
  with stream continuity fixed, partials came out empty (en-US) or as a frozen
  wrong-language fragment (auto), on audio the same model transcribes perfectly
  offline.

The rewritten `feed()` recomputes the mel over the whole in-flight turn each
feed (milliseconds, bounded by turn length) and sends only newly complete
32-frame chunks — with the 9-frame pre-encode overlap — through the cache-aware
encoder, holding back the last 4 frames until they stabilise. Verified against
offline decodes of the same utterances: text matches.

## 3. Fitting 8 GB (deployment notes)

- NeMo's `from_pretrained` straight to GPU leaves **two** copies of the weights
  live (~5.0 GB for 0.6B fp32); restoring with `map_location="cpu"` and moving
  one copy over halves it to ~2.6 GB. Both servers now do this.
- Resident together: Parakeet 2.57 GB + Nemotron 2.66 GB ≈ 5.2 GB of 8 GB, with
  decode headroom (`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` advised).
- Minor-version compatibility mode (driver 12.8, container 12.9) breaks both
  CUDA-graph decoders — `PARAKEET_CUDA_GRAPHS=0` / `NEMOTRON_CUDA_GRAPHS=0` are
  the knobs. Costs nothing measurable here (see §1 — it may well be a *win*).

## 4. What this enables

The full STT tier runs comfortably on a consumer 8 GB card — faster on finals
than the current production GPU, real-time on partials, transcripts effectively
identical. That frees the 96 GB card to be what it's actually needed for (the
65 GB cue LLM), and turns "STT capacity" into a ~$300 line item. Concurrency is
the open question an 8 GB card doesn't answer: one household is fine; many
simultaneous sessions still favour the big card or a second small one.
