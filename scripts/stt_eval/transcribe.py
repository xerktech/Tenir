"""Replay retained session audio through an STT endpoint, segment by segment.

For every stored transcript segment, slices [start_ms, end_ms) out of the
conversation's retained WAV (the disk audio backend's ``{household}/{id}.wav``)
and POSTs it to an OpenAI-compatible ``/v1/audio/transcriptions`` endpoint —
the same surface and the same decode unit (one final per turn) production
uses, so wall-clock timings and hypotheses are directly comparable to what the
live session would have produced.

Output is a JSON file pairing each segment's stored production text with the
candidate hypothesis and the request wall time; ``score.py`` turns that into
WER + latency numbers.

Usage: see scripts/stt_eval/README.md.
"""

from __future__ import annotations

import argparse
import collections
import io
import json
import statistics
import sys
import time
import wave
from pathlib import Path

import httpx

# The audio store writes 16 kHz mono s16le (api/src/api/persistence/wav.py);
# refuse anything else rather than silently mis-slicing.
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1

# A 503 means the model server is still loading weights (parakeet-stt answers
# /health and /v1 with 503 until warm). Worth waiting out, once per run.
LOADING_RETRIES = 30
LOADING_WAIT_S = 10.0


def load_segments(path: Path, only: set[str] | None) -> dict[str, list[dict]]:
    """Group the exported segment rows by conversation, ordered by start."""
    by_conv: dict[str, list[dict]] = collections.defaultdict(list)
    for seg in json.loads(path.read_text()):
        if only and seg["conversation_id"] not in only:
            continue
        by_conv[seg["conversation_id"]].append(seg)
    for segs in by_conv.values():
        segs.sort(key=lambda s: s["start_ms"])
    return by_conv


class ConversationAudio:
    """One conversation's retained WAV, sliceable by transcript-timeline ms."""

    def __init__(self, path: Path) -> None:
        with wave.open(str(path), "rb") as wav:
            if (
                wav.getframerate() != SAMPLE_RATE
                or wav.getnchannels() != CHANNELS
                or wav.getsampwidth() != SAMPLE_WIDTH
            ):
                raise ValueError(
                    f"{path.name}: expected {SAMPLE_RATE} Hz mono s16le, got "
                    f"{wav.getframerate()} Hz x{wav.getnchannels()} w{wav.getsampwidth()}"
                )
            self.pcm = wav.readframes(wav.getnframes())
        self.duration_ms = len(self.pcm) * 1000 // (SAMPLE_RATE * SAMPLE_WIDTH)

    def slice_wav(self, start_ms: int, end_ms: int) -> tuple[bytes, int]:
        """WAV bytes for [start_ms, end_ms) clamped to the file, + actual ms."""
        start_ms = max(0, min(start_ms, self.duration_ms))
        end_ms = max(start_ms, min(end_ms, self.duration_ms))
        lo = (start_ms * SAMPLE_RATE // 1000) * SAMPLE_WIDTH
        hi = (end_ms * SAMPLE_RATE // 1000) * SAMPLE_WIDTH
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(CHANNELS)
            out.setsampwidth(SAMPLE_WIDTH)
            out.setframerate(SAMPLE_RATE)
            out.writeframes(self.pcm[lo:hi])
        return buf.getvalue(), end_ms - start_ms


def transcribe_once(
    client: httpx.Client,
    url: str,
    wav_bytes: bytes,
    model: str,
    language: str | None,
    timestamps: bool,
) -> tuple[dict, float]:
    """One transcription request; returns (response json, wall ms).

    Waits out 503s (model loading) so a fresh server doesn't fail the run, but
    any 503 wait is excluded from the reported wall time.
    """
    data: dict[str, str] = {
        "model": model,
        "response_format": "json",
        # Production finals ask for word timing (want_words=True), so keeping
        # timestamps on is what makes the latency numbers production-shaped.
        "timestamps": "true" if timestamps else "false",
    }
    if language:
        data["language"] = language
    for attempt in range(LOADING_RETRIES):
        t0 = time.perf_counter()
        resp = client.post(
            url,
            data=data,
            files={"file": ("segment.wav", wav_bytes, "audio/wav")},
        )
        wall_ms = (time.perf_counter() - t0) * 1000
        if resp.status_code == 503:
            if attempt == LOADING_RETRIES - 1:
                resp.raise_for_status()
            time.sleep(LOADING_WAIT_S)
            continue
        resp.raise_for_status()
        return resp.json(), wall_ms
    raise RuntimeError("unreachable")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("segments", type=Path, help="exported segments JSON (see README)")
    ap.add_argument("--audio-dir", type=Path, required=True, help="retained-audio root (contains {household}/{id}.wav)")
    ap.add_argument("--endpoint", default="http://10.10.10.22:9401", help="STT server base URL")
    ap.add_argument("--model", default="nvidia/parakeet-tdt-0.6b-v3")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--conversations", help="comma-separated conversation ids (the frozen eval set)")
    ap.add_argument(
        "--language",
        default="auto",
        help="'auto' (omit, let the model detect — the production default), "
        "'pinned' (use each conversation's source_lang when set), or a language code",
    )
    ap.add_argument(
        "--pad-ms",
        type=int,
        default=0,
        help="widen each slice by this many ms on both sides (word-edge clipping probe)",
    )
    ap.add_argument("--no-timestamps", action="store_true", help="skip word timing (NOT production-shaped)")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    only = set(args.conversations.split(",")) if args.conversations else None
    by_conv = load_segments(args.segments, only)
    if not by_conv:
        sys.exit("no segments matched")

    url = args.endpoint.rstrip("/") + "/v1/audio/transcriptions"
    results: list[dict] = []
    skipped: list[str] = []
    client = httpx.Client(timeout=args.timeout)

    for conv_id, segs in by_conv.items():
        audio_key = segs[0].get("audio_key") or f"default/{conv_id}.wav"
        wav_path = args.audio_dir / audio_key
        if not wav_path.exists():
            skipped.append(conv_id)
            print(f"[skip] {conv_id}: no audio at {wav_path}", file=sys.stderr)
            continue
        audio = ConversationAudio(wav_path)
        language = None
        if args.language == "pinned":
            language = segs[0].get("source_lang") or None
        elif args.language != "auto":
            language = args.language
        walls: list[float] = []
        for seg in segs:
            wav_bytes, actual_ms = audio.slice_wav(
                seg["start_ms"] - args.pad_ms, seg["end_ms"] + args.pad_ms
            )
            if actual_ms <= 0:
                skipped.append(seg["segment_id"])
                print(f"[skip] {seg['segment_id']}: span outside audio", file=sys.stderr)
                continue
            body, wall_ms = transcribe_once(
                client, url, wav_bytes, args.model, language, not args.no_timestamps
            )
            walls.append(wall_ms)
            results.append(
                {
                    "segment_id": seg["segment_id"],
                    "conversation_id": conv_id,
                    "start_ms": seg["start_ms"],
                    "end_ms": seg["end_ms"],
                    "audio_ms": actual_ms,
                    "stored_text": seg["text"],
                    "hyp_text": body.get("text", ""),
                    "hyp_language": body.get("language"),
                    "wall_ms": round(wall_ms, 1),
                }
            )
        if walls:
            print(
                f"{conv_id}: {len(walls)} segments, "
                f"median {statistics.median(walls):.0f}ms/decode"
            )

    out = {
        "meta": {
            "endpoint": args.endpoint,
            "model": args.model,
            "language": args.language,
            "pad_ms": args.pad_ms,
            "timestamps": not args.no_timestamps,
            "segments": len(results),
            "skipped": skipped,
        },
        "results": results,
    }
    args.out.write_text(json.dumps(out, indent=1))
    print(f"wrote {len(results)} results ({len(skipped)} skipped) -> {args.out}")


if __name__ == "__main__":
    main()
