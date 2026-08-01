"""Music service seam (XERK-184).

A music service turns a short window of the live audio into a recognized track —
artist, title, and crucially the current *offset into the song* — and supplies
that track's time-synced lyrics, which together let the client auto-scroll the
words in time. Recognition and lyrics are two steps behind one seam so the
session stays simple and the whole thing stubs as a unit for CI: the ``shazam``
service recognizes via shazamio and fetches lyrics from LRCLIB, while the
``stub`` does both deterministically with no network.

Methods are async (shazamio and the LRCLIB client are async HTTP). A no-match
returns ``None``/``[]`` and nothing is shown; a transient backend failure (a
network error, upstream rate limiting) may raise, and every caller guards it —
the session's scan step catches and counts, so a music box is an aside that can
never disturb the captions, while the eval harness backs off instead of
mistaking an error for "no song playing" (XERK-187).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from api.music.lyrics import SyncedLine


@dataclass
class MusicMatch:
    """One recognized track and where in it the audio currently is.

    ``offset_ms`` is the play position at the END of the submitted window (i.e.
    "now"), so the session can anchor it to the current session-timeline time and
    the client can scroll from there. ``track_key`` is a stable identity used to
    dedupe the same song across consecutive scans and to key the lyric cache.
    """

    artist: str
    title: str
    offset_ms: int
    confidence: float
    duration_ms: int | None = None
    album: str | None = None
    track_key: str | None = None


class MusicService(Protocol):
    async def identify(self, wav: bytes) -> MusicMatch | None:
        """Identify the track in ``wav`` (a WAV-wrapped audio window).

        Returns the match, or ``None`` when nothing recognizable is playing
        (silence, speech, an unknown track). May raise on a transient backend
        failure — callers catch and treat it distinctly from a no-match.
        """
        ...

    async def lyrics(self, match: MusicMatch) -> list[SyncedLine]:
        """Time-synced lyric lines for a recognized track, or ``[]`` when none are
        available (the client then shows the title without a scroll)."""
        ...

    async def close(self) -> None:
        """Release any held resources (HTTP clients, etc). Best-effort."""
        ...
