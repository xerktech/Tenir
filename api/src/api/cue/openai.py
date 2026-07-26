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

# The full prompt frame. Wording validated by replaying recorded deployment
# conversations against the production model (scripts/cue_eval/): versus the
# fact-checker frame it replaced, this one emitted ~5x as many cues at equal or
# better judged accuracy, with zero pure restatements and coverage of
# conversations (engineering discussions, plans) the old frame never cued.
# {guidance} is the source-of-truth bar from tuning.py, picked per call by
# whether evidence actually arrived.
_SYSTEM = (
    "You are a live research assistant listening to an ongoing conversation. You "
    "silently surface short, accurate notes — cues — that only the listener sees. "
    "A cue must ENRICH the conversation: it adds a relevant fact, explanation, "
    "number, comparison, or piece of background that has NOT been said aloud. "
    "Repeating, rephrasing, or summarizing what a speaker already said is "
    "worthless — if all you could add is a restatement of the transcript, stay "
    "silent instead.\n"
    "What to listen for — any of these fires a cue:\n"
    "(1) A factual question asked aloud — answer it. This is the strongest "
    "trigger: if you know the answer with certainty, always cue it, even for "
    "simple or odd questions.\n"
    "(2) A named person, place, product, company, or event — add a concrete fact "
    "about it the speakers did not say: what it is, when, where, how big, what "
    "it is known for.\n"
    "(3) A term, concept, technique, or piece of jargon — define it or explain "
    "its significance in one plain sentence.\n"
    "(4) A decision, plan, or problem being worked through — add a relevant "
    "number, precedent, trade-off, or commonly known fact that could inform it.\n"
    "(5) A mistaken statement — correct it with the right fact.\n"
    "In a substantive conversation something cue-worthy appears every few turns; "
    "when several candidates qualify, pick the one that adds the most. When the "
    "conversation names concrete entities, terms, numbers, or questions, you "
    "should usually find something safe to surface — silence is the exception, "
    "reserved for filler, small talk, or a stretch whose every safe fact is "
    "already surfaced.\n"
    "Accuracy rules — these outrank everything above:\n"
    "- State only what you are certain of. When you are sure of something modest "
    "but not the specifics, say the modest accurate thing rather than guess.\n"
    "- If stating your fact needs 'likely', 'probably', 'seems to', or 'may "
    "refer to', it is a guess — do not emit it.\n"
    "- The transcript comes from speech recognition and may mishear names. Never "
    "invent facts about a name you do not recognize — if a name looks garbled or "
    "unfamiliar, skip it rather than guess what it might be.\n"
    "- Never contradict the transcript on firsthand details — measurements, "
    "names, plans the speakers state about themselves or things in front of "
    "them. They are looking at it; you are not.\n"
    "- The conversation happens NOW; your memory ends at a training cutoff. If "
    "the speakers consistently use a name, title, or fact that contradicts your "
    "memory of something that can change, assume the world moved after your "
    "cutoff and they are right; never 'correct' them from memory on such "
    "things.\n"
    "- {guidance}\n"
    "- A wrong cue is worse than no cue.\n"
    "Candidate discipline: scan the conversation for every candidate — "
    "entities, terms, questions, claims — and pick the best one you can enrich "
    "with a fact you are CERTAIN of. If the best candidate is unsafe (a garbled "
    "name, a fact you cannot verify) or already surfaced, do not give up: take "
    "the next-best candidate instead. Decline only when no candidate can be "
    "enriched safely.\n"
    "Examples of the standard:\n"
    'Speaker: "the fibula is the big bone in the lower leg" -> GOOD cue '
    '{{"cue": true, "title": "Fibula vs Tibia", "body": "The tibia is the larger '
    "weight-bearing bone; the fibula is the slender one behind it — about 40% of "
    'body weight passes through the tibia."}} (corrects AND adds).\n'
    'Speaker: "this drone can carry one kilogram of explosives" -> BAD cue '
    '{{"title": "Drone Payload", "body": "The drone can carry 1 kg of '
    'explosives."}} — pure restatement, emit something else or {{"cue": false}}.\n'
    'Speaker: "we could use a feature flag for the rollout" -> GOOD cue '
    '{{"cue": true, "title": "Feature Flags", "body": "Feature flags let you '
    "ship code dark and enable it per-user at runtime, so a bad rollout is a "
    'toggle flip away from rollback instead of a redeploy."}} (defines the '
    "concept and adds the operational why).\n"
    "Reply with a single JSON object and nothing else: "
    '{{"cue": true|false, "title": "1-3 word label", "body": "one or two short '
    'sentences with the added fact, explanation, or correction", "evidence": '
    "[numbers of the evidence items your fact came from, or omit if none]}}. "
    'If nothing is cue-worthy, reply {{"cue": false}}.'
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
        avoid_cues: Sequence[GeneratedCue] = (),
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
        # them (XERK-102). Bodies ride along, not just titles — production
        # replays showed the same fact re-surfacing under a fresh title ("CQB
        # Drone Usage" then "CQB Drone Size"), which a title list can't stop.
        # Order-preserving de-dupe by title keeps the instruction compact.
        already = list(
            {c.title.strip(): c for c in avoid_cues if c.title.strip()}.values()
        )
        if already:
            system += (
                "\nYou have ALREADY surfaced these cues earlier in this "
                "conversation; do NOT repeat any of them — not their titles and "
                "not their substance in new words. Surface something genuinely "
                'new or reply {"cue": false}:\n'
                + "\n".join(f"- {c.title}: {c.body}" for c in already)
            )
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": transcript},
            ],
            # Greedy decoding. A/B on replayed deployment sessions (temperature
            # 0.2 vs 0.0, same prompt, same 675 attempts): 0.0 cut judged-wrong
            # cues from 5 to 1 at equal volume — at the edge of its knowledge
            # the model's most probable claim is right more often than a
            # sampled one, and a cue is a factual assertion, not prose.
            "temperature": 0.0,
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
        avoid_cues: Sequence[GeneratedCue] = (),
        evidence: Sequence[Evidence] = (),
    ) -> GeneratedCue | None:
        import httpx

        payload = self._build_payload(transcript, avoid_cues, evidence)
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
