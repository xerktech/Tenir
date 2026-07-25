"""Cache-aware streaming ASR server for NVIDIA Nemotron (XERK-115 follow-up).

Why this exists
---------------
The Parakeet server (`parakeet-stt/server.py`) is an *offline* model driven in a
re-decode loop: to produce a live partial the api re-decodes the whole in-flight
turn every cadence. GPU-benchmarked on the host (see `docs/stt-model-gpu-benchmark.md`)
that costs latency, not accuracy — Parakeet's *finals* stay excellent because they
re-decode the whole turn, but its *partials* are coarse (700 ms cadence) and lag as
a turn grows.

`nemotron-3.5-asr-streaming-0.6b` is a **cache-aware FastConformer-RNNT**: it encodes
each 80 ms frame exactly once and carries encoder state across chunks, so a partial
costs one chunk of compute (~20 ms, measured) regardless of turn length. That is the
right engine for the *live partial* half of the hybrid; Parakeet keeps the *final*.

This server exposes that streaming model over a **WebSocket** rather than the
one-shot `POST /audio/transcriptions` Parakeet uses, because the win is stateful:
one persistent connection per session holds the encoder cache and emits a growing
transcript as audio arrives. See `api/src/api/stt/streaming_engine.py` for the client
and `api/src/api/stt/streaming.py` (the `stream_engine` path) for how the api drives
partials from here while finalising on Parakeet.

Protocol (`/v1/audio/stream`)
-----------------------------
1. Client sends one JSON text frame: ``{"target_lang": "en-US" | "es-ES" | "auto"}``.
2. Client streams binary frames of 16 kHz s16le mono PCM (any length).
   Server replies to each with ``{"type": "partial", "text": <running turn text>}``.
3. Client may send ``{"type": "reset"}`` at a turn boundary to clear the encoder
   cache and running text for the next turn; server replies ``{"type": "reset_ok"}``.

The GPU/NeMo decode is isolated in `NemotronStreamDecoder` so the transport,
lifecycle and language handling are unit-tested with a fake decoder — the same split
`parakeet-stt` uses (NeMo and a GPU stay out of the PR gate). The exact cache-aware
streaming primitives are NVIDIA's documented path but, like Parakeet's timestamp
keys, want a first-run check on the GPU host; they degrade to an empty partial rather
than crashing the socket.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nemotron-stt")

MODEL_NAME = os.environ.get("NEMOTRON_MODEL", "nvidia/nemotron-3.5-asr-streaming-0.6b")
TARGET_SR = 16000  # the model decodes 16 kHz mono; the api already sends exactly this.

# Right-context lookahead, in 80 ms frames. att_context_size = [56, RIGHT]:
# 0=80ms, 1=160ms, 3=320ms, 6=560ms, 13=1120ms. GPU-benchmarked here, 320ms (=3) is
# the sweet spot — the EN accuracy curve is flat 320→560 ms and ES is best at 320 ms,
# so buying past it only costs latency (docs/stt-model-gpu-benchmark.md §2).
ATT_CONTEXT_LEFT = 56
ATT_CONTEXT_RIGHT = int(os.environ.get("NEMOTRON_ATT_CONTEXT_RIGHT", "3"))

app = FastAPI(title="tenir-nemotron-stt")

# One resident model on one GPU. Each WebSocket owns its own encoder-cache state
# (per-connection, cheap) but shares the model weights; NeMo decode steps aren't
# safe to run concurrently on the same model, so every step runs under this lock.
_model = None
_model_lock = threading.Lock()
_ready = threading.Event()


class NemotronStreamDecoder:  # pragma: no cover - requires NeMo + a GPU
    """Per-connection cache-aware streaming decode state.

    Holds the encoder cache and previous hypotheses for one audio stream, feeds new
    PCM through NeMo's ``conformer_stream_step`` chunk by chunk, and returns the
    running transcript. Isolated here so `_make_decoder` can be swapped for a fake in
    tests — everything above this class (transport, protocol, reset, language) is
    covered without NeMo or a GPU.
    """

    def __init__(self, model: object, *, target_lang: str, att_context_right: int) -> None:
        from nemo.collections.asr.parts.utils.streaming_utils import (  # noqa: PLC0415
            CacheAwareStreamingAudioBuffer,
        )

        self._model = model
        self._target_lang = target_lang
        model.encoder.set_default_att_context_size(  # type: ignore[attr-defined]
            att_context_size=[ATT_CONTEXT_LEFT, att_context_right]
        )
        if hasattr(model, "set_inference_prompt"):
            model.set_inference_prompt(target_lang or "auto")  # type: ignore[attr-defined]
        if hasattr(model.decoding, "set_strip_lang_tags"):  # type: ignore[attr-defined]
            # Drop the trailing "<en-US>"-style language tag the prompt model appends,
            # so the api gets clean caption text (mirrors the GPU-benchmark harness).
            model.decoding.set_strip_lang_tags(True, lang_tag_pattern=None)  # type: ignore[attr-defined]
        self._buffer_cls = CacheAwareStreamingAudioBuffer
        self.reset()

    def reset(self) -> None:
        """Clear encoder cache + running text — call at each turn boundary."""
        m = self._model
        self._cache = m.encoder.get_initial_cache_state(batch_size=1)  # type: ignore[attr-defined]
        self._prev_hyp = None
        self._pred_out = None
        self._step = 0
        self._buffer = self._buffer_cls(model=m, online_normalization=False)
        self._text = ""

    def _drop_extra_pre_encoded(self) -> int:
        if self._step == 0:
            return 0
        return self._model.encoder.streaming_cfg.drop_extra_pre_encoded  # type: ignore[attr-defined]

    def feed(self, samples: np.ndarray) -> str:
        """Append mono float32 samples, decode any newly-complete chunks, return text."""
        import torch  # noqa: PLC0415

        self._buffer.append_audio(samples, stream_id=-1)
        cache_lc, cache_lt, cache_len = self._cache
        with torch.no_grad():
            for chunk_audio, chunk_len in self._buffer:
                (
                    self._pred_out,
                    texts,
                    cache_lc,
                    cache_lt,
                    cache_len,
                    self._prev_hyp,
                ) = self._model.conformer_stream_step(  # type: ignore[attr-defined]
                    processed_signal=chunk_audio,
                    processed_signal_length=chunk_len,
                    cache_last_channel=cache_lc,
                    cache_last_time=cache_lt,
                    cache_last_channel_len=cache_len,
                    keep_all_outputs=self._buffer.is_buffer_empty(),
                    previous_hypotheses=self._prev_hyp,
                    previous_pred_out=self._pred_out,
                    drop_extra_pre_encoded=self._drop_extra_pre_encoded(),
                    return_transcription=True,
                )
                self._step += 1
                self._text = _extract_text(texts)
        self._cache = (cache_lc, cache_lt, cache_len)
        return self._text


def _extract_text(t: object) -> str:
    """Pull the transcript string out of whatever conformer_stream_step returned."""
    while isinstance(t, (list, tuple)):
        if not t:
            return ""
        t = t[0]
    return getattr(t, "text", t) or ""


def _make_decoder(*, target_lang: str) -> object:
    """Build a decode session bound to the resident model. Swapped in tests."""
    with _model_lock:
        return NemotronStreamDecoder(
            _model, target_lang=target_lang, att_context_right=ATT_CONTEXT_RIGHT
        )


# Indirection so tests can inject a fake decoder factory without a model/GPU.
make_decoder: Callable[..., object] = _make_decoder


def _load_model() -> None:  # pragma: no cover - requires NeMo + a GPU
    """Load Nemotron once at startup, off the request path, then warm the decode."""
    global _model
    import nemo.collections.asr as nemo_asr  # noqa: PLC0415 — deferred heavy import

    log.info("loading %s ...", MODEL_NAME)
    model = nemo_asr.models.ASRModel.from_pretrained(model_name=MODEL_NAME)
    model.eval()
    _model = model
    # Warm up before going ready so the first real chunk isn't the slow one (CUDA
    # context, cuDNN autotune, NeMo decode setup) — same rationale as parakeet-stt.
    try:
        dec = _make_decoder(target_lang="en-US")
        dec.feed(np.zeros(TARGET_SR, dtype=np.float32))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a failed warmup must never keep the server down
        log.warning("warmup decode failed; serving anyway", exc_info=True)
    _ready.set()
    log.info("model ready")


@app.on_event("startup")
async def _startup() -> None:  # pragma: no cover - starts a background loader thread
    threading.Thread(target=_load_model, name="model-loader", daemon=True).start()


@app.get("/health")
async def health() -> JSONResponse:
    if _ready.is_set():
        return JSONResponse({"status": "ok", "model": MODEL_NAME})
    return JSONResponse({"status": "loading", "model": MODEL_NAME}, status_code=503)


def _pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """Decode 16 kHz s16le mono PCM to float32 in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


@app.websocket("/v1/audio/stream")
async def stream(ws: WebSocket) -> None:
    import asyncio  # noqa: PLC0415

    await ws.accept()
    if not _ready.is_set():
        await ws.send_json({"type": "error", "error": "model still loading"})
        await ws.close(code=1013)  # try again later
        return

    # First frame pins the language for this stream (a locale key or "auto").
    init = await ws.receive_json()
    target_lang = (init or {}).get("target_lang") or "auto"
    decoder = make_decoder(target_lang=target_lang)
    log.info("stream open: lang=%s", target_lang)

    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            text = msg.get("text")
            if text is not None:
                # Control frame. Only "reset" (turn boundary) is defined.
                import json  # noqa: PLC0415

                ctrl = json.loads(text)
                if ctrl.get("type") == "reset":
                    decoder.reset()  # type: ignore[attr-defined]
                    await ws.send_json({"type": "reset_ok"})
                continue
            data = msg.get("bytes")
            if not data:
                continue
            samples = _pcm16_to_float32(data)
            # Decode off the event loop so a slow GPU step applies backpressure to
            # this one socket without stalling other sessions' sockets.
            try:
                running = await asyncio.to_thread(decoder.feed, samples)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — never kill the socket on a decode error
                log.warning("decode step failed", exc_info=True)
                running = ""
            await ws.send_json({"type": "partial", "text": running})
    except WebSocketDisconnect:
        pass
    finally:
        log.info("stream closed")
