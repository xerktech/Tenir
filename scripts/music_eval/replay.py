"""Replay a recorded session's audio through the SHIPPED Music ID recognizer.

Slides the session's real gating window across a stored WAV and runs the exact
``ShazamMusicService`` the api uses (recognition via shazamio + synced lyrics via
LRCLIB), emulating the session scan loop: a fixed scan interval, the min-confidence
gate, and the same track-key dedupe that decides "same song, re-sync" vs. "new
song". It reports which songs were recognized, at what session-timeline position,
the play-offset the box would anchor on, and whether LRCLIB has synced lyrics for
them — i.e. exactly what the live feature would have shown.

Not part of CI — it needs network access to Shazam + LRCLIB and real recorded
audio (install the recognizer with ``pip install -e 'api[music]'``). See
scripts/music_eval/README.md.

Usage:
    python scripts/music_eval/replay.py path/to/session.wav [more.wav ...] \
        [--scan-seconds 8] [--window-seconds 8] [--min-confidence 0.5] \
        [--lyrics-endpoint https://lrclib.net] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from api.music.shazam import ShazamMusicService
from api.music.tuning import track_key
from api.persistence import pcm16_to_wav
from api.stt.engine import BYTES_PER_SEC


@dataclass
class Recognition:
    """One recognizer hit while sliding the window across a session's audio."""

    at_ms: int  # session-timeline position of the window END (where the box anchors)
    artist: str
    title: str
    offset_ms: int  # play position into the song the box would scroll from
    confidence: float
    synced_lyric_lines: int  # 0 = no LRC on LRCLIB (box shows title, no scroll)
    event: str  # "song" (new/replaced run) or "song.sync" (same song continuing)


def _read_wav_pcm16(path: Path) -> bytes:
    """The raw 16 kHz mono PCM16 payload of a stored session WAV."""
    with wave.open(str(path), "rb") as wav:
        if wav.getframerate() != 16000 or wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(
                f"{path}: expected 16 kHz mono PCM16, got "
                f"{wav.getframerate()}Hz {wav.getnchannels()}ch {wav.getsampwidth() * 8}bit"
            )
        return wav.readframes(wav.getnframes())


async def replay_file(
    path: Path,
    *,
    svc: ShazamMusicService,
    scan_seconds: float,
    window_seconds: float,
    min_confidence: float,
) -> list[Recognition]:
    """Slide the scan window across one session WAV, emulating the session loop."""
    pcm = _read_wav_pcm16(path)
    total_ms = len(pcm) * 1000 // BYTES_PER_SEC
    win_bytes = int(window_seconds * BYTES_PER_SEC)
    step_bytes = int(scan_seconds * BYTES_PER_SEC)
    out: list[Recognition] = []
    locked_key: str | None = None
    pos = 0
    while pos + win_bytes <= len(pcm):
        window = pcm[pos : pos + win_bytes]
        at_ms = (pos + win_bytes) * 1000 // BYTES_PER_SEC
        match = await svc.identify(pcm16_to_wav(window))
        if match is not None and match.confidence >= min_confidence:
            key = match.track_key or track_key(match.artist, match.title)
            event = "song.sync" if key == locked_key else "song"
            locked_key = key
            lines = await svc.lyrics(match)
            out.append(
                Recognition(
                    at_ms=at_ms,
                    artist=match.artist,
                    title=match.title,
                    offset_ms=match.offset_ms,
                    confidence=match.confidence,
                    synced_lyric_lines=len(lines),
                    event=event,
                )
            )
        else:
            locked_key = None
        pos += step_bytes
    print(f"\n=== {path.name} ({total_ms / 1000:.0f}s) ===")
    if not out:
        print("  no song recognized")
    for r in out:
        tag = "♪ NEW " if r.event == "song" else "  sync"
        lyr = f"{r.synced_lyric_lines} synced lines" if r.synced_lyric_lines else "NO synced lyrics"
        print(
            f"  [{r.at_ms // 1000:>4}s] {tag} {r.artist} - {r.title} "
            f"(offset {r.offset_ms // 1000}s, conf {r.confidence:.2f}, {lyr})"
        )
    return out


async def main_async(args: argparse.Namespace) -> None:
    svc = ShazamMusicService(
        lyrics_endpoint=args.lyrics_endpoint,
        min_confidence=args.min_confidence,
        window_seconds=args.window_seconds,
    )
    results: dict[str, list[dict]] = {}
    try:
        for path in args.wavs:
            recs = await replay_file(
                Path(path),
                svc=svc,
                scan_seconds=args.scan_seconds,
                window_seconds=args.window_seconds,
                min_confidence=args.min_confidence,
            )
            results[Path(path).name] = [asdict(r) for r in recs]
    finally:
        await svc.close()
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("wavs", nargs="+", help="Recorded session WAV(s), 16 kHz mono PCM16.")
    p.add_argument("--scan-seconds", type=float, default=8.0, help="Gap between scans.")
    p.add_argument("--window-seconds", type=float, default=8.0, help="Fingerprint window length.")
    p.add_argument("--min-confidence", type=float, default=0.5)
    p.add_argument("--lyrics-endpoint", default="https://lrclib.net")
    p.add_argument("--json", help="Optional path to write the full results JSON.")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
