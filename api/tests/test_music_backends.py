"""Music ID backend seam (XERK-184): factory selection, the model-free stub, the
pure LRC parser, the shazam response mapper, and the shared tuning helpers — all
without a network call or a real fingerprinter."""

from __future__ import annotations

import asyncio

import pytest

from api.config import settings
from api.music import (
    make_music_service,
    parse_lrc,
    scan_backoff_ms,
    track_key,
    window_bytes,
)
from api.music.lyrics import SyncedLine
from api.music.shazam import ShazamMusicService
from api.music.stub import StubMusicService
from api.stt.engine import BYTES_PER_SEC


# ---- factory -----------------------------------------------------------------


@pytest.mark.parametrize(
    "backend,expected",
    [("off", type(None)), ("stub", StubMusicService), ("shazam", ShazamMusicService)],
)
def test_factory_selects_backend(
    monkeypatch: pytest.MonkeyPatch, backend: str, expected: type
) -> None:
    monkeypatch.setattr(settings, "music_backend", backend)
    assert isinstance(make_music_service(), expected)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "music_backend", "bogus")
    with pytest.raises(ValueError, match="unknown music backend"):
        make_music_service()


# ---- stub --------------------------------------------------------------------


def test_stub_recognizes_audio_and_ignores_silence() -> None:
    async def run() -> None:
        svc = StubMusicService()
        # Silence (all zero past the WAV header) is "no song playing".
        assert await svc.identify(b"\x00" * 200) is None
        # Real audio yields the canned track with synced lyrics.
        wav = b"\x00" * 44 + b"\x01\x02" * 500
        match = await svc.identify(wav)
        assert match is not None
        assert match.artist and match.title
        assert match.confidence >= settings.music_min_confidence
        assert match.offset_ms >= 0
        assert match.track_key == track_key(match.artist, match.title)
        lines = await svc.lyrics(match)
        assert lines and all(isinstance(ln, SyncedLine) for ln in lines)
        await svc.close()

    asyncio.run(run())


# ---- LRC parser --------------------------------------------------------------


def test_parse_lrc_expands_timestamps_and_sorts() -> None:
    lrc = "\n".join(
        [
            "[ar:Radiohead]",  # metadata — ignored
            "[length:03:20]",  # metadata — ignored
            "[00:15.00][00:45.00] chorus",  # two timestamps, one line
            "[00:12.34] first",
            "[01:02.500] third",  # 3-digit fraction = milliseconds
            "not a lyric line",  # no timestamp — ignored
        ]
    )
    lines = parse_lrc(lrc)
    assert [(ln.at_ms, ln.text) for ln in lines] == [
        (12340, "first"),
        (15000, "chorus"),
        (45000, "chorus"),
        (62500, "third"),
    ]


def test_parse_lrc_fraction_scaling() -> None:
    # 1 digit = tenths, 2 = centiseconds, none = whole second.
    assert parse_lrc("[00:01.5] a")[0].at_ms == 1500
    assert parse_lrc("[00:01.05] a")[0].at_ms == 1050
    assert parse_lrc("[00:01] a")[0].at_ms == 1000


def test_parse_lrc_empty_and_garbage() -> None:
    assert parse_lrc("") == []
    assert parse_lrc("no timestamps here\njust text") == []


def test_parse_lrc_keeps_instrumental_gap_lines() -> None:
    # An empty lyric line (instrumental gap) is kept so scroll timing stays honest.
    lines = parse_lrc("[00:10.00] \n[00:20.00] words")
    assert [(ln.at_ms, ln.text) for ln in lines] == [(10000, ""), (20000, "words")]


# ---- tuning ------------------------------------------------------------------


def test_track_key_normalizes() -> None:
    assert track_key("Radiohead —", "Weird  Fishes") == track_key("radiohead", "weird fishes")
    assert track_key("A", "B") != track_key("A", "C")


def test_window_bytes() -> None:
    assert window_bytes(8.0) == 8 * BYTES_PER_SEC
    assert window_bytes(0) == 0
    assert window_bytes(-3) == 0


def test_scan_backoff_grows_and_caps() -> None:
    assert scan_backoff_ms(0, base_ms=8000, cap_ms=60000) == 8000
    assert scan_backoff_ms(1, base_ms=8000, cap_ms=60000) == 16000
    assert scan_backoff_ms(2, base_ms=8000, cap_ms=60000) == 32000
    # Caps rather than growing unbounded.
    assert scan_backoff_ms(3, base_ms=8000, cap_ms=60000) == 60000
    assert scan_backoff_ms(50, base_ms=8000, cap_ms=60000) == 60000


# ---- shazam response mapping (pure) ------------------------------------------


def _shazam_payload(offset: float = 12.5) -> dict:
    return {
        "matches": [{"offset": offset}],
        "track": {
            "title": "Weird Fishes",
            "subtitle": "Radiohead",
            "sections": [
                {"type": "SONG", "metadata": [{"title": "Album", "text": "In Rainbows"}]}
            ],
        },
    }


def test_shazam_parse_match_anchors_on_window_end() -> None:
    match = ShazamMusicService._parse_match(_shazam_payload(offset=12.5), window_ms=8000)
    assert match is not None
    assert match.artist == "Radiohead"
    assert match.title == "Weird Fishes"
    assert match.album == "In Rainbows"
    # offset = start-of-window (12.5s) + window length (8s) = 20500ms.
    assert match.offset_ms == 20500
    assert match.confidence == 1.0
    assert match.track_key == track_key("Radiohead", "Weird Fishes")


def test_shazam_parse_match_no_match() -> None:
    assert ShazamMusicService._parse_match({"matches": [], "track": {}}, window_ms=8000) is None
    # A resolved id but no artist/title is not a usable match.
    assert (
        ShazamMusicService._parse_match(
            {"matches": [{"offset": 1.0}], "track": {"title": "X"}}, window_ms=8000
        )
        is None
    )


def test_shazam_parse_match_bad_offset_defaults_to_window() -> None:
    payload = _shazam_payload()
    payload["matches"][0]["offset"] = "not-a-number"
    match = ShazamMusicService._parse_match(payload, window_ms=8000)
    assert match is not None
    assert match.offset_ms == 8000  # 0 start + window


@pytest.mark.parametrize(
    "raw,expected",
    [(210, 210000), (210000, 210000), (None, None), ("bad", None)],
)
def test_shazam_duration_coercion(raw: object, expected: int | None) -> None:
    payload = _shazam_payload()
    if raw is not None:
        payload["track"]["duration"] = raw
    match = ShazamMusicService._parse_match(payload, window_ms=0)
    assert match is not None
    assert match.duration_ms == expected
