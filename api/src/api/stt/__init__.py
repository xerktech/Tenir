"""Streaming STT seam: "stub" (model-free, CI/simulator), "voxtral" (one HTTP model
via LiteLLM or a direct route), or "hybrid" (XERK-115: cache-aware streaming partials
from Nemotron + offline finals from Parakeet)."""

from __future__ import annotations

from api.config import settings
from api.contract import Lang
from api.stt.base import Transcriber
from api.stt.stub import StubTranscriber

__all__ = ["Transcriber", "make_transcriber"]


def make_transcriber(source_lang: Lang | None = None) -> Transcriber:
    """Factory selected by API_STT_BACKEND.

    `source_lang` (when known from session.start) constrains decoding for faster,
    more reliable recognition.
    """
    backend = settings.stt_backend

    if backend == "stub":
        return StubTranscriber()

    if backend in ("voxtral", "hybrid"):
        # Imported lazily so the networked deps load only when actually selected.
        from api.stt.streaming import StreamingTranscriber
        from api.stt.voxtral import VoxtralEngine

        # Offline engine: partials (voxtral backend) AND finals (both backends). On
        # the hybrid path finals decode the whole turn here (Parakeet) for accuracy.
        offline = VoxtralEngine(
            # The caption hot path prefers a direct route to the model server and
            # falls back to the LiteLLM gateway (see Settings.stt_endpoint_url).
            endpoint=settings.stt_endpoint_url,
            model=settings.stt_model,
            api_key=settings.stt_key,
        )

        stream_engine = None
        if backend == "hybrid":
            # Live partials come from the cache-aware streaming server over a WS.
            from api.stt.streaming_engine import NemotronWsEngine

            if not settings.stt_stream_endpoint:
                raise ValueError(
                    "STT backend 'hybrid' needs stt_stream_endpoint "
                    "(API_STT_STREAM_ENDPOINT) — the WebSocket root of the "
                    "nemotron-stt server, e.g. ws://nemotron:8000"
                )
            stream_engine = NemotronWsEngine(
                endpoint=settings.stt_stream_endpoint,
                timeout=settings.stt_stream_open_timeout_ms / 1000,
            )

        return StreamingTranscriber(
            offline,
            language=source_lang.value if source_lang is not None else None,
            partial_interval_ms=settings.stt_partial_interval_ms,
            partial_window_ms=settings.stt_partial_window_ms,
            max_segment_ms=settings.stt_max_segment_ms,
            min_segment_ms=settings.stt_min_segment_ms,
            silence_ms=settings.stt_silence_ms,
            silence_rms=settings.stt_silence_rms,
            local_agreement=settings.stt_local_agreement,
            vad_adaptive=settings.stt_vad_adaptive,
            vad_noise_ratio=settings.stt_vad_noise_ratio,
            vad_window_ms=settings.stt_vad_window_ms,
            stream_engine=stream_engine,
        )
    raise ValueError(
        f"unknown STT backend: {backend!r} (expected 'stub', 'voxtral' or 'hybrid')"
    )
