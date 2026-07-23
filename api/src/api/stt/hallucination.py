"""Non-speech hallucination filter for STT output (XERK-92).

Voxtral is an instruction-tuned audio model, not a pure transcriber. Fed a window
with no intelligible speech — silence, room noise, a cough — it sometimes
*answers* the (imagined) speaker instead of transcribing, emitting a canned
conversational line like "I'm sorry", "I'm sorry, I can't understand that. Could
you please repeat or clarify what you need help with?" or "I didn't quite catch
that". Nobody said those; the model is guessing at an intent it doesn't have.
Tenir is a recorder: when there's nothing to transcribe it must surface nothing,
so the streaming layer drops these before they ever reach a caption (live band or
persisted transcript).

The unit of judgement is a **sentence**, not the whole window. Voxtral strings
these together — an apology sentence followed by a repeat/clarify sentence — so a
whole-window match misses them. Instead we split the decoded text into sentences
and suppress the window only when *every* sentence is a canned non-speech line.
That has two nice properties:

- A multi-sentence canned response ("I'm sorry, I can't understand that. Could you
  please repeat...") is dropped even though no single pattern spans it.
- A window with any genuine sentence in it is kept whole — we never eat real
  transcript to strip an apology the model tacked on.

Each sentence is matched with :func:`re.fullmatch` against a normalized form
(lowercased, apostrophes and other punctuation stripped, whitespace collapsed),
so a *longer* genuine sentence that merely shares a few words ("I couldn't find
my keys anywhere") doesn't match end to end and is treated as real speech.

Note the one deliberate aggressive call: a bare "sorry" / "I'm sorry" sentence is
treated as a canned non-speech line, because that is exactly what Voxtral emits on
silence and what the ticket calls out. A user genuinely saying only "I'm sorry"
in isolation would be dropped — an acceptable trade for a recorder whose job is to
stay silent when no one is speaking.
"""

from __future__ import annotations

import re

# Apostrophes are dropped outright (not spaced) so "couldn't" -> "couldnt" and the
# patterns need no apostrophe forms; every other non-word char becomes a separator.
_APOSTROPHES = str.maketrans("", "", "'’‘ʼ`")
_NON_WORD = re.compile(r"[^a-z0-9\s]+")
_WS = re.compile(r"\s+")
# Sentence boundaries: terminators plus newlines. Voxtral concatenates an apology
# sentence and a clarify sentence, so we judge each independently.
_SENTENCE_SPLIT = re.compile(r"[.!?…\n]+")


def _norm(text: str) -> str:
    """Fold a sentence to the comparison form: lowercased, apostrophes removed,
    other punctuation to spaces, whitespace collapsed and stripped."""
    lowered = text.lower().translate(_APOSTROPHES)
    return _WS.sub(" ", _NON_WORD.sub(" ", lowered)).strip()


# --- reusable fragments (all against the normalized, apostrophe-free form) -------
_INABILITY = r"(?:cant|couldnt|didnt|could not|did not|cannot|am unable to|was unable to)"
_VERB = r"(?:hear|catch|understand|make out|get|pick up|make sense of)"
_DEGREE = r"(?: quite| really| fully)?"
_CLARITY = r"(?: clearly| properly| very well| well)?"
_FILLER = r"(?:im |i am |well |uh |oh |hmm |ok |okay )*"  # leading throat-clearing
# Optional "(I'm) sorry, " lead, so an apology fused into one sentence with a
# request ("I'm sorry, could you repeat that?") still matches as a single sentence.
_APOLOGY_LEAD = r"(?:" + _FILLER + r"sorry(?: but)?[ ,]* )?"
# What the model asks you to repeat / clarify, or claims it couldn't make out.
_OBJECT = (
    r"(?:that|you|it|this|the audio|anything|what you said|what you meant|what you mean|"
    r"what you need|what you need help with|what you are asking(?: for)?|"
    r"what you would like(?: help with)?|your (?:request|question|message|point))"
)

# Canned non-speech *sentences* Voxtral emits. Each is anchored via re.fullmatch, so
# it only fires when it accounts for an entire sentence.
_SENTENCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        # A bare apology sentence: "sorry", "I'm sorry", "my apologies", "I apologize".
        r"(?:im |i am |well |oh |uh )*"
        r"(?:sorry(?: about that)?|my (?:apologies|apology)|i apolog(?:ize|ise))",
        # "(I'm sorry,) (I) couldn't/can't hear/understand that|you|what you said ..."
        _FILLER
        + r"(?:sorry(?: but)?[ ,]*)?(?:i )?"
        + _INABILITY
        + _DEGREE
        + " "
        + _VERB
        + " "
        + _OBJECT
        + _CLARITY,
        # "(I'm sorry,) I don't understand (that)." — with or without an explicit object.
        _FILLER + r"(?:sorry(?: but)?[ ,]*)?i (?:dont|do not) understand(?: " + _OBJECT + r")?",
        # "(I'm sorry,) Could/Can/Would you (please) repeat/say/rephrase (that) (or clarify) <object>"
        _APOLOGY_LEAD + r"(?:could|can|would|will) you(?: please| kindly)? "
        r"(?:repeat|say|rephrase|clarify)(?: that)?"
        r"(?: (?:or|and) (?:clarify|repeat|rephrase|explain))?"
        r"(?: " + _OBJECT + r")?(?: again| please| for me)*",
        # "(I'm sorry,) Please repeat/clarify/say that again/try again (or clarify) <object>"
        _APOLOGY_LEAD + r"please (?:repeat|clarify|rephrase|say that again|try again)(?: that)?"
        r"(?: (?:or|and) (?:clarify|repeat|rephrase|explain))?"
        r"(?: " + _OBJECT + r")?",
    )
)


def _is_canned_sentence(sentence: str) -> bool:
    norm = _norm(sentence)
    if not norm:
        return True  # a whitespace/punctuation-only fragment counts as "not real speech"
    return any(p.fullmatch(norm) for p in _SENTENCE_PATTERNS)


def is_non_speech_hallucination(text: str) -> bool:
    """True if the decoded window is entirely canned non-speech responses and should
    be dropped rather than shown as a caption.

    Judged per sentence: the window is suppressed only when it has at least one real
    sentence's worth of content and *every* sentence is a canned line. A window with
    any genuine sentence is kept whole, so real transcript is never eaten. Empty /
    whitespace text is not a hallucination (the caller already suppresses empties)."""
    if not _norm(text):
        return False
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if _norm(s)]
    return bool(sentences) and all(_is_canned_sentence(s) for s in sentences)
