"""Replay a recorded session's audio through the SHIPPED Music ID recognizer.

Slides the session's real gating window across a stored WAV and runs the exact
``ShazamMusicService`` the api uses (recognition via shazamio + synced lyrics via
LRCLIB), emulating the session scan loop: a fixed scan interval, the
min-confidence gate, the hold window that keeps a run alive across missed scans,
and the takeover debounce that requires a different track to match twice before
it replaces a locked one. It reports which songs were recognized, at what
session-timeline position, the play-offset the box would anchor on, whether
LRCLIB has synced lyrics for them, and per-scan recognition latency — i.e.
exactly what the live feature would have shown, and how fast.

Recognition errors (network, Shazam rate limiting) are counted separately from
no-matches and trigger a cool-off pause, so a 429 burst neither reads as "no
song playing" nor snowballs by hammering the endpoint (XERK-187).

Not part of CI — it needs network access to Shazam + LRCLIB and real recorded
audio (install the recognizer with ``pip install -e 'api[music]'``). See
scripts/music_eval/README.md.

Usage:
    python scripts/music_eval/replay.py path/to/session.wav [more.wav ...] \
        [--scan-seconds 8] [--window-seconds 8] [--min-confidence 0.5] \
        [--hold-seconds 45] [--pace-seconds 0] \
        [--lyrics-endpoint https://lrclib.net] [--json out.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
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
    identify_ms: int  # wall time of the recognition call (the live scan's latency)
    lyrics_ms: int  # wall time of the lyric fetch (0 when cached)


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
    hold_seconds: float,
    pace_seconds: float,
) -> dict:
    """Slide the scan window across one session WAV, emulating the session loop.

    Gating mirrors the live session (``session.py``): a miss inside the hold
    window keeps the run locked (the live box lingers on ``music_hold_ms``), and
    a different track must match twice in a row to replace a locked one (the
    takeover debounce). The hold runs on the SESSION timeline, not wall clock —
    replay is faster than real time.
    """
    pcm = _read_wav_pcm16(path)
    total_ms = len(pcm) * 1000 // BYTES_PER_SEC
    win_bytes = int(window_seconds * BYTES_PER_SEC)
    step_bytes = int(scan_seconds * BYTES_PER_SEC)
    hold_ms = int(hold_seconds * 1000)
    out: list[Recognition] = []
    identify_times_ms: list[int] = []
    errors = 0
    locked_key: str | None = None
    pending_key: str | None = None
    last_match_at_ms: int | None = None
    pos = 0
    while pos + win_bytes <= len(pcm):
        window = pcm[pos : pos + win_bytes]
        at_ms = (pos + win_bytes) * 1000 // BYTES_PER_SEC
        if pace_seconds > 0:
            await asyncio.sleep(pace_seconds)
        t0 = time.perf_counter()
        try:
            match = await svc.identify(pcm16_to_wav(window))
        except Exception as exc:
            # Not a no-match: don't touch the run state, cool off so a rate
            # limit doesn't snowball, and move to the next window.
            errors += 1
            cool = min(120.0, 15.0 * (2 ** min(errors - 1, 3)))
            print(f"  [{at_ms // 1000:>4}s] !! identify failed ({exc!r:.80}), cooling off {cool:.0f}s", flush=True)
            await asyncio.sleep(cool)
            pos += step_bytes
            continue
        identify_ms = int((time.perf_counter() - t0) * 1000)
        identify_times_ms.append(identify_ms)
        if match is not None and match.confidence >= min_confidence:
            key = match.track_key or track_key(match.artist, match.title)
            if locked_key is not None and key != locked_key and key != pending_key:
                # First sighting of a different track while locked: the live
                # session only notes it (takeover debounce) — no frame yet. The
                # locked run's hold clock keeps running underneath.
                pending_key = key
                if last_match_at_ms is not None and at_ms - last_match_at_ms >= hold_ms:
                    locked_key = None
                    pending_key = None
                    last_match_at_ms = None
                pos += step_bytes
                continue
            event = "song.sync" if key == locked_key else "song"
            locked_key = key
            pending_key = None
            last_match_at_ms = at_ms
            t1 = time.perf_counter()
            lines = await svc.lyrics(match)
            lyrics_ms = int((time.perf_counter() - t1) * 1000)
            out.append(
                Recognition(
                    at_ms=at_ms,
                    artist=match.artist,
                    title=match.title,
                    offset_ms=match.offset_ms,
                    confidence=match.confidence,
                    synced_lyric_lines=len(lines),
                    event=event,
                    identify_ms=identify_ms,
                    lyrics_ms=lyrics_ms,
                )
            )
        elif locked_key is not None and last_match_at_ms is not None:
            # A miss: the live run survives inside the hold window.
            if at_ms - last_match_at_ms >= hold_ms:
                locked_key = None
                pending_key = None
                last_match_at_ms = None
        pos += step_bytes
    print(f"\n=== {path.name} ({total_ms / 1000:.0f}s) ===", flush=True)
    if not out:
        print("  no song recognized", flush=True)
    for r in out:
        tag = "♪ NEW " if r.event == "song" else "  sync"
        lyr = f"{r.synced_lyric_lines} synced lines" if r.synced_lyric_lines else "NO synced lyrics"
        print(
            f"  [{r.at_ms // 1000:>4}s] {tag} {r.artist} - {r.title} "
            f"(offset {r.offset_ms // 1000}s, conf {r.confidence:.2f}, {lyr}, "
            f"id {r.identify_ms}ms)",
            flush=True,
        )
    latency: dict[str, int | float] = {}
    if identify_times_ms:
        # The scan's recognition latency — what the live session pays per scan.
        # The window is a rolling ~8s, so anything comfortably under the scan
        # interval keeps the loop from backing up.
        times = sorted(identify_times_ms)
        latency = {
            "scans": len(times),
            "mean_ms": round(sum(times) / len(times)),
            "p95_ms": times[max(0, int(len(times) * 0.95) - 1)],
            "max_ms": times[-1],
        }
        print(
            f"  identify latency over {latency['scans']} scans: "
            f"mean {latency['mean_ms']}ms, p95 {latency['p95_ms']}ms, "
            f"max {latency['max_ms']}ms"
            + (f"  ({errors} errored scans excluded)" if errors else ""),
            flush=True,
        )
    return {
        "duration_ms": total_ms,
        "songs": sorted({f"{r.artist} - {r.title}" for r in out}),
        "recognitions": [asdict(r) for r in out],
        "errors": errors,
        "identify_latency": latency,
    }


async def main_async(args: argparse.Namespace) -> None:
    svc = ShazamMusicService(
        lyrics_endpoint=args.lyrics_endpoint,
        min_confidence=args.min_confidence,
        window_seconds=args.window_seconds,
    )
    results: dict[str, dict] = {}
    try:
        for path in args.wavs:
            results[Path(path).name] = await replay_file(
                Path(path),
                svc=svc,
                scan_seconds=args.scan_seconds,
                window_seconds=args.window_seconds,
                min_confidence=args.min_confidence,
                hold_seconds=args.hold_seconds,
                pace_seconds=args.pace_seconds,
            )
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
    p.add_argument(
        "--hold-seconds",
        type=float,
        default=45.0,
        help="Session-time a locked run survives missed scans (music_hold_ms).",
    )
    p.add_argument(
        "--pace-seconds",
        type=float,
        default=0.0,
        help="Wall-clock pause between scans — rate-limit hygiene for long replays.",
    )
    p.add_argument("--lyrics-endpoint", default="https://lrclib.net")
    p.add_argument("--json", help="Optional path to write the full results JSON.")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
