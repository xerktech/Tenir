"""Screen meetings read back as sentences, not wrapped caption rows (XERK-2696)."""

from __future__ import annotations

from api.persistence import Segment
from api.screen.utterances import sentences


def _rows(*rows):
    return [
        Segment(f"r{i}", text, i * 1000, i * 1000 + 900, lang="en", speaker=speaker)
        for i, (speaker, text) in enumerate(rows)
    ]


def _out(segs):
    return [(s.speaker, s.text, s.start_ms, s.end_ms) for s in segs]


def test_wrapped_rows_of_one_speaker_join_into_one_sentence() -> None:
    # The production rows from the ticket (Teams 86e8d7f9 / Zoom 2b487035).
    got = sentences(
        _rows(
            ("Ana", "Are we missing the cedar in test"),
            ("Ana", "environment?"),
            ("Bo", "Otherwise the prod I'll I'll be"),
            ("Bo", "going crazy prod will be."),
        )
    )
    assert _out(got) == [
        ("Ana", "Are we missing the cedar in test environment?", 0, 1900),
        ("Bo", "Otherwise the prod I'll I'll be going crazy prod will be.", 2000, 3900),
    ]
    assert [s.segment_id for s in got] == ["r0", "r2"]
    assert all(s.lang == "en" for s in got)


def test_a_speaker_change_ends_an_unfinished_line() -> None:
    got = sentences(_rows(("Ana", "so the plan is"), ("Bo", "wait")))
    assert _out(got) == [("Ana", "so the plan is", 0, 900), ("Bo", "wait", 1000, 1900)]


def test_a_sentence_end_mid_row_cuts_it_and_the_rest_starts_the_next_sentence() -> None:
    got = sentences(
        _rows(
            ("Ana", "we ship friday. Then we"),
            ("Ana", "rest. Okay? Good"),
            ("Ana", "night"),
        )
    )
    assert _out(got) == [
        ("Ana", "we ship friday.", 0, 900),
        ("Ana", "Then we rest.", 0, 1900),
        ("Ana", "Okay?", 1000, 1900),
        ("Ana", "Good night", 1000, 2900),
    ]
    ids = [s.segment_id for s in got]
    assert ids == ["r0", "r0.1", "r1.1", "r1.2"] and len(set(ids)) == len(ids)


def test_abbreviations_and_lower_case_after_a_period_do_not_cut() -> None:
    got = sentences(_rows(("Ana", "ask Dr. Smith about e.g. the"), ("Ana", "logs, v1.2 too.")))
    assert _out(got) == [("Ana", "ask Dr. Smith about e.g. the logs, v1.2 too.", 0, 1900)]


def test_closing_quotes_after_the_end_still_end_the_sentence() -> None:
    got = sentences(_rows(("Ana", 'she said "no."'), ("Ana", "fine")))
    assert [s.text for s in got] == ['she said "no."', "fine"]


def test_unpunctuated_speech_is_cut_at_the_size_cap() -> None:
    got = sentences(_rows(*[("Ana", "word " * 3)] * 10), max_chars=40)
    assert all(len(s.text) <= 40 for s in got)
    assert " ".join(s.text for s in got) == " ".join(["word word word"] * 10)


def test_rows_are_ordered_blank_rows_skipped_and_translated_rows_left_whole() -> None:
    rows = _rows(("Ana", "hello"), ("Ana", "   "), ("Ana", "there"))
    rows.append(Segment("t", "hola", 3000, 3900, lang="es", translation="hi", speaker="Ana"))
    got = sentences(list(reversed(rows)))
    assert [(s.text, s.translation) for s in got] == [("hello there", None), ("hola", "hi")]


def test_rows_are_not_mutated() -> None:
    rows = _rows(("Ana", "a"), ("Ana", "b"))
    sentences(rows)
    assert [r.text for r in rows] == ["a", "b"]
