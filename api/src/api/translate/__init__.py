"""Translation seam: "off" (disabled), "stub" (model-free) or "openai" (LiteLLM chat)."""

from __future__ import annotations

from api.config import settings
from api.translate.base import Translator

__all__ = [
    "Translator",
    "make_translator",
]


def make_translator() -> Translator | None:
    """Factory selected by API_TRANSLATION_BACKEND. Returns ``None`` when
    translations are off, so the session skips all translation work."""
    backend = settings.translation_backend

    if backend == "off":
        return None

    if backend == "stub":
        from api.translate.stub import StubTranslator

        return StubTranslator()

    if backend == "openai":
        # Imported lazily so httpx loads only when the real backend is selected.
        from api.translate.openai import OpenAITranslator

        return OpenAITranslator(
            endpoint=settings.litellm_endpoint,
            model=settings.translation_model,
            api_key=settings.litellm_api_key,
            disable_thinking=settings.cue_disable_thinking,
        )
    raise ValueError(
        f"unknown translation backend: {backend!r} (expected 'off', 'stub' or 'openai')"
    )
