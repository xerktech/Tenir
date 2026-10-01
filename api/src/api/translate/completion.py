"""Completion-prompt translator for dedicated MT models (XERK-1354).

Dedicated translation models such as MiLMMT-46 are trained on a bare completion
prompt, not chat: under the shipped system prompt + JSON envelope they ramble to
the token cap (scripts/translation_eval/RESULTS-2026-10.md). This backend sends
their documented format to ``/completions`` through the same LiteLLM gateway:

    Translate this from Spanish to English:
    Spanish: <turn text>
    English:

greedy, stopping at the first newline. The output is the translation itself.

Unlike the chat prompt, this one must name the source language. A turn with no
detected language (an inherited run continuation) is translated from the run's
language, unless it leans English: told English text is Spanish, the model
rewrites it rather than returning it, so those turns are skipped.
"""

from __future__ import annotations

import logging

from api.stt.langid import leans_english
from api.translate.openai import _LANG_NAMES

log = logging.getLogger("api.translate")


class CompletionTranslator:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 20.0,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def _build_payload(
        self, text: str, source_lang: str | None = None, run_lang: str | None = None
    ) -> dict | None:
        """The /completions request body, or None when no call should be made: no
        nameable source language, or an inherited turn that leans English. Pure (no
        I/O) so it's unit-tested."""
        lang = source_lang or run_lang
        name = _LANG_NAMES.get(lang or "")
        if name is None or lang == "en":
            return None
        if source_lang is None and leans_english(text, versus=lang):
            return None
        # One line in, one line out: the stop sequence is the newline after "English:".
        line = " ".join(text.split())
        return {
            "model": self._model,
            "prompt": f"Translate this from {name} to English:\n{name}: {line}\nEnglish:",
            "temperature": 0.0,
            # A turn is bounded by the STT max-segment window; this is headroom, not
            # a limit a real translation reaches.
            "max_tokens": 512,
            "stop": ["\n"],
        }

    @staticmethod
    def _parse(body: dict) -> str | None:
        try:
            out = str(body["choices"][0]["text"]).strip()
        except (KeyError, IndexError, TypeError):
            return None
        return out or None

    def translate(  # pragma: no cover - requires httpx + a live completion endpoint
        self, text: str, *, source_lang: str | None = None, run_lang: str | None = None
    ) -> str | None:
        import httpx

        if not text.strip():
            return None
        payload = self._build_payload(text, source_lang, run_lang)
        if payload is None:
            return None
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            return self._parse(resp.json())
        except Exception:
            # A translation is a best-effort aside; never disturb the captions.
            log.warning("translation call failed", exc_info=True)
            return None
