"""Non-speech hallucination filter (XERK-92).

Voxtral, fed silence or noise, sometimes answers with a canned conversational
line instead of transcribing ("Sorry, I couldn't hear that", "Could you please
repeat or clarify your request?"). These pin that those canned lines are
recognised and dropped, while genuine speech that merely shares a few words is
never eaten — the whole point is that a recorder surfaces nothing when nobody
spoke.
"""

from __future__ import annotations

import pytest

from api.stt.hallucination import _norm, is_non_speech_hallucination


def test_norm_folds_case_apostrophes_and_punctuation() -> None:
    assert _norm("Sorry, I couldn't hear that.") == "sorry i couldnt hear that"
    assert _norm("  Could you   repeat? ") == "could you repeat"
    # Curly apostrophe folds the same as a straight one.
    assert _norm("I didn’t catch that") == "i didnt catch that"
    assert _norm("") == ""
    assert _norm("   ") == ""


# The three phrases named in the ticket must all be suppressed.
@pytest.mark.parametrize(
    "text",
    [
        "Sorry, I couldn't hear that.",
        "I didn't quite catch that.",
        "Could you please repeat or clarify your request?",
    ],
)
def test_ticket_phrases_are_suppressed(text: str) -> None:
    assert is_non_speech_hallucination(text) is True


# Close variants Voxtral rolls in practice (re-punctuation, extra filler, synonyms).
@pytest.mark.parametrize(
    "text",
    [
        "I'm sorry, I couldn't hear that",
        "Sorry, I didn't catch that.",
        "I couldn't understand that.",
        "I couldn't make out what you said.",
        "Sorry, I couldn't hear you clearly.",
        "Uh, sorry, I didn't quite get that.",
        "Could you please repeat that?",
        "Can you repeat that?",
        "Could you please clarify your request?",
        "Please repeat or clarify your request.",
        "Please repeat that.",
        "I didn't get that",
    ],
)
def test_close_variants_are_suppressed(text: str) -> None:
    assert is_non_speech_hallucination(text) is True


# Genuine speech that shares a word or two but is a real, longer utterance must
# pass through untouched — we never eat real transcript.
@pytest.mark.parametrize(
    "text",
    [
        "hello world",
        "the quick brown fox",
        "could you pass me the salt",
        "I couldn't find my keys anywhere",
        "sorry I couldn't make it to the party last night",
        "can you say that again please the line dropped",
        "I didn't catch the train this morning",
        "please repeat the experiment three times for accuracy",
        "so I went to the store and bought some milk",
        "sorry",  # a bare apology is real speech, not the canned non-speech line
        "can you hear me now",
        "could you clarify your request by tomorrow so we can plan the sprint",
    ],
)
def test_genuine_speech_passes_through(text: str) -> None:
    assert is_non_speech_hallucination(text) is False


def test_empty_and_blank_are_not_hallucinations() -> None:
    # Empties are the caller's job to suppress; the filter only claims canned lines.
    assert is_non_speech_hallucination("") is False
    assert is_non_speech_hallucination("   ") is False
