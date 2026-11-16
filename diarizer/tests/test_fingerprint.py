"""Voice fingerprints past Sortformer's 4 slots (XERK-3017)."""

from __future__ import annotations

import numpy as np

from fingerprint import Fingerprints

_rng = np.random.default_rng(0)
_VOICES = [v / np.linalg.norm(v) for v in _rng.standard_normal((8, 192))]


def _voice(i: int, noise: float = 0.6) -> np.ndarray:
    """A piece by person i: their voice plus per-piece variation (same-person cosine ~0.85,
    different people ~0, like TitaNet on clean multi-second pieces)."""
    v = _VOICES[i] + noise * _rng.standard_normal(192) / np.sqrt(192)
    return v / np.linalg.norm(v)


def test_people_are_numbered_by_first_appearance() -> None:
    fp = Fingerprints()
    assert [fp.person(s, _voice(s)) for s in (3, 1, 3, 2)] == [1, 2, 1, 3]


def test_a_fifth_person_in_a_reused_slot_gets_a_new_number() -> None:
    fp = Fingerprints()
    for s in range(4):
        assert fp.person(s + 1, _voice(s)) == s + 1
    # Sortformer folds person 5 into slot 2: the voice says it is someone new.
    assert fp.person(2, _voice(4)) == 5
    # ...and person 2, back in slot 2, is matched to their own fingerprint again.
    assert fp.person(2, _voice(1)) == 2
    # A short piece in slot 2 takes whoever the slot stands for now.
    assert fp.person(2, None) == 2


def test_a_known_person_in_another_slot_keeps_their_number() -> None:
    fp = Fingerprints()
    fp.person(1, _voice(0))
    fp.person(2, _voice(1))
    assert fp.person(3, _voice(0)) == 1  # Sortformer gave person 1 a fresh slot


def test_a_slot_first_seen_without_a_voice_is_fingerprinted_later() -> None:
    fp = Fingerprints()
    assert fp.person(1, None) == 1
    assert fp.person(2, _voice(1)) == 2
    assert fp.person(1, _voice(0)) == 1  # its first solo piece becomes its fingerprint
    assert fp.person(1, _voice(0)) == 1


def test_eight_people_through_four_slots_stay_distinct() -> None:
    fp = Fingerprints()
    seen = {}
    for rnd in range(3):
        for i in range(8):
            seen.setdefault(i, set()).add(fp.person(i % 4 + 1, _voice(i)))
    assert all(len(v) == 1 for v in seen.values())
    assert len({next(iter(v)) for v in seen.values()}) == 8


def test_a_match_folds_the_piece_into_the_fingerprint() -> None:
    """XERK-3017 QA m7: matched pieces update the person's running mean."""
    fp = Fingerprints()
    fp.person(1, _voice(0))
    fp.person(1, _voice(0))
    assert fp.counts == [2]
    fp.person(2, _voice(0))
    assert fp.counts == [3]
