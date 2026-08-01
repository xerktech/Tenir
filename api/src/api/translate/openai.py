"""Chat-model translator via the LiteLLM gateway (XERK-160).

POSTs /chat/completions to the same endpoint + key the STT finals and cues use
(prod: the ``gpt-oss:120b-translate`` alias — the cue model's weights on a
dedicated route with reasoning_effort low; XERK-180). One call per finalized
non-English turn: the
turn's text goes in, an English rendering comes out as a small JSON object
(``{"translation": …}``) so a chatty model can't smuggle commentary into the
transcript. Mirrors the cue backend's defensive posture: temperature 0, thinking
disabled for the reasoning model, the first JSON object extracted from wherever
the gateway put the answer, and every failure degrades to "no translation" —
captions are never disturbed.
"""

from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("api.translate")

# Names the prompt uses for the source language, keyed by the contract's Lang
# codes. An undetected language falls back to letting the model identify it.
_LANG_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
}

_SYSTEM = """You translate live conversation into English, one utterance at a time.

The user message is one utterance{source_clause}, transcribed by speech-to-text.
Translate it into natural, faithful English. Rules:

- Translate EVERYTHING you are given; never answer, summarize, or comment on it.
- Preserve tone and register; keep names, numbers, and quoted phrases intact.
- The transcription may contain recognition errors; translate what was most
  plausibly said without inventing content.
- If the utterance is already entirely English, return it unchanged.

Reply with a JSON object, nothing else: {{"translation": "<the English text>"}}"""

# First {...} block in the response — reasoning models sometimes wrap the JSON
# in prose even when asked not to. Non-greedy so trailing chatter is ignored.
_JSON_OBJECT = re.compile(r"\{.*?\}", re.DOTALL)


class OpenAITranslator:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        disable_thinking: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._disable_thinking = disable_thinking
        self._timeout = timeout

    def _build_payload(self, text: str, source_lang: str | None = None) -> dict:
        """The /chat/completions request body. Pure (no I/O) so it's unit-tested."""
        name = _LANG_NAMES.get(source_lang or "")
        source_clause = f", spoken in {name}," if name else ""
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM.format(source_clause=source_clause)},
                {"role": "user", "content": text},
            ],
            # Greedy decoding: a translation is a faithful rendering, not prose to
            # vary — same rationale as the cue model's temperature 0.
            "temperature": 0.0,
            # Generous headroom over the input: English rarely runs longer than
            # ~2x a Romance-language source, and a turn is bounded by the STT
            # max-segment window, so this never truncates a real translation.
            "max_tokens": 1000,
            "response_format": {"type": "json_object"},
        }
        if self._disable_thinking:
            # A reasoning model left thinking burns the token budget before the
            # JSON answer (same failure mode as cues, XERK-81); a server that
            # doesn't know the kwarg drops it harmlessly.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    @staticmethod
    def _message_content(message: dict) -> str:
        """Normally ``content``; fall back to ``reasoning_content`` for a reasoning
        model/gateway that routes the answer there and leaves ``content`` empty."""
        return message.get("content") or message.get("reasoning_content") or ""

    def translate(  # pragma: no cover - requires httpx + a live chat endpoint
        self, text: str, *, source_lang: str | None = None
    ) -> str | None:
        import httpx

        if not text.strip():
            return None
        payload = self._build_payload(text, source_lang)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            content = self._message_content(resp.json()["choices"][0]["message"])
        except Exception:
            # A translation is a best-effort aside; never disturb the captions.
            log.warning("translation call failed", exc_info=True)
            return None
        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> str | None:
        match = _JSON_OBJECT.search(content)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        translation = str(data.get("translation") or "").strip()
        return translation or None
