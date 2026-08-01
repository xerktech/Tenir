"""Real music recognition via shazamio (XERK-184).

shazamio is a free, reverse-engineered client for Shazam's own recognition
backend, so it matches against Shazam's global catalog — the best-in-class
ambient recognizer, at no per-call cost and with no account. It runs in-process
(async + a Rust fingerprinter); there is no extra container. The lyrics come
from LRCLIB, keyed by the recognized artist/title.

Caveats and how they're handled (see docs/music-id.md):
- Unofficial/ToS-gray and can break if Shazam changes their backend — the
  ``off``/``stub``/``shazam`` factory keeps a paid provider a one-module drop-in.
- The Rust fingerprinter + ffmpeg decode are blocking, so recognition runs in a
  worker thread (with its own event loop) — never on the caption loop.
- A clean no-match returns ``None``; a transient failure (network, a Shazam 429)
  RAISES so callers can tell the two apart. The session's scan step catches and
  counts it (captions are never touched); the music_eval harness backs off on it
  instead of mistaking rate limiting for "no song playing" (XERK-187).

The network + fingerprint path is excluded from coverage; the response mapper
``_parse_match`` is pure and unit-tested against a canned Shazam payload.
"""

from __future__ import annotations

import logging

from api.music.base import MusicMatch
from api.music.lyrics import LrcLibLyrics, SyncedLine
from api.music.tuning import track_key

log = logging.getLogger("api.music")


def _section_metadata(track: dict, key: str) -> str | None:
    """Pull a labelled value (e.g. "Album") out of Shazam's track ``sections`` —
    a list of ``{type, metadata:[{title, text}]}`` blocks."""
    for section in track.get("sections") or []:
        for item in section.get("metadata") or []:
            if item.get("title") == key and item.get("text"):
                return str(item["text"])
    return None


def _coerce_duration_ms(track: dict) -> int | None:
    """Best-effort track length. Shazam doesn't reliably expose a duration; some
    payloads carry it under ``hub.actions``/``sections``. Absent → None, and the
    LRCLIB lookup falls back to search-by-name."""
    raw = track.get("duration") or track.get("durationMs")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    # Heuristic: a bare number under ~10000 is seconds, otherwise already ms.
    return value * 1000 if value < 10_000 else value


class ShazamMusicService:
    def __init__(
        self,
        *,
        lyrics_endpoint: str,
        min_confidence: float = 0.5,
        window_seconds: float = 8.0,
    ) -> None:
        self._min_confidence = min_confidence
        self._window_ms = int(window_seconds * 1000)
        self._lyrics = LrcLibLyrics(endpoint=lyrics_endpoint)

    @staticmethod
    def _parse_match(data: dict, *, window_ms: int) -> MusicMatch | None:
        """Map a shazamio ``recognize`` response to a MusicMatch. Pure — no I/O.

        Shazam returns ``matches:[{offset, ...}]`` and a ``track:{title, subtitle,
        …}``; ``offset`` (seconds) is the position at the START of the submitted
        window, so we add the window length to anchor on "now" (the end of the
        window). A response with no match or no track title is a no-match.
        """
        matches = data.get("matches") or []
        track = data.get("track") or {}
        title = str(track.get("title") or "").strip()
        artist = str(track.get("subtitle") or "").strip()
        if not matches or not title or not artist:
            return None
        try:
            start_offset_ms = int(round(float(matches[0].get("offset", 0.0)) * 1000))
        except (TypeError, ValueError):
            start_offset_ms = 0
        return MusicMatch(
            artist=artist,
            title=title,
            offset_ms=max(0, start_offset_ms + window_ms),
            # Shazam only returns confident matches; the presence of a resolved
            # track is the signal. Kept at 1.0 so min-confidence gating still lets
            # a paid provider that DOES score matches slot in behind this seam.
            confidence=1.0,
            duration_ms=_coerce_duration_ms(track),
            album=_section_metadata(track, "Album"),
            track_key=track_key(artist, title),
        )

    def _recognize_blocking(self, wav: bytes) -> dict | None:  # pragma: no cover - needs shazamio
        """Run shazamio in a worker thread with its own event loop. The Rust
        signature generation and ffmpeg decode block; keep them off the caption
        loop entirely."""
        import asyncio

        from shazamio import Shazam

        async def _run() -> dict:
            return await Shazam().recognize(wav)

        return asyncio.run(_run())

    async def identify(self, wav: bytes) -> MusicMatch | None:
        """Recognize the window, or ``None`` on a clean no-match. Transient
        failures propagate — every caller (the session scan, the eval harness)
        handles them, and each needs to distinguish an error from silence."""
        import asyncio

        raw = await asyncio.to_thread(self._recognize_blocking, wav)
        if not raw:
            return None
        match = self._parse_match(raw, window_ms=self._window_ms)
        if match is None or match.confidence < self._min_confidence:
            return None
        return match

    async def lyrics(self, match: MusicMatch) -> list[SyncedLine]:
        return await self._lyrics.synced_lines(
            artist=match.artist,
            title=match.title,
            duration_ms=match.duration_ms,
            album=match.album,
            cache_key=match.track_key,
        )

    async def close(self) -> None:
        await self._lyrics.close()
