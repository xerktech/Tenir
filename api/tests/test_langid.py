"""Language-identification fallback for finalized turns (XERK-160 regression).

The trigger case is deployment session ``a6ef5cad`` (2026-07-31): a
fully-Spanish conversation whose finals all reached the clients with
``lang: null`` — the deployed Parakeet server transcribes multilingual speech
without reporting the detected language — so live translation never fired.
The first test replays that session's actual stored transcript through the
detector; the rest pin the conservative behaviour (ambiguity → None) that
makes the fallback safe to run on every final.
"""

from __future__ import annotations

import pytest

from api.stt.langid import detect_lang

# The stored transcript of session a6ef5cad, verbatim, with the language a
# human labels each turn. The turns marked None are genuinely undecidable from
# text alone (proper-noun lists, non-Latin script, mixed fragments) — the
# detector must stay silent on them rather than guess; the Spanish turns are
# the ones whose silence caused the bug.
SESSION_A6EF5CAD = [
    ("Los planetas.", "es"),
    ("El sistema solar son.", "es"),
    ("Mercurio, Venus, Tierra, Marte.", None),  # proper nouns — no evidence
    ("Whoopi there.", "en"),
    ("Turno, urano, né?", None),  # garbled STT fragment
    ("Yeah.", None),  # bare interjection — no evidence
    ("И Плуто, первый Плутон.", None),  # Cyrillic — outside the contract langs
    ("Ya es un poco.", "es"),
    ("un planeta enano, no forma parte del sistema solar.", "es"),
    ("That totally did not translate. It just talked about planet.", "en"),
    ("That's", None),  # single fragment — no evidence
]


@pytest.mark.parametrize(("text", "expected"), SESSION_A6EF5CAD)
def test_session_a6ef5cad_turns(text: str, expected: str | None) -> None:
    assert detect_lang(text) == expected


def test_every_spanish_turn_of_the_session_is_detected() -> None:
    # The regression in one assertion: the session's substantive Spanish turns
    # must come out "es", because these exact turns went untranslated live.
    spanish = [t for t, lang in SESSION_A6EF5CAD if lang == "es"]
    assert spanish, "fixture must keep its Spanish turns"
    assert all(detect_lang(t) == "es" for t in spanish)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # One clear sentence per contract language.
        ("the meeting was moved to next week and nobody told me", "en"),
        ("el tren sale a las nueve y media de la mañana", "es"),
        ("je voudrais réserver une table pour ce soir", "fr"),
        ("das Treffen wurde auf nächsten Dienstag verschoben", "de"),
        ("eu preciso comprar as passagens antes que o preço suba", "pt"),
        ("il treno parte alle nove e mezza di sera", "it"),
    ],
)
def test_detects_each_contract_language(text: str, expected: str) -> None:
    assert detect_lang(text) == expected


def test_distinctive_characters_pin_short_turns() -> None:
    assert detect_lang("¿qué tal?") == "es"
    assert detect_lang("die Straße ist gesperrt") == "de"
    assert detect_lang("as informações estão no cartão") == "pt"


def test_empty_and_wordless_input() -> None:
    assert detect_lang("") is None
    assert detect_lang("   ") is None
    assert detect_lang("1234 !!") is None


def test_ambiguous_ties_stay_none() -> None:
    # "no" scores Spanish and "so" scores English — a tie is a guess, not a call.
    assert detect_lang("no, so") is None


def test_single_hit_needs_a_short_turn() -> None:
    # One distinctive word in a two-word turn is a call ("Los planetas.")...
    assert detect_lang("los planetas") == "es"
    # ...but one hit buried in a long proper-noun list is noise, not evidence.
    assert detect_lang("los Beatles Rolling Stones Metallica Nirvana Oasis Blur") is None
