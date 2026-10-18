"""Cue generation seam: "off" (disabled), "stub" (model-free) or "openai" (LiteLLM chat)."""

from __future__ import annotations

from api.config import settings
from api.cue.base import CueGenerator, GeneratedCue, normalize_cue_title
from api.cue.tuning import cue_guidance, min_interval_ms

__all__ = [
    "CueGenerator",
    "GeneratedCue",
    "cue_guidance",
    "make_cue_generator",
    "make_fact_checker",
    "min_interval_ms",
    "normalize_cue_title",
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
            max_tokens=settings.cue_max_tokens,
            disable_thinking=settings.cue_disable_thinking,
        )
    raise ValueError(f"unknown cue backend: {backend!r} (expected 'off', 'stub' or 'openai')")


def make_fact_checker():
    """The encyclopedia fact check (api/cue/factcheck.py), or None when it can't or shouldn't
    run: cues not on the openai backend, no Kiwix mirror configured, or API_CUE_FACT_CHECK off."""
    if settings.cue_backend != "openai" or not settings.cue_fact_check:
        return None
    if not settings.cue_kiwix_endpoint:
        return None
    from api.cue.factcheck import FactChecker

    return FactChecker(
        endpoint=settings.litellm_endpoint,
        model=settings.llm_model,
        api_key=settings.litellm_api_key,
        kiwix_endpoint=settings.cue_kiwix_endpoint,
        kiwix_book=settings.cue_kiwix_book,
        # Under the session's wait, so a slow check stops before its model call instead of
        # calling the model after the cue was already shown.
        budget_s=max(0.5, settings.cue_fact_check_timeout_s - 0.5),
    )
