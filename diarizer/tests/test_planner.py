"""Turn → speaker pieces policy (XERK-2958)."""

from __future__ import annotations

import numpy as np
import pytest

import planner
from planner import FRAME_S, Piece, assign_streams, plan_pieces, speaker_intervals


def _probs(n: int, **spans: list[tuple[float, float]]) -> np.ndarray:
    """frames x 4 activity; spans like s0=[(0.0, 1.2)] in seconds."""
    p = np.zeros((n, 4), np.float32)
    for name, lst in spans.items():
        k = int(name[1:])
        for a, b in lst:
            p[round(a / FRAME_S) : round(b / FRAME_S), k] = 0.9
    return p


def test_silence_has_no_pieces() -> None:
    assert plan_pieces(np.zeros((50, 4)), 4.0) == []


def test_one_speaker_owns_the_whole_turn() -> None:
    p = _probs(50, s1=[(0.4, 1.6), (2.0, 3.6)])
    assert plan_pieces(p, 4.0) == [Piece(2, 0.0, 4.0, False)]


def test_a_flicker_of_another_speaker_does_not_split_a_turn() -> None:
    p = _probs(50, s0=[(0.0, 3.6)], s2=[(1.0, 1.16)])  # 160 ms: below MIN_SPEECH_S
    assert plan_pieces(p, 4.0) == [Piece(1, 0.0, 4.0, False)]


def test_sequential_speakers_are_cut_mid_gap_and_cover_the_turn() -> None:
    p = _probs(50, s0=[(0.0, 1.6)], s3=[(2.4, 4.0)])
    pieces = plan_pieces(p, 4.0)
    assert [x.speaker for x in pieces] == [1, 4]
    assert pieces[0].start_s == 0.0 and pieces[-1].end_s == 4.0
    assert pieces[0].end_s == pieces[1].start_s == 2.0  # middle of the 1.6-2.4 gap
    assert not any(x.separate for x in pieces)


def test_a_too_short_turn_part_folds_into_its_neighbour() -> None:
    # 0.4 s of speaker 2 between two long stretches of speaker 1 is kept as one piece.
    p = _probs(75, s0=[(0.0, 2.4), (3.4, 6.0)], s1=[(2.6, 3.0)])
    pieces = plan_pieces(p, 6.0)
    assert pieces == [Piece(1, 0.0, 6.0, False)]


def test_overlapped_talk_is_separated_per_speaker() -> None:
    p = _probs(75, s0=[(0.0, 4.0)], s1=[(2.0, 6.0)])
    pieces = plan_pieces(p, 6.0)
    assert [(x.speaker, x.separate) for x in pieces] == [(1, True), (2, True)]
    a, b = pieces
    assert a.start_s == 0.0 and abs(a.end_s - (4.0 + planner.SEPARATE_PAD_S)) < 1e-6
    assert abs(b.start_s - (2.0 - planner.SEPARATE_PAD_S)) < 1e-6 and b.end_s == 6.0


def test_turns_that_only_touch_are_cut_not_separated() -> None:
    p = _probs(75, s0=[(0.0, 3.2)], s1=[(2.96, 6.0)])  # 240 ms shared: below MIN_OVERLAP_S
    pieces = plan_pieces(p, 6.0)
    assert [(x.speaker, x.separate) for x in pieces] == [(1, False), (2, False)]


def test_a_back_channel_inside_a_turn_is_separated() -> None:
    p = _probs(75, s0=[(0.0, 6.0)], s2=[(2.0, 2.8)])
    pieces = plan_pieces(p, 6.0)
    assert [(x.speaker, x.separate) for x in pieces] == [(1, True), (3, True)]


def test_speaker_intervals_bridge_breaths() -> None:
    p = _probs(50, s0=[(0.0, 1.0), (1.24, 2.0)])  # 240 ms breath
    assert speaker_intervals(p) == {0: [(0, 25)]}


def test_streams_follow_the_speaker_whose_activity_they_track() -> None:
    act = np.array([[1, 1, 1, 0, 0, 0], [0, 0, 1, 1, 1, 1]], float)
    env = np.array([[0.1, 0.1, 0.5, 0.9, 0.8, 0.9], [0.9, 0.8, 0.5, 0.1, 0.0, 0.1]])
    assert assign_streams(env, act) == [1, 0]
    assert assign_streams(env[::-1], act) == [0, 1]


def test_flat_streams_still_get_distinct_owners() -> None:
    act = np.array([[1, 1, 0], [0, 1, 1]], float)
    assert sorted(assign_streams(np.ones((2, 3)), act)) == [0, 1]


def test_a_solo_stretch_stops_where_an_overlap_begins() -> None:
    """QA: a lone solo interval was tiled over the whole turn, overlaps included, so every
    word of the turn went to that speaker."""
    p = _probs(100, s0=[(0.0, 3.0)], s1=[(3.5, 6.0)], s2=[(5.0, 8.0)])
    pieces = plan_pieces(p, 8.0)
    solo = [x for x in pieces if not x.separate]
    assert [(x.speaker, x.start_s) for x in solo] == [(1, 0.0)]
    assert solo[0].end_s == pytest.approx(3.28)  # mid-pause before speaker 2, not 8.0
    assert {x.speaker for x in pieces if x.separate} == {2, 3}


def test_a_solo_stretch_after_an_overlap_starts_after_it() -> None:
    p = _probs(100, s0=[(0.0, 3.0)], s1=[(2.0, 5.0)], s2=[(5.6, 8.0)])
    pieces = plan_pieces(p, 8.0)
    (solo,) = [x for x in pieces if not x.separate]
    assert solo.speaker == 3 and solo.end_s == 8.0
    assert solo.start_s == pytest.approx(5.28)  # mid-pause after speaker 2's 5.0 end
