"""Music ID seam (XERK-184): "off" (disabled), "stub" (model-free) or "shazam"
(real recognition via shazamio + synced lyrics via LRCLIB)."""

from __future__ import annotations

from api.config import settings
from api.music.base import MusicMatch, MusicService
from api.music.lyrics import SyncedLine, parse_lrc
from api.music.tuning import scan_backoff_ms, track_key, window_bytes

__all__ = [
    "MusicMatch",
    "MusicService",
    "SyncedLine",
    "make_music_service",
    "parse_lrc",
    "scan_backoff_ms",
    "track_key",
    "window_bytes",
]


def make_music_service() -> MusicService | None:
    """Factory selected by API_MUSIC_BACKEND. Returns ``None`` when music ID is
    off (the default), so the session does no music work at all."""
    backend = settings.music_backend

    if backend == "off":
        return None

    if backend == "stub":
        from api.music.stub import StubMusicService

        return StubMusicService()

    if backend == "shazam":
        # Imported lazily so shazamio/httpx load only when the real backend runs.
        from api.music.shazam import ShazamMusicService

        return ShazamMusicService(
            lyrics_endpoint=settings.music_lyrics_endpoint,
            min_confidence=settings.music_min_confidence,
            window_seconds=settings.music_window_seconds,
            identify_timeout=settings.music_identify_timeout_ms / 1000,
        )
    raise ValueError(
        f"unknown music backend: {backend!r} (expected 'off', 'stub' or 'shazam')"
    )
