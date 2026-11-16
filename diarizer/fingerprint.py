"""Voice fingerprints: more speakers than Sortformer's 4 slots (XERK-3017).

Sortformer numbers at most 4 speakers; a fifth person is folded into the most similar slot.
Within a few seconds that is rarely wrong (few tables have five people talking at once), but
over a session slots get reused by different people. Each conversation therefore keeps a
fingerprint (mean TitaNet embedding) per person it has heard, and every Sortformer slot
points at the person currently using it:

- a solo piece long enough to embed is matched against every fingerprint. A clear match
  re-points its slot at that person; a voice unlike the slot's person and unlike everyone
  else is a new person (a new number); anything in between trusts Sortformer;
- other pieces (short, or overlapped and separated) take their slot's current person.

Numbers are assigned in order of first appearance, so the labels read "Speaker 1, 2, …"
with no ceiling. Pure: embeddings in, numbers out — the thresholds are tuned on
``scripts/diarize_eval`` sessions with 4, 6 and 8 speakers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

# Tuned together on scripts/diarize_eval sessions with 4, 6 and 8 speakers (RESULTS-2026-11.md):
# the one setting that lifted every many-speaker session without costing any 4-speaker one.
# Cosine to a fingerprint at or above which a piece is that person. Lower merged two people of
# a 4-speaker table into one number.
MATCH_COS = float(os.environ.get("DIAR_FP_MATCH_COS", "0.55"))
# Cosine to the slot's own person below which, with no other match, the piece is someone new.
NEW_COS = float(os.environ.get("DIAR_FP_NEW_COS", "0.30"))
# Shortest solo piece embedded: shorter TitaNet embeddings are too noisy to re-point a slot or
# found a person (same-speaker 2 s clips already overlap different-speaker ones).
MIN_EMBED_S = float(os.environ.get("DIAR_FP_MIN_S", "2.0"))


@dataclass
class Fingerprints:
    """One conversation's people. ``slot_person[k]`` is the 1-based person Sortformer slot
    ``k`` currently stands for."""

    sums: list[np.ndarray] = field(default_factory=list)  # per person, sum of unit embeddings
    counts: list[int] = field(default_factory=list)
    slot_person: dict[int, int] = field(default_factory=dict)

    def _centroid(self, p: int) -> np.ndarray:
        c = self.sums[p - 1]
        return c / (np.linalg.norm(c) + 1e-9)

    def _add(self, p: int, e: np.ndarray) -> None:
        self.sums[p - 1] = self.sums[p - 1] + e
        self.counts[p - 1] += 1

    def _new(self, e: np.ndarray) -> int:
        self.sums.append(e.copy())
        self.counts.append(1)
        return len(self.sums)

    def person(self, slot: int, embedding: np.ndarray | None) -> int:
        """The person behind a piece Sortformer gave to ``slot`` (1-based Sortformer
        speaker). ``embedding`` is the piece's voice, or None when it is too short or
        overlapped to trust."""
        current = self.slot_person.get(slot)
        if embedding is None:
            if current is None:
                # First sight of this slot, nothing to match on: a person of its own, without
                # a fingerprint until a solo piece gives one.
                current = self._placeholder()
                self.slot_person[slot] = current
            return current
        e = embedding / (np.linalg.norm(embedding) + 1e-9)
        scored = [
            (float(self._centroid(p) @ e), p)
            for p in range(1, len(self.sums) + 1)
            if self.counts[p - 1] and self.sums[p - 1].shape == e.shape
        ]
        best_cos, best = max(scored, default=(-1.0, 0))
        if current is not None and self.sums[current - 1].shape != e.shape:
            # The slot's person had no fingerprint yet: this piece is theirs, unless it
            # clearly belongs to someone already known.
            if best_cos >= MATCH_COS and best != current:
                self.slot_person[slot] = best
                self._add(best, e)
                return best
            self.sums[current - 1] = e.copy()
            self.counts[current - 1] = 1
            return current
        if best_cos >= MATCH_COS:
            self.slot_person[slot] = best
            self._add(best, e)
            return best
        cur_cos = (
            float(self._centroid(current) @ e)
            if current is not None and self.counts[current - 1]
            else -1.0
        )
        if current is None or cur_cos < NEW_COS:
            p = self._new(e)
            self.slot_person[slot] = p
            return p
        return current  # ambiguous: trust Sortformer's slot

    def _placeholder(self) -> int:
        self.sums.append(np.zeros(0))
        self.counts.append(0)
        return len(self.sums)
