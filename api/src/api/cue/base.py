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

from dataclasses import dataclass
from typing import Protocol

from api.contract import CueLevel


@dataclass
class GeneratedCue:
    """A model's cue proposal, before the session assigns an id / timeline slot."""

    title: str
    body: str


class CueGenerator(Protocol):
    def generate(self, transcript: str, *, level: CueLevel) -> GeneratedCue | None:
        """Return a cue for the given recent transcript, or ``None`` for nothing
        cue-worthy. Synchronous (may block on model I/O); the session calls it off
        the event loop via ``asyncio.to_thread``."""
        ...
