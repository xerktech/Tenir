"""Non-speech hallucination filter for STT output (XERK-92).

Voxtral is an instruction-tuned audio model, not a pure transcriber. Fed a window
with no intelligible speech — silence, room noise, a cough — it sometimes
*answers* the (imagined) speaker instead of transcribing, emitting a canned
conversational line like "Sorry, I couldn't hear that", "I didn't quite catch
that" or "Could you please repeat or clarify your request?". Nobody said those;
the model is guessing at intent it doesn't have. Tenir is a recorder: when
there's nothing to transcribe it must surface nothing, so the streaming layer
drops these before they ever reach a caption (live band or persisted transcript).

The match is deliberately conservative. A decoded window is suppressed only when
its *entire* normalized text is one of these known non-speech responses — matched
with :func:`re.fullmatch`, so a real utterance that merely *contains* one of these
words (someone genuinely saying "could you repeat that? the line dropped") is
longer than the canned phrase, doesn't match end to end, and passes through
untouched. We would rather let a rare real "sorry, I didn't catch that" through
than eat a sentence of genuine speech.

The patterns run against a normalized form (lowercased, apostrophes and
punctuation stripped, whitespace collapsed), so only the words matter here — no
need to enumerate the comma/capitalisation variants Voxtral re-rolls each pass.
"""

from __future__ import annotations

import re

# Apostrophes are dropped outright (not spaced) so "couldn't" -> "couldnt" and the
# patterns need no apostrophe forms; every other non-word char becomes a separator.
_APOSTROPHES = str.maketrans("", "", "'’‘ʼ")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """Fold a transcript to the comparison form: lowercased, apostrophes removed,
    other punctuation to spaces, whitespace collapsed and stripped."""
    lowered = text.lower().translate(_APOSTROPHES)
    return _WS.sub(" ", _NON_WORD.sub(" ", lowered)).strip()


# Reusable fragments (all against the normalized, apostrophe-free form).
_INABILITY = r"(?:couldnt|could not|didnt|did not|cant|cannot|am unable to|was unable to)"
_VERB = r"(?:hear|catch|understand|make out|get|pick up|make sense of)"
_DEGREE = r"(?: quite| really| fully)?"
_CLARITY = r"(?: clearly| properly| very well| well)?"
_FILLER = r"(?:im |i am |well |uh |oh )*"  # leading throat-clearing Voxtral prepends
_REQ = r"(?: your (?:request|question|message))"

# Canned non-speech responses Voxtral emits, as whole-string patterns. Each is
# anchored via re.fullmatch, so it only fires when it accounts for the *entire*
# decoded window.
_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # "(I'm) sorry, (I) couldn't/didn't hear/catch/understand that|you|the audio"
        _FILLER
        + r"sorry(?: but)?(?: i)? "
        + _INABILITY
        + _DEGREE
        + " "
        + _VERB
        + r" (?:that|you|what you said|the audio|anything"
        + _REQ
        + r")"
        + _CLARITY,
        # "I didn't (quite) catch/hear/understand/get that" (no leading apology)
        _FILLER
        + r"i "
        + _INABILITY
        + _DEGREE
        + " "
        + _VERB
        + r" (?:that|you|what you said)"
        + _CLARITY,
        # "Could/Can/Would you (please) repeat/say that (or clarify your request)"
        r"(?:could|can|would|will) you(?: please| kindly)? (?:repeat|say)(?: that)?"
        + r"(?: or (?:clarify|repeat)"
        + _REQ
        + r"?)?(?: again| please)?",
        # "Could/Can/Would you (please) clarify your request (or repeat that)"
        r"(?:could|can|would|will) you(?: please| kindly)? clarify"
        + _REQ
        + r"?"
        + r"(?: or (?:repeat|clarify)(?: that)?)?(?: please)?",
        # "Please repeat/clarify/say that again (or clarify your request)"
        r"please (?:repeat|clarify|say that again|try again)(?: that)?"
        + _REQ
        + r"?"
        + r"(?: or (?:clarify|repeat)"
        + _REQ
        + r"?)?",
    )
)


def is_non_speech_hallucination(text: str) -> bool:
    """True if the whole decoded window is a canned non-speech response and should
    be dropped rather than shown as a caption. Empty/whitespace text is not treated
    as a hallucination here — the caller already suppresses empties on its own."""
    norm = _norm(text)
    if not norm:
        return False
    return any(p.fullmatch(norm) for p in _PATTERNS)
