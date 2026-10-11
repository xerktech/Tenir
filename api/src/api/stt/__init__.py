"""Streaming STT seam: "stub" (model-free, CI/simulator) or "parakeet" (one HTTP
model via LiteLLM or a direct route)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from api.config import settings
from api.contract import Lang
from api.stt.base import Transcriber
from api.stt.stub import StubTranscriber

if TYPE_CHECKING:
    from api.stt.streaming import SpeechModel

__all__ = ["Transcriber", "make_transcriber", "preload_vad_model"]

log = logging.getLogger("api.stt")


def preload_vad_model() -> None:
    """Load the shared Silero model at boot, off the event loop, instead of inside the
    first session.start. A failure is logged here and again per session (energy only)."""
    if settings.stt_backend != "parakeet" or not settings.stt_vad_model:
        return
    try:
        from api.stt.vad import preload

        preload()
    except Exception:
        log.exception("Silero VAD failed to load; loud rooms fall back to the energy gate")


def _vad_model() -> SpeechModel | None:
    """A per-conversation Silero VAD, or None (energy gate only) when it is turned off
    or can't load. A missing model must cost loud-room pauses, never the captions."""
    if not settings.stt_vad_model:
        return None
    try:
        from api.stt.vad import SileroVad

        return SileroVad()
    except Exception:
        log.exception("Silero VAD failed to load; loud rooms fall back to the energy gate")
        return None


def make_transcriber(
    source_lang: Lang | None = None, *, start_offset_ms: int = 0
) -> Transcriber:
    """Factory selected by API_STT_BACKEND.

    `source_lang` (when known from session.start) constrains decoding for faster,
    more reliable recognition. `start_offset_ms` is where this transcriber's
    segment timeline begins — nonzero for a resumed conversation, so its segments
    continue the existing transcript/audio timeline instead of restarting at 0.
    """
    backend = settings.stt_backend

    if backend == "stub":
        return StubTranscriber(start_offset_ms=start_offset_ms)

    if backend == "parakeet":
        # Imported lazily so the networked deps load only when actually selected.
        from api.stt.parakeet import ParakeetEngine
        from api.stt.streaming import StreamingTranscriber

        # One offline engine drives both partials (trailing-window re-decode on a
        # cadence) and finals (whole-turn decode for the accurate stored transcript).
        offline = ParakeetEngine(
            # The caption hot path prefers a direct route to the model server and
            # falls back to the LiteLLM gateway (see Settings.stt_endpoint_url).
            endpoint=settings.stt_endpoint_url,
            model=settings.stt_model,
            api_key=settings.stt_key,
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
            vad_model=_vad_model(),
            vad_model_floor_rms=settings.stt_vad_model_floor_rms,
            start_offset_ms=start_offset_ms,
            final_words=settings.stt_final_word_timestamps,
        )
    raise ValueError(f"unknown STT backend: {backend!r} (expected 'stub' or 'parakeet')")
