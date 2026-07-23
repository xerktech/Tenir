"""Non-speech hallucination filter (XERK-92).

Voxtral, fed silence or noise, sometimes answers with a canned conversational
line instead of transcribing — often several sentences of it ("I'm sorry, I can't
understand that. Could you please repeat or clarify what you need help with?").
These pin that such windows are recognised per sentence and dropped, while genuine
speech that merely shares a few words is never eaten — a recorder surfaces nothing
when nobody spoke.
"""

from __future__ import annotations

import pytest

from api.stt.hallucination import _is_canned_sentence, _norm, is_non_speech_hallucination


def test_norm_folds_case_apostrophes_and_punctuation() -> None:
    assert _norm("Sorry, I couldn't hear that.") == "sorry i couldnt hear that"
    assert _norm("  Could you   repeat? ") == "could you repeat"
    # Curly apostrophe folds the same as a straight one.
    assert _norm("I didn’t catch that") == "i didnt catch that"
    assert _norm("") == ""
    assert _norm("   ") == ""


# The exact lines the user reported still coming through (XERK-92 follow-up): a bare
# apology, and a multi-sentence apology + clarify request.
@pytest.mark.parametrize(
    "text",
    [
        "I'm sorry",
        "I'm sorry, I can't understand that. Could you please repeat or clarify what you need help with?",
    ],
)
def test_user_reported_lines_are_suppressed(text: str) -> None:
    assert is_non_speech_hallucination(text) is True


# The three phrases named in the original ticket must all still be suppressed.
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


# Close variants Voxtral rolls in practice (re-punctuation, filler, synonyms,
# multi-sentence, apology fused into the request).
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
        "Could you please rephrase your question?",
        "Please repeat or clarify your request.",
        "Please repeat that.",
        "I didn't get that",
        "Sorry.",
        "My apologies.",
        "I apologize.",
        "I'm sorry, I don't understand.",
        "I don't understand what you mean.",
        "Sorry, I didn't catch that. Could you say that again please?",
        "I'm sorry, could you repeat that for me?",
    ],
)
def test_close_variants_are_suppressed(text: str) -> None:
    assert is_non_speech_hallucination(text) is True


# Genuine speech — even when it shares a word or two with a canned line, or contains
# a real apology inside a real sentence — must pass through untouched.
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
        "can you hear me now",
        "I couldn't understand that equation so I asked the teacher",
        "I'm sorry I'm late, traffic was terrible this morning",
        "could you clarify your request by tomorrow so we can plan the sprint",
        "I don't understand why the build keeps failing on CI",
    ],
)
def test_genuine_speech_passes_through(text: str) -> None:
    assert is_non_speech_hallucination(text) is False


def test_a_real_sentence_mixed_in_keeps_the_whole_window() -> None:
    """Judged per sentence: one genuine sentence means the window is real speech and
    is kept whole — we don't eat transcript to strip a tacked-on apology."""
    text = "The meeting is at three. Sorry, I couldn't hear that."
    assert is_non_speech_hallucination(text) is False


def test_bare_apology_sentence_is_canned_but_a_bare_apology_is_dropped() -> None:
    # A bare "sorry" / "I'm sorry" is treated as a canned non-speech line (the ticket
    # calls it out explicitly), but only when it is the whole window.
    assert _is_canned_sentence("I'm sorry") is True
    assert is_non_speech_hallucination("I'm sorry") is True


def test_empty_and_blank_are_not_hallucinations() -> None:
    # Empties are the caller's job to suppress; the filter only claims canned lines.
    assert is_non_speech_hallucination("") is False
    assert is_non_speech_hallucination("   ") is False
    assert is_non_speech_hallucination("...") is False
    # A fragment that normalizes to nothing isn't "real speech" for the all-canned test.
    assert _is_canned_sentence("...") is True
    assert _is_canned_sentence("   ") is True
