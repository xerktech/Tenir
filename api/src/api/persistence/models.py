"""Conversation persistence domain model.

A conversation is the durable record of one live session: its metadata, the
finalized transcript segments (with timing), and a pointer to the retained full
audio. The store and audio backends persist *these* objects; the Postgres schema
in ``schema.sql`` is their on-disk shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

# live: session in progress · ready: finished and persisted.
ConversationStatus = Literal["live", "ready"]


def coerce_status(value: object, *, ended: bool = False) -> ConversationStatus:
    """Map a stored status onto the current vocabulary.

    ``schema.sql`` only runs on a fresh data volume, so rows written by older
    versions outlive the code that wrote them — a database carried across an
    upgrade still holds statuses ("processing") that no longer exist. Those rows
    must not fail validation and take the whole history listing down with them
    (XERK-58), so anything unrecognized is read as finished when the conversation
    has an end time and in-progress otherwise.
    """
    if value in ("live", "ready"):
        return value  # type: ignore[return-value]
    return "ready" if ended else "live"


def utcnow() -> datetime:
    """Timezone-aware current time; conversations are stamped in UTC."""
    return datetime.now(timezone.utc)


@dataclass
class Segment:
    """One finalized transcript turn (mirrors the ``caption.final`` contract)."""

    segment_id: str
    text: str
    start_ms: int
    end_ms: int
    lang: str | None = None


@dataclass
class Cue:
    """One private contextual info card (mirrors the ``cue`` contract, XERK-81).

    A cue is derived from the conversation but is *not* part of it — private
    context for the listener. ``at_ms`` is the transcript-timeline position it
    relates to, so history renders it inline where it appeared.
    """

    cue_id: str
    title: str
    body: str
    at_ms: int


@dataclass
class Conversation:
    """A persisted conversation and its transcript."""

    id: str
    household: str
    mic_source: str | None = None
    source_lang: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None
    status: ConversationStatus = "live"
    audio_key: str | None = None
    segments: list[Segment] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)

    @property
    def transcript(self) -> str:
        """The full transcript text, one turn per line — the search corpus."""
        return "\n".join(s.text for s in self.segments if s.text)

    @property
    def duration_ms(self) -> int:
        """Wall length of the transcript, from the first to the last turn."""
        if not self.segments:
            return 0
        return max(s.end_ms for s in self.segments) - min(s.start_ms for s in self.segments)
