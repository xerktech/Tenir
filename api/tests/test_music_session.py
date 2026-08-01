"""Session-level Music ID behaviour (XERK-184): a fingerprinted window opens a
`song` run with synced lyrics, the same song re-syncs, a hold with no match ends
the run with `song.done`, a different song replaces the last, cues stand aside
while a song plays, and the scan loop drains on close — all against a spy service
(no network, no real fingerprinter)."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.config import settings
from api.contract import MicSource, ServerMessage, Song, SongDone, SongSync
from api.cue.base import GeneratedCue
from api.music.base import MusicMatch
from api.music.lyrics import SyncedLine
from api.persistence import get_conversation_store
from api.session import Session


class _SpyMusicService:
    """Controllable music service: returns queued matches, canned lyrics, records
    calls, and notes when it was closed."""

    def __init__(
        self, matches: list[MusicMatch | None], lyrics: list[SyncedLine] | None = None
    ) -> None:
        self._matches = list(matches)
        self._lyrics = lyrics if lyrics is not None else [
            SyncedLine(at_ms=0, text="one"),
            SyncedLine(at_ms=5000, text="two"),
        ]
        self.identify_calls = 0
        self.closed = False

    async def identify(self, wav: bytes) -> MusicMatch | None:
        self.identify_calls += 1
        return self._matches.pop(0) if self._matches else None

    async def lyrics(self, match: MusicMatch) -> list[SyncedLine]:
        return list(self._lyrics)

    async def close(self) -> None:
        self.closed = True


def _match(
    *, artist: str = "Radiohead", title: str = "Weird Fishes", offset_ms: int = 60000
) -> MusicMatch:
    from api.music.tuning import track_key

    return MusicMatch(
        artist=artist,
        title=title,
        offset_ms=offset_ms,
        confidence=1.0,
        duration_ms=318000,
        track_key=track_key(artist, title),
    )


async def _fresh_session(sent: list[ServerMessage]) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
    return session


def _arm_window(session: Session) -> None:
    """Give the session a full, non-silent scan window so `_scan_music_once`
    actually fingerprints instead of waiting for audio."""
    session._music_window_bytes = 160_000
    session._music_audio = bytearray(b"\x01\x02" * 80_000)


def _songs(sent: list[ServerMessage]) -> list[Song]:
    return [m for m in sent if isinstance(m, Song)]


def _syncs(sent: list[ServerMessage]) -> list[SongSync]:
    return [m for m in sent if isinstance(m, SongSync)]


def _dones(sent: list[ServerMessage]) -> list[SongDone]:
    return [m for m in sent if isinstance(m, SongDone)]


def test_song_emitted_and_persisted() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._music = _SpyMusicService([_match()])
        _arm_window(session)
        await session._scan_music_once()

        songs = _songs(sent)
        assert len(songs) == 1
        song = songs[0]
        assert song.artist == "Radiohead"
        assert song.title == "Weird Fishes"
        assert song.offsetMs == 60000
        assert song.durationMs == 318000
        assert [(ln.atMs, ln.text) for ln in song.lines] == [(0, "one"), (5000, "two")]
        assert session._music_active is True

        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None
        assert [(s.artist, s.title, s.duration_ms) for s in conv.songs] == [
            ("Radiohead", "Weird Fishes", 318000)
        ]
        await session.close()

    asyncio.run(run())


def test_same_song_resyncs() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._music = _SpyMusicService([_match(offset_ms=60000), _match(offset_ms=78000)])
        _arm_window(session)
        await session._scan_music_once()  # opens the run
        await session._scan_music_once()  # same song -> re-sync

        songs, syncs = _songs(sent), _syncs(sent)
        assert len(songs) == 1
        assert len(syncs) == 1
        # Re-sync keys the same run and carries the fresh offset.
        assert syncs[0].songId == songs[0].songId
        assert syncs[0].offsetMs == 78000
        await session.close()

    asyncio.run(run())


def test_hold_expiry_ends_run() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        # One match, then nothing.
        session._music = _SpyMusicService([_match(), None])
        _arm_window(session)
        await session._scan_music_once()  # opens
        assert session._music_active is True
        # Age the last match past the hold window, then a no-match scan closes it.
        session._music_last_match_monotonic = time.monotonic() - (
            settings.music_hold_ms / 1000 + 1
        )
        await session._scan_music_once()

        dones = _dones(sent)
        assert len(dones) == 1
        assert dones[0].songId == _songs(sent)[0].songId
        assert session._music_active is False
        await session.close()

    asyncio.run(run())


def test_hold_not_yet_expired_keeps_run() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._music = _SpyMusicService([_match(), None])
        _arm_window(session)
        await session._scan_music_once()
        # A single miss well within the hold window does not end the run.
        await session._scan_music_once()
        assert _dones(sent) == []
        assert session._music_active is True
        await session.close()

    asyncio.run(run())


def test_different_song_replaces() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._music = _SpyMusicService(
            [_match(title="Weird Fishes"), _match(artist="Muse", title="Starlight")]
        )
        _arm_window(session)
        await session._scan_music_once()  # song A
        await session._scan_music_once()  # song B replaces A

        songs, dones = _songs(sent), _dones(sent)
        assert [s.title for s in songs] == ["Weird Fishes", "Starlight"]
        # The first run is closed before the second opens.
        assert len(dones) == 1
        assert dones[0].songId == songs[0].songId
        assert songs[0].songId != songs[1].songId
        await session.close()

    asyncio.run(run())


def test_low_confidence_is_not_a_match() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        weak = _match()
        weak.confidence = settings.music_min_confidence - 0.1
        session._music = _SpyMusicService([weak])
        _arm_window(session)
        await session._scan_music_once()
        assert _songs(sent) == []
        assert session._music_active is False
        await session.close()

    asyncio.run(run())


def test_no_window_skips_identify() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        spy = _SpyMusicService([_match()])
        session._music = spy
        # No audio armed → nothing to fingerprint.
        await session._scan_music_once()
        assert spy.identify_calls == 0
        assert _songs(sent) == []
        await session.close()

    asyncio.run(run())


def test_scan_skipped_during_translation() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        spy = _SpyMusicService([_match()])
        session._music = spy
        _arm_window(session)
        session._translation_active = True  # translation owns the box
        await session._scan_music_once()
        assert spy.identify_calls == 0
        assert _songs(sent) == []
        await session.close()

    asyncio.run(run())


def test_cue_suppressed_while_song_plays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        from api.contract import Cue

        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._music = _SpyMusicService([_match()])
        _arm_window(session)
        await session._scan_music_once()  # song run is live
        assert session._music_active is True

        from api.contract import CaptionFinal

        session._consider_cue(
            CaptionFinal(
                type="caption.final", segmentId="s1", text="how far is the sun", startMs=0,
                endMs=1000, lang="en",
            )
        )
        await asyncio.gather(*session._cue_tasks)
        assert [m for m in sent if isinstance(m, Cue)] == []
        await session.close()

    asyncio.run(run())


def test_inflight_cue_dropped_when_song_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        from api.contract import Cue

        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)

        # A cue generator that flips the song on mid-generation, mimicking a song
        # that opens while this cue was already being generated.
        class _Gen:
            def generate(self, transcript, *, avoid_cues=(), evidence=()):
                session._music_active = True
                return GeneratedCue(title="Sun", body="It is far.")

        session._cue_generator = _Gen()
        await session._generate_cue(at_ms=1000)
        assert [m for m in sent if isinstance(m, Cue)] == []
        await session.close()

    asyncio.run(run())


def test_scan_loop_and_close_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "music_backend", "stub")
    monkeypatch.setattr(settings, "music_scan_interval_ms", 10)
    monkeypatch.setattr(settings, "music_lock_interval_ms", 10)

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        assert session._music_scan is not None  # loop spawned when on
        spy = _SpyMusicService([_match()], lyrics=[SyncedLine(0, "la")])
        session._music = spy
        _arm_window(session)
        # Let the loop tick a couple of times.
        for _ in range(20):
            if _songs(sent):
                break
            await asyncio.sleep(0.01)
        assert _songs(sent), "scan loop should have emitted a song"
        await session.close()
        assert spy.closed is True
        assert session._music_scan is None

    asyncio.run(run())


def test_music_off_starts_no_loop() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)  # default backend is off
        assert session._music is None
        assert session._music_scan is None
        await session.close()

    asyncio.run(run())
