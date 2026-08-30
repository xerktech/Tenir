"""Chat-LLM cue generator via the LiteLLM gateway (XERK-81).

Reuses the SAME gateway base URL + key the STT engine uses (no new URL/key var):
it POSTs /chat/completions instead of /audio/transcriptions. In prod the alias is
``qwen3.8-27b-dflash`` → Qwen3.8-27B on the SGLang server (NVFP4 weights, DFlash
speculative decoding), which replaced the retired gpt-oss:120b Ollama deployment;
the July 2026 cue-model eval that picked gpt-oss is in
scripts/cue_eval/RESULTS-2026-07.md.

The prod model is a *reasoning* model, and the August 2026 replay retune
(scripts/cue_eval/RESULTS-2026-08.md) found the fix was MORE reasoning, not less:
cues run with thinking ON by default
(``chat_template_kwargs.enable_thinking = true``, toggle
``API_CUE_DISABLE_THINKING``) against a 2048-token budget — replay-measured ahead
of thinking-off on both volume and judged accuracy (159 vs 64 cues on the frozen
set; judged accuracy 1.97 vs 1.81). Clean single-request latency is ~6s p50 /
~10s p90, so the 30s call timeout leaves headroom for concurrent sessions; a
missed cue degrades to a skipped card, never a stalled caption. The toggle is
sent explicitly in both directions so the outcome never depends on the server's
own default; a server that doesn't know the kwarg drops it harmlessly (LiteLLM's
drop_params). We still extract the first JSON object defensively, and fall back
to ``reasoning_content`` if a gateway ever routes the answer there instead.

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
# conversations against the production model (scripts/cue_eval/). The July 2026
# enrichment frame (RESULTS-2026-07.md) was tuned on gpt-oss-120b and, on the
# Qwen3.8-27B replacement, under-emitted badly (6 cues on the frozen set with
# thinking off, 26 with it on). The August 2026 retune (RESULTS-2026-08.md)
# replaced it with this short, emission-first frame ("v5"): on Qwen3.8-27B with
# thinking on and a 2048-token budget it replayed 159 cues on the same frozen
# set at judged novelty 1.90 / relevance 1.96 / accuracy 1.97 — ahead of the
# gpt-oss-120b baseline of 150 cues at accuracy 1.99. The worked examples below
# the frame are kept from the July calibration (they carry the mishearing,
# wrong-referent, and cross-generation traps, and v5 was measured WITH them);
# the per-case restraint prose dropped with the frame — the shorter prompt
# measured fewer wrong cues, not more.
# {guidance} is the time-varying-facts bar from tuning.py, picked per call by
# whether evidence actually arrived.
_SYSTEM = (
    "You are a live research assistant. You listen to an ongoing conversation "
    "and silently surface short, accurate cues — private notes only the "
    "listener sees. A cue ADDS information the speakers did not say aloud. "
    "Repeating or summarizing what they said is worthless, so if you have "
    "nothing new, reply {{\"cue\": false}}.\n"
    "You should surface a cue on most turns of a substantive conversation. "
    "Fire when you can add ANY of the following, picking the best candidate "
    "from the newest turns:\n"
    "1) A factual question was asked and you know the answer with certainty.\n"
    "2) A person, place, product, or event is being engaged with and you can "
    "add a concrete fact the speakers did not mention.\n"
    "3) A technical term or piece of jargon appears that these listeners may "
    "not know — define it in one plain sentence.\n"
    "4) A decision or problem is being worked through and a relevant number, "
    "precedent, or trade-off would inform it.\n"
    "5) A claim is clearly wrong and you know the correct fact.\n"
    "Accuracy is absolute:\n"
    "- State only what you are certain of; if unsure, stay silent.\n"
    "- Never invent facts about a name you do not recognize or that sounds "
    "garbled.\n"
    "- Never contradict a firsthand detail the speakers stated about something "
    "they are looking at.\n"
    "- {guidance}\n"
    "- Never present a sibling model's specs, a predecessor's dates, or a "
    "rival product's defaults as the named thing's own.\n"
    "Reply with a single JSON object and nothing else: "
    '{{"cue": true, "title": "1-3 word label", "body": "one or two short '
    'sentences under 200 characters"}}. If nothing is cue-worthy, reply '
    '{{"cue": false}}.\n'
    "\n"
    "Examples of the standard:\n"
    'Speaker: "the fibula is the big bone in the lower leg" -> GOOD cue '
    '{{"cue": true, "title": "Fibula vs Tibia", "body": "The tibia is the larger '
    "weight-bearing bone; the fibula is the slender one behind it — about 40% of "
    'body weight passes through the tibia."}} (corrects AND adds).\n'
    'Speaker: "this drone can carry one kilogram of explosives" -> BAD cue '
    '{{"title": "Drone Payload", "body": "The drone can carry 1 kg of '
    'explosives."}} — pure restatement, emit something else or {{"cue": false}}.\n'
    'Speaker (family talk): "the doctor thinks it\'s plantar fasciitis" -> '
    'GOOD cue {{"cue": true, "title": "Plantar Fasciitis", "body": "Plantar '
    "fasciitis is inflammation of the tissue along the sole of the foot — the "
    "most common cause of heel pain, and it usually resolves without "
    'surgery."}} (a term from outside the speakers\' own field, defined once).\n'
    'Engineer (standup): "I\'ll open a PR once the pipeline is green" -> BAD '
    'cue {{"title": "Pull Request", "body": "A pull request is a way of '
    'proposing code changes for review..."}} — that is this room\'s native '
    "vocabulary; defining their own tools to practitioners adds nothing. "
    'Reply {{"cue": false}}.\n'
    'Speaker (standup): "Jonathan will demo the RPM changes after lunch" -> '
    'BAD cue {{"title": "RPM", "body": "RPM stands for Red Hat Package '
    'Manager..."}} — Jonathan is their coworker and RPM is the name of THEIR '
    "system; a famous person or product sharing the name is the wrong "
    'referent. Reply {{"cue": false}}.\n'
    'Speaker (ordering dessert): "I\'m gonna get a large cheesecake" -> BAD cue '
    '{{"title": "Cheesecake Origin", "body": "Cheesecake dates back to ancient '
    'Greece..."}} — trivia about an everyday food nobody asked about; the '
    'listener is buying dessert, not researching it. Reply {{"cue": false}}.\n'
    'Speaker (garbled): "Play the missus. We gonna Bentley out girl. What?" -> '
    'BAD cue {{"title": "Bentley", "body": "Bentley is a British luxury car '
    'maker..."}} — a brand token inside incoherent speech; nothing here is '
    "about cars. The same for any recognizable place, show, or person "
    'surfacing once in fragmented speech: "Pompeii drive" mumbled between '
    "half-sentences of screen-troubleshooting is not an invitation to cue "
    'the Roman city. Reply {{"cue": false}}.\n'
    'Speakers have spent minutes debugging Grafana access; a garbled turn says '
    '"gravano?" -> BAD cue {{"title": "Salvatore Gravano", "body": "Salvatore '
    '\'Sammy the Bull\' Gravano was a Gambino family underboss..."}} — in this '
    "conversation that sound is Grafana misheard, not a mobster. Reply "
    '{{"cue": false}}.\n'
    'Speaker: "I can\'t open that link you sent" -> BAD cue {{"title": "Link '
    'Access", "body": "I can\'t open or view external links."}} — the cue '
    'spoke as a participant; a cue is a note about the world, never an "I".\n'
    'Speaker: "reviewing the Pixel 12 Pro today" and the newest Pixel you know '
    'is the 9 -> BAD cue {{"title": "Pixel 12 Pro", "body": "The Pixel 12 Pro '
    'has a 6.7-inch display and a Tensor G4 chip..."}} — those are an older '
    "model's specs with the new name pasted on; every detail of a "
    "product newer than your knowledge is unknown to you. Cue a different "
    'topic or reply {{"cue": false}}.\n'
    "Reply with a single JSON object and nothing else: "
    '{{"cue": true|false, "title": "1-3 word label", "body": "one or two short '
    "sentences — under 200 characters — with the added fact, explanation, or "
    'correction", "evidence": '
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
    "unverified knowledge, and never cite evidence that does not support the body. "
    "Evidence about a DIFFERENT model, generation, or version than the one the "
    "speakers named does not cover the named one — an article about a "
    "predecessor product answers nothing about its successor. More broadly, "
    "evidence is usable only when it is about the very subject the speakers "
    "are discussing: an item that merely shares a word, phrase, figure, or "
    "date with the conversation — a price change for a different product, a "
    "poll from a different country, news about a different organization — is "
    "about something ELSE. Do not cue it, and above all never use such "
    "evidence to 'correct' the speakers about their own subject. Evidence "
    "cannot rescue a mishearing either: an article about a name proves the "
    "name exists, not that the speakers said it — when a word is garbled or "
    "sounds like something else already in the conversation, retrieved "
    "material about the stray reading does not make it the topic."
)

_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _clip_at_word(text: str, limit: int) -> str:
    """Clip overlong model output at a word boundary with an ellipsis.

    The cue body renders on a glasses card — a hard character slice ended
    mid-word on 8 of 51 cues in a reviewed production session ("…vascular
    trend monito"). Clipping back to the last full word costs a few
    characters and reads as an intentional continuation instead of a bug.
    """
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]  # reserve one char for the ellipsis
    head, _, _ = cut.rpartition(" ")
    return (head or cut).rstrip(" ,;:([—–-") + "…"


class OpenAICueGenerator(CueGenerator):
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str = "",
        max_body_chars: int = 240,
        max_tokens: int = 2048,
        disable_thinking: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._max_body_chars = max_body_chars
        self._max_tokens = max_tokens
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
                "conversation; do NOT repeat any of them — not their titles, "
                "not their substance in new words, and not the same subject "
                "from a different angle: a definition, a mechanism, and a "
                "piece of history about one thing are all the SAME cue, and "
                "so are different names for one idea — a pattern, its "
                "synonym, and a protocol or product embodying it. If "
                "your best candidate overlaps anything below, pick a "
                'different subject entirely or reply {"cue": false}:\n'
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
            # 2048, not 600: with thinking on (the default) the model reasons
            # inside the same budget before the JSON answer, and 600 measurably
            # starved it — finish_reason: length, EMPTY content, a silently
            # dropped cue (scripts/cue_eval/RESULTS-2026-08.md). The body is
            # still clipped to max_body_chars at parse, so the extra budget
            # costs latency only when the reasoning actually uses it.
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        # Thinking ON by default — the replay-measured winner for Qwen3.8-27B
        # (RESULTS-2026-08.md). Sent explicitly in both directions so the
        # outcome never depends on the server's own default; a server that
        # doesn't know the kwarg drops it (LiteLLM's drop_params).
        payload["chat_template_kwargs"] = {"enable_thinking": not self._disable_thinking}
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
        # A placeholder answer ({"cue": true, "title": "...", "body": "..."}) is
        # a decline the model phrased as an acceptance (seen once in the v5think
        # replay): dots are not cue content, so require at least one alphanumeric
        # in each field, any script.
        if not any(ch.isalnum() for ch in title) or not any(ch.isalnum() for ch in body):
            return None
        return GeneratedCue(
            title=title[:60],
            body=_clip_at_word(body, self._max_body_chars),
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
