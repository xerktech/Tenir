"""Chat-LLM cue generator via the LiteLLM gateway (XERK-81).

Reuses the SAME gateway base URL + key the STT engine uses (no new URL/key var):
it POSTs /chat/completions instead of /audio/transcriptions. In prod the alias is
``qwen3-llm`` → Qwen3.6-27B-FP8 on the tenir-vllm container.

The prod model is a *reasoning* model: left to its own devices Qwen3 spends the
token budget on a chain-of-thought it returns in ``reasoning_content`` and leaves
``content`` empty (``finish_reason: length``), so the JSON answer never arrives and
every cue is silently dropped. Cues want fast, structured output, not reasoning, so
we disable thinking (`chat_template_kwargs.enable_thinking = false`) — the JSON then
lands in ``content`` and the call finishes cleanly. We still extract the first JSON
object defensively, and fall back to ``reasoning_content`` if a gateway ever routes
the answer there instead.

The network call is excluded from coverage — CI runs the deterministic stub and the
session-level behaviour (rate-limit, dedupe, delivery) is covered against it — but
the payload builder and response parser below are pure and unit-tested.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

from api.cue.base import CueGenerator, GeneratedCue
from api.cue.retrieval.base import Evidence
from api.cue.tuning import cue_guidance

log = logging.getLogger("api.cue.openai")

_SYSTEM = (
    "You are a private fact-checker listening to a live conversation. You silently "
    "help the listener by surfacing accurate information: correcting things said in "
    "the conversation that are wrong, and adding a verified fact when a name, place, "
    "number, date, or claim is mentioned. Everything you surface must be correct — "
    "a cue only the listener sees. {guidance}\n"
    "Reply with a single JSON object and nothing else: "
    '{{"cue": true|false, "title": "1-3 word label", "body": "one or two short '
    "sentences: the correction, or the verified fact. State it plainly and only if "
    'you are confident it is accurate", "evidence": [numbers of the evidence items '
    "your fact came from, or omit if none]}}. "
    'If nothing is cue-worthy or you are not sure it is accurate, reply {{"cue": false}}.'
)

# Grounding preamble for the evidence block (XERK-120). The model's weights are
# years stale, so for anything time-sensitive the evidence must outrank memory —
# and the citation requirement is what lets the cue carry a source label the
# listener can trust.
_EVIDENCE_HEADER = (
    "\nEVIDENCE from live sources, retrieved moments ago (numbered; freshest and "
    "most reliable first):\n"
)
_EVIDENCE_RULES = (
    "\nYour built-in knowledge has a training cutoff and may be YEARS out of date. "
    "For anything involving recent events, current officeholders, prices, scores, "
    "or dates, rely on the evidence above, not memory; where evidence contradicts "
    "your memory, the evidence wins. If your cue's fact comes from the evidence, "
    'cite the item numbers you used in "evidence" — cite only items you actually '
    "used. A fact from your own knowledge (stable facts are fine from memory) "
    'omits "evidence". Never present an evidence item\'s claim as your own '
    "unverified knowledge, and never cite evidence that does not support the body."
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
        disable_thinking: bool = True,
        timeout: float = 20.0,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._max_body_chars = max_body_chars
        self._disable_thinking = disable_thinking
        self._timeout = timeout

    def _build_payload(
        self,
        transcript: str,
        avoid_titles: Sequence[str] = (),
        evidence: Sequence[Evidence] = (),
    ) -> dict:
        """The /chat/completions request body. Pure (no I/O) so it's unit-tested."""
        # The emission bar is picked by whether evidence actually arrived
        # (XERK-120): generous for evidence-covered facts when it did, the tight
        # memory bar when it didn't — so a retrieval outage degrades to the
        # conservative pre-grounding behaviour, never to aggressive guessing.
        system = _SYSTEM.format(guidance=cue_guidance(grounded=bool(evidence)))
        if evidence:
            lines = []
            for i, item in enumerate(evidence, start=1):
                dated = f", {item.published}" if item.published else ""
                lines.append(f"[{i}] ({item.source}{dated}) {item.title}: {item.snippet}")
            system += _EVIDENCE_HEADER + "\n".join(lines) + _EVIDENCE_RULES
        # Cues already surfaced this conversation: tell the model not to repeat
        # them (XERK-102), so it finds fresh context instead of re-proposing an
        # old cue that would only be discarded. Order-preserving de-dupe keeps the
        # instruction compact.
        already = list(dict.fromkeys(t.strip() for t in avoid_titles if t.strip()))
        if already:
            system += (
                "\nYou have ALREADY surfaced these cues earlier in this "
                "conversation; do NOT repeat any of them — surface something new "
                'or reply {"cue": false}: ' + ", ".join(already) + "."
            )
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": transcript},
            ],
            "temperature": 0.2,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
        }
        if self._disable_thinking:
            # Qwen3 is a reasoning model; without this it burns the whole token budget
            # thinking and returns an empty `content`. LiteLLM forwards the kwarg to
            # vLLM, which applies it to the chat template.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    @staticmethod
    def _message_content(message: dict) -> str:
        """The text to parse a cue out of: normally ``content``, but fall back to
        ``reasoning_content`` for a reasoning model/gateway that routes the answer
        there and leaves ``content`` empty (`or` also handles a ``None`` content)."""
        return message.get("content") or message.get("reasoning_content") or ""

    def generate(  # pragma: no cover - requires httpx + a live chat endpoint
        self,
        transcript: str,
        *,
        avoid_titles: Sequence[str] = (),
        evidence: Sequence[Evidence] = (),
    ) -> GeneratedCue | None:
        import httpx

        payload = self._build_payload(transcript, avoid_titles, evidence)
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            resp = httpx.post(self._url, json=payload, headers=headers, timeout=self._timeout)
            resp.raise_for_status()
            content = self._message_content(resp.json()["choices"][0]["message"])
        except Exception:
            # A cue is a best-effort aside; never let it disturb the caption stream.
            log.warning("cue generation call failed", exc_info=True)
            return None

        return self._parse(content, evidence)

    def _parse(
        self, content: str, evidence: Sequence[Evidence] = ()
    ) -> GeneratedCue | None:
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
        return GeneratedCue(
            title=title[:60],
            body=body[: self._max_body_chars],
            source=self._cited_source(data.get("evidence"), evidence),
        )

    @staticmethod
    def _cited_source(cited: object, evidence: Sequence[Evidence]) -> str | None:
        """The attribution label for the cue: the source of the first evidence item
        the model cited (XERK-120). Citations are 1-based prompt numbers; anything
        malformed or out of range is ignored — a wrong label is worse than none."""
        if not isinstance(cited, list):
            return None
        for index in cited:
            if isinstance(index, int) and 1 <= index <= len(evidence):
                return evidence[index - 1].source
        return None
