"""Chat-LLM cue generator via the LiteLLM gateway (XERK-81).

Reuses the SAME gateway base URL + key the STT engine uses (no new URL/key var):
it POSTs /chat/completions instead of /audio/transcriptions. In prod the alias is
``qwen3-llm`` → Qwen3.6-27B-FP8 on the tenir-vllm container. The model is asked to
return a small JSON object; a reasoning model (Qwen3) may wrap it, so we extract
the first JSON object defensively.

Network/model I/O, so excluded from coverage — CI runs the deterministic stub and
the session-level behaviour (rate-limit, dedupe, delivery) is covered against it.
"""

from __future__ import annotations

import json
import logging
import re

from api.contract import CueLevel
from api.cue.base import CueGenerator, GeneratedCue
from api.cue.levels import level_guidance

log = logging.getLogger("api.cue.openai")

_SYSTEM = (
    "You are a private assistant listening to a live conversation and giving the "
    "listener silent context. When something in the conversation has a useful "
    "fact behind it, surface it as a short cue only the listener sees. {guidance}\n"
    "Reply with a single JSON object and nothing else: "
    '{{"cue": true|false, "title": "1-3 word label", "body": "one or two short '
    'sentences of the actual fact/context"}}. '
    'If nothing is cue-worthy, reply {{"cue": false}}.'
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class OpenAICueGenerator(CueGenerator):
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        max_body_chars: int = 240,
        timeout: float = 20.0,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._max_body_chars = max_body_chars
        self._timeout = timeout

    def generate(  # pragma: no cover - requires httpx + a live chat endpoint
        self, transcript: str, *, level: CueLevel
    ) -> GeneratedCue | None:
        import httpx

        system = _SYSTEM.format(guidance=level_guidance(level))
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.2,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"] or ""
        except Exception:
            # A cue is a best-effort aside; never let it disturb the caption stream.
            log.warning("cue generation call failed", exc_info=True)
            return None

        return self._parse(content)

    def _parse(self, content: str) -> GeneratedCue | None:
        match = _JSON_OBJECT.search(content)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not data.get("cue"):
            return None
        title = str(data.get("title") or "").strip()
        body = str(data.get("body") or "").strip()
        if not title or not body:
            return None
        return GeneratedCue(title=title[:60], body=body[: self._max_body_chars])
