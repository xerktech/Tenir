"""Screen meetings read back as sentences, not wrapped caption rows (XERK-2696)."""

from __future__ import annotations

from api.persistence import Segment
from api.screen.utterances import MAX_CUTS, MAX_GAP_MS, sentences


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
    got = sentences(_rows(("Ana", 'she said "no."'), ("Ana", "Fine")))
    assert [s.text for s in got] == ['she said "no."', "Fine"]


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


def test_rows_with_no_speaker_never_join() -> None:
    # Zoom stores every off-roster speaker as None: two None rows may be two people (QA).
    got = sentences(_rows((None, "so the plan is"), (None, "I disagree completely")))
    assert [s.text for s in got] == ["so the plan is", "I disagree completely"]


def test_a_long_pause_ends_an_unfinished_line() -> None:
    rows = [
        Segment("a", "okay let me share my screen", 0, 900, speaker="Ana"),
        Segment("b", "right after lunch we can", 900 + MAX_GAP_MS + 1, 99_000, speaker="Ana"),
        Segment("c", "start", 99_500, 99_900, speaker="Ana"),
    ]
    got = sentences(rows)
    assert _out(got) == [
        ("Ana", "okay let me share my screen", 0, 900),
        ("Ana", "right after lunch we can start", 900 + MAX_GAP_MS + 1, 99_900),
    ]


def test_a_wrap_after_an_abbreviation_joins_like_the_unwrapped_text() -> None:
    for first, second in [
        ("I talked to Dr.", "Smith about it."),
        ("bring some snacks, e.g.", "chips or nuts."),
        ("and then...", "we just stopped"),
    ]:
        assert [s.text for s in sentences(_rows(("Ana", first), ("Ana", second)))] == [
            f"{first} {second}"
        ], first
    got = sentences(_rows(("Ana", "Fine."), ("Ana", "Émile agrees.")))
    assert [s.text for s in got] == ["Fine.", "Émile agrees."]
    assert [s.text for s in sentences(_rows(("Ana", "Fine. Émile agrees.")))] == [
        "Fine.",
        "Émile agrees.",
    ]


def test_a_row_is_cut_a_bounded_number_of_times() -> None:
    # QA: "A. A. A. ..." fanned 10k stored rows out to 530k segments.
    row = " ".join(["A."] * 50)
    got = sentences(_rows(("Ana", row)))
    assert len(got) == MAX_CUTS + 1
    assert " ".join(s.text for s in got) == row


def test_an_utterance_can_fill_the_cap_exactly_but_not_pass_it() -> None:
    got = sentences(_rows(("Ana", "a" * 10), ("Ana", "b" * 9)), max_chars=20)
    assert [len(s.text) for s in got] == [20]
    got = sentences(_rows(("Ana", "a" * 10), ("Ana", "b" * 10)), max_chars=20)
    assert [len(s.text) for s in got] == [10, 10]


def test_a_pause_of_exactly_the_gap_still_joins() -> None:
    for gap, count in [(MAX_GAP_MS, 1), (MAX_GAP_MS + 1, 2)]:
        rows = [
            Segment("a", "so the plan", 0, 900, speaker="Ana"),
            Segment("b", "is this", 900 + gap, 2000 + gap, speaker="Ana"),
        ]
        assert len(sentences(rows)) == count, gap


def test_one_row_and_the_same_text_wrapped_split_alike() -> None:
    for first, second in [
        ("with Prof.", "Lee today."),
        ("(see Dr.)", "Smith now."),
        ("こんにちは。", "元気ですか？"),
        ("Done!”", "Next one."),
    ]:
        wrapped = [s.text for s in sentences(_rows(("Ana", first), ("Ana", second)))]
        whole = [s.text for s in sentences(_rows(("Ana", f"{first} {second}")))]
        assert wrapped == whole, (first, wrapped, whole)
    # Closers and repeated marks stay on their sentence (QA).
    for row, want in [
        ("「元気？」", ["「元気？」"]),
        ("他说“你好！”", ["他说“你好！”"]),
        ("本当？！", ["本当？！"]),
        ("ええ。。。そうですね", ["ええ。。。", "そうですね"]),
        ("彼は「はい。」次です。", ["彼は「はい。」", "次です。"]),
    ]:
        assert [s.text for s in sentences(_rows(("Ana", row)))] == want, row
    got = sentences(_rows(("Ana", "彼は「はい。」"), ("Ana", "Next one.")))
    assert [s.text for s in got] == ["彼は「はい。」", "Next one."]
    assert [s.text for s in sentences(_rows(("Ana", "with Prof. Lee today.")))] == [
        "with Prof. Lee today."
    ]
    assert [s.text for s in sentences(_rows(("Ana", "こんにちは。元気ですか？")))] == [
        "こんにちは。",
        "元気ですか？",
    ]
