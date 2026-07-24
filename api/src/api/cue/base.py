"""Cue generation seam (XERK-81).

A cue is a private contextual info card the api derives from the live
conversation — someone asks how far the sun is and the answer appears above the
transcript, private to the listener. Generation sits behind this narrow seam so
the model-backed backend and the model-free stub are interchangeable, exactly
like the STT ``Transcriber`` seam.

The generator is *pure* per call: given the recent transcript and an
aggressiveness level it returns a cue or ``None``. It knows nothing about the
WebSocket, persistence, or rate-limiting — the session owns those (so timing and
delivery stay testable without a model).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from api.contract import CueLevel


@dataclass
class GeneratedCue:
    """A model's cue proposal, before the session assigns an id / timeline slot."""

    title: str
    body: str


def normalize_cue_title(title: str) -> str:
    """Canonical form of a cue title for de-duplication (XERK-102).

    Casefolded, punctuation dropped, whitespace collapsed — so trivial variants
    of the same cue ("Sun", "sun.", " The  Sun ") collapse to one key and don't
    slip past the dedupe as "different" titles.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", title).casefold().split())


class CueGenerator(Protocol):
    def generate(
        self,
        transcript: str,
        *,
        level: CueLevel,
        avoid_titles: Sequence[str] = (),
    ) -> GeneratedCue | None:
        """Return a cue for the given recent transcript, or ``None`` for nothing
        cue-worthy. Synchronous (may block on model I/O); the session calls it off
        the event loop via ``asyncio.to_thread``.

        ``avoid_titles`` are cues already surfaced earlier in this conversation
        (XERK-102): the generator should not repeat them, so a genuinely new cue
        spawns instead of an old one popping up again later. The session also
        drops any repeat post-hoc as a backstop, but honoring the list here lets
        a model-backed generator find *fresh* context rather than returning the
        same proposal to be discarded."""
        ...
