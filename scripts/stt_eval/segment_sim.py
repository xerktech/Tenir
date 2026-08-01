"""Replay retained session audio through the production VAD/windowing logic
offline, across candidate (silence_ms, max_segment_ms) settings (XERK-175).

Translations and cues run only on FINALIZED turns, so the turn-close windows
are their latency floor: a word spoken at time t inside a turn that closes at
time T waits (T - t) before the translation/cue pipeline can even see it.
This harness measures that wait — and how turns close (real pause vs forced
cap) — for each candidate setting, over the household's real audio, using the
REAL ``StreamingTranscriber`` windowing/VAD code with a stub engine (no model
server needed; only the boundary decisions are exercised).

Like ``scripts/cue_eval/replay.py`` it imports the installed ``api`` package:
run ``pip install -e api`` first. Audio is the disk audio backend's retained
``{household}/{conversation_id}.wav`` tree (16 kHz mono s16le).

Usage:
  python scripts/stt_eval/segment_sim.py --audio-dir /mnt/data/Docker/Tenir/audio \
      --config 700/12000 --config 500/8000 --out sim.json

Each ``--config`` is ``silence_ms/max_segment_ms``. The printed table reports,
per config: speech-turn count, how many closed on the forced cap, turn-length
percentiles, and the wait-for-final distribution over speech chunks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import wave
from pathlib import Path

from api.stt.streaming import StreamingTranscriber

# The audio store writes 16 kHz mono s16le (api/src/api/persistence/wav.py).
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH

# Feed size for the replay. Web capture ships ~85 ms chunks (4096 samples at
# 48 kHz, packages/client-core/src/browserAudio.ts); 100 ms is close enough
# and divides the byte math evenly. VAD granularity follows the chunk size, so
# keep this near what clients really send.
CHUNK_MS = 100
CHUNK_BYTES = BYTES_PER_SEC * CHUNK_MS // 1000


class _StubResult:
    """Non-empty so every boundary surfaces; the text itself is never read."""

    text = "x"
    words = ()
    language = None


class _StubEngine:
    def transcribe(self, samples, language=None, want_words=True):  # noqa: ARG002
        return _StubResult()


class BoundaryProbe(StreamingTranscriber):
    """Records every finalize with its close reason, plus each speech chunk's
    timeline position, so wait-for-final can be computed per spoken chunk."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[tuple[int, int, bool, str]] = []  # start, end, had_speech, reason
        self.speech_ms: list[int] = []

    def _update_vad(self, pcm: bytes) -> None:
        super()._update_vad(pcm)
        if self._trailing_silence == 0 and self._has_speech:
            self.speech_ms.append(self._segment_start_ms + len(self._buf) * 1000 // BYTES_PER_SEC)

    async def _finalize(self) -> None:
        reason = (
            "cap"
            if len(self._buf) >= self._max_segment_bytes
            else ("silence" if self._trailing_silence >= self._silence_bytes else "flush")
        )
        start = self._segment_start_ms
        end = start + len(self._buf) * 1000 // BYTES_PER_SEC
        self.turns.append((start, end, self._has_speech, reason))
        await super()._finalize()


async def replay(pcm: bytes, silence_ms: int, max_segment_ms: int) -> dict:
    """One WAV through one config; returns speech turns + per-chunk waits."""
    probe = BoundaryProbe(
        _StubEngine(),
        partial_interval_ms=10**9,  # partials never fire; boundaries are the subject
        max_segment_ms=max_segment_ms,
        silence_ms=silence_ms,
        local_agreement=False,
        final_words=False,
    )
    for i in range(0, len(pcm), CHUNK_BYTES):
        await probe.push(pcm[i : i + CHUNK_BYTES])
        while not probe._queue.empty():  # keep the unread caption queue bounded
            probe._queue.get_nowait()
    await probe.flush()

    speech_turns = [(s, e, r) for (s, e, sp, r) in probe.turns if sp]
    waits = []
    ti = 0
    for ms in probe.speech_ms:
        while ti < len(speech_turns) and speech_turns[ti][1] <= ms:
            ti += 1
        for s, e, _ in speech_turns[ti : ti + 2]:
            if s <= ms <= e:
                waits.append(e - ms)
                break
    return {
        "turns": [(e - s, r) for s, e, r in speech_turns],
        "waits": waits,
    }


def _pct(xs: list[int], p: float) -> int:
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))]


def summarize(label: str, turns: list, waits: list[int]) -> dict:
    lens = [length for length, _ in turns]
    capped = sum(1 for _, reason in turns if reason == "cap")
    return {
        "config": label,
        "speech_turns": len(turns),
        "cap_closed": capped,
        "cap_pct": round(100 * capped / len(turns), 1) if turns else 0.0,
        "len_p50_ms": _pct(lens, 0.5) if lens else 0,
        "len_p90_ms": _pct(lens, 0.9) if lens else 0,
        "wait_mean_ms": round(statistics.mean(waits)) if waits else 0,
        "wait_p50_ms": _pct(waits, 0.5) if waits else 0,
        "wait_p90_ms": _pct(waits, 0.9) if waits else 0,
        "wait_max_ms": max(waits) if waits else 0,
    }


def load_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as wav:
        if (
            wav.getframerate() != SAMPLE_RATE
            or wav.getnchannels() != 1
            or wav.getsampwidth() != SAMPLE_WIDTH
        ):
            raise ValueError(f"{path.name}: expected {SAMPLE_RATE} Hz mono s16le")
        return wav.readframes(wav.getnframes())


async def run(args: argparse.Namespace) -> None:
    wavs = sorted(args.audio_dir.glob("*/*.wav"))
    if args.conversations:
        only = set(args.conversations.split(","))
        wavs = [w for w in wavs if w.stem in only]
    if not wavs:
        raise SystemExit(f"no WAVs under {args.audio_dir}")
    configs = []
    for spec in args.config:
        silence_ms, _, cap_ms = spec.partition("/")
        configs.append((spec, int(silence_ms), int(cap_ms)))

    results = {spec: {"turns": [], "waits": []} for spec, _, _ in configs}
    for i, path in enumerate(wavs, 1):
        pcm = load_pcm(path)
        for spec, silence_ms, cap_ms in configs:
            one = await replay(pcm, silence_ms, cap_ms)
            results[spec]["turns"].extend(one["turns"])
            results[spec]["waits"].extend(one["waits"])
        print(f"[{i}/{len(wavs)}] {path.stem}", flush=True)

    summaries = [summarize(spec, r["turns"], r["waits"]) for spec, r in results.items()]
    header = (
        f"{'config':>14} {'turns':>6} {'cap%':>5} {'len p50':>8} {'len p90':>8}"
        f" {'wait mean':>10} {'wait p90':>9} {'wait max':>9}"
    )
    print(header)
    for s in summaries:
        print(
            f"{s['config']:>14} {s['speech_turns']:>6} {s['cap_pct']:>4.0f}%"
            f" {s['len_p50_ms']:>8} {s['len_p90_ms']:>8}"
            f" {s['wait_mean_ms']:>10} {s['wait_p90_ms']:>9} {s['wait_max_ms']:>9}"
        )
    if args.out:
        args.out.write_text(json.dumps(summaries, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio-dir", type=Path, required=True, help="retained-audio root ({household}/{id}.wav)")
    ap.add_argument(
        "--config",
        action="append",
        required=True,
        help="silence_ms/max_segment_ms, repeatable (e.g. --config 500/8000)",
    )
    ap.add_argument("--conversations", help="comma-separated conversation ids to restrict to")
    ap.add_argument("--out", type=Path, help="write per-config summary JSON here")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
