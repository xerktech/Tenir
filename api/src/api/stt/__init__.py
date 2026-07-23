"""Streaming STT seam: "stub" (model-free, CI/simulator) or "voxtral" (LiteLLM)."""

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

    if backend == "voxtral":
        # Imported lazily so the networked deps load only when actually selected.
        from api.stt.streaming import StreamingTranscriber
        from api.stt.voxtral import VoxtralEngine

        return StreamingTranscriber(
            VoxtralEngine(
                endpoint=settings.litellm_endpoint,
                model=settings.stt_model,
                api_key=settings.litellm_api_key,
            ),
            language=source_lang.value if source_lang is not None else None,
            partial_interval_ms=settings.stt_partial_interval_ms,
            partial_window_ms=settings.stt_partial_window_ms,
            max_segment_ms=settings.stt_max_segment_ms,
            min_segment_ms=settings.stt_min_segment_ms,
            silence_ms=settings.stt_silence_ms,
            silence_rms=settings.stt_silence_rms,
            local_agreement=settings.stt_local_agreement,
        )
    raise ValueError(f"unknown STT backend: {backend!r} (expected 'stub' or 'voxtral')")
