"""Cue generation seam: "off" (disabled), "stub" (model-free) or "openai" (LiteLLM chat)."""

from __future__ import annotations

from api.config import settings
from api.cue.base import CueGenerator, GeneratedCue
from api.cue.levels import level_guidance, min_interval_ms

__all__ = [
    "CueGenerator",
    "GeneratedCue",
    "make_cue_generator",
    "level_guidance",
    "min_interval_ms",
]


def make_cue_generator() -> CueGenerator | None:
    """Factory selected by API_CUE_BACKEND. Returns ``None`` when cues are off, so
    the session skips all cue work with no import/CPU cost on the stripped core."""
    backend = settings.cue_backend

    if backend == "off":
        return None

    if backend == "stub":
        from api.cue.stub import StubCueGenerator

        return StubCueGenerator()

    if backend == "openai":
        # Imported lazily so httpx loads only when the real backend is selected.
        from api.cue.openai import OpenAICueGenerator

        return OpenAICueGenerator(
            endpoint=settings.litellm_endpoint,
            model=settings.llm_model,
            api_key=settings.litellm_api_key,
            max_body_chars=settings.cue_max_body_chars,
            disable_thinking=settings.cue_disable_thinking,
        )
    raise ValueError(f"unknown cue backend: {backend!r} (expected 'off', 'stub' or 'openai')")
