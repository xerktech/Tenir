"""Model-free music service for CI/dev (XERK-184).

Deterministic so tests and the model-free single-host stack exercise the whole
music path — scan loop → song/song.sync/song.done WS messages → persistence →
history — without a network call or a real fingerprinter. It "recognizes" one
canned track from any window that carries real (non-silent) audio, and serves a
canned set of synced lyrics so the box actually scrolls in the demo. The play
offset advances with wall time, so a locked song's periodic re-sync nudges the
scroll forward rather than resetting it.
"""

from __future__ import annotations

import time

from api.music.base import MusicMatch
from api.music.lyrics import SyncedLine
from api.music.tuning import track_key

_STUB_ARTIST = "Tenir Stub"
_STUB_TITLE = "Model-Free Anthem"
_STUB_DURATION_MS = 210_000  # 3:30
_STUB_CONFIDENCE = 0.99

# A canned LRC-shaped lyric set: one line every ~6s so the box visibly scrolls.
_STUB_LINES = [
    SyncedLine(at_ms=i * 6_000, text=f"Stub lyric line {i + 1}, still playing…")
    for i in range(_STUB_DURATION_MS // 6_000)
]


def _has_audio(wav: bytes) -> bool:
    """Whether the window carries real audio — any non-zero PCM byte past the
    44-byte WAV header. Pure silence (or an empty buffer) is "no song playing"."""
    return any(b != 0 for b in wav[44:])


class StubMusicService:
    def __init__(self) -> None:
        self._t0: float | None = None

    async def identify(self, wav: bytes) -> MusicMatch | None:
        if not _has_audio(wav):
            return None
        # Anchor the pretend playback to the first recognized window, then let the
        # offset advance in real time (looping within the track) so the demo scroll
        # and the re-sync both move forward.
        now = time.monotonic()
        if self._t0 is None:
            self._t0 = now
        offset_ms = int((now - self._t0) * 1000) % _STUB_DURATION_MS
        return MusicMatch(
            artist=_STUB_ARTIST,
            title=_STUB_TITLE,
            offset_ms=offset_ms,
            confidence=_STUB_CONFIDENCE,
            duration_ms=_STUB_DURATION_MS,
            album=None,
            track_key=track_key(_STUB_ARTIST, _STUB_TITLE),
        )

    async def lyrics(self, match: MusicMatch) -> list[SyncedLine]:
        return list(_STUB_LINES)

    async def close(self) -> None:
        return None
