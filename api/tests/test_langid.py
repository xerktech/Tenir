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

from api.stt.langid import detect_lang, leans_english

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
    ("That's", "en"),  # an English contraction is evidence (XERK-1415)
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
    # "que" scores Spanish (and French, Portuguese) and "so" scores English — a tie
    # is a guess, not a call.
    assert detect_lang("so, que") is None


def test_single_hit_needs_a_short_turn() -> None:
    # One distinctive word in a two-word turn is a call ("Los planetas.")...
    assert detect_lang("los planetas") == "es"
    # ...but one hit buried in a long proper-noun list is noise, not evidence.
    assert detect_lang("los Beatles Rolling Stones Metallica Nirvana Oasis Blur") is None


# ---- leans_english (XERK-1354) ------------------------------------------------


def test_leans_english_where_detect_lang_leaves_it_undecided() -> None:
    # English and French tie (2-2), so detect_lang won't call it and the turn would
    # inherit a Spanish run — but it is clearly not Spanish.
    text = "the chef est très and"
    assert detect_lang(text) is None
    assert leans_english(text, versus="es")


@pytest.mark.parametrize(
    ("text", "versus"),
    [
        # QA (XERK-1354): monolingual turns whose one "English" hit is a native word.
        ("Has visto a Marco ayer.", "es"),
        ("Was kostet Brot beim Bäcker?", "de"),
        ("So bene cosa vuole Marco", "it"),
        ("Mi hermano trabaja in Miami", "es"),
        ("Vamos al mall, so whatever", "es"),
        # two English hits, but just as much Spanish: the "more than the run" half
        ("Vamos al mall and the store de la esquina", "es"),
        # no evidence / run language wins / tie / not Latin
        ("Mercurio, Venus, Tierra, Marte.", "es"),
        ("el que the", "es"),
        ("the casa de", "es"),
        ("Привет", "es"),
    ],
)
def test_no_lean_without_real_english_evidence(text: str, versus: str) -> None:
    assert not leans_english(text, versus=versus)


# ---- English homographs (XERK-1415) ---------------------------------------------
# Across the recorded sessions most turns tagged non-English were plain English whose
# only "evidence" was an everyday English word that is also a distinctive word of
# another contract language — each one sent an English turn to the translator. These
# are synthetic turns of the same shapes (not transcript text).


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # "i" (it): English turns that are just "I" plus words in no vocabulary
        ("I did.", "en"),
        ("I don't remember.", "en"),
        ("I moved back.", None),
        ("Yeah, I guess.", "en"),
        # "ha" (it)
        ("Ha ha ha.", None),
        # "um" / "do" / "as" (pt)
        ("Um", None),
        ("Um, okay.", None),
        ("Do that.", "en"),
        ("What do we do?", "en"),
        ("As far as I know, it's fine.", "en"),
        # "no" (es)
        ("No.", None),
        ("No way.", None),
        ("No, it's okay.", "en"),
        # "a" (fr) / "in", "come" (it) / "yo" (es)
        ("It's a good one.", "en"),
        ("A little bit.", None),
        ("Come in.", "en"),
        ("Yo, what's up.", "en"),
    ],
)
def test_english_homographs_alone_are_not_foreign(text: str, expected: str | None) -> None:
    # None is safe: outside a run nothing is translated, inside one the turn inherits.
    assert detect_lang(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # ...but they still count once the turn has other evidence for the language.
        ("No me importa lo que pase.", "es"),
        ("Yo no sé, pero está bien.", "es"),
        ("Eu não sei o que fazer.", "pt"),
        ("Do meu lado, tudo bem.", "pt"),
        ("I ragazzi sono in giardino.", "it"),
        ("Il a dit que c'est fini.", "fr"),
        ("Die Kinder sind in der Schule.", "de"),
    ],
)
def test_english_homographs_still_corroborate(text: str, expected: str) -> None:
    assert detect_lang(text) == expected
