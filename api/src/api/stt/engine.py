"""Whisper engine seam + PCM helpers (master plan §5.2).

`StreamingTranscriber` (see `streaming.py`) owns the realtime windowing, VAD and
partial/final cadence; the actual model lives behind this narrow `WhisperEngine`
interface so it can be swapped (faster-whisper now, Parakeet/Triton later) and so
the streaming logic is testable with a fake engine — no GPU, no model download.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

# The G2 mic and the api STT input are both 16 kHz s16le mono — no resampling.
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_SEC = SAMPLE_RATE * BYTES_PER_SAMPLE


@dataclass
class EngineWord:
    """One recognised word with timing relative to the start of the given samples."""

    text: str
    start: float  # seconds
    end: float  # seconds
    probability: float | None = None


@dataclass
class EngineResult:
    """A transcription of one audio window."""

    text: str
    words: list[EngineWord] = field(default_factory=list)
    language: str | None = None


class WhisperEngine(Protocol):
    def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
        """Transcribe a mono float32 [-1, 1] window. Synchronous (may block on GPU)."""
        ...


def pcm16_to_float32(pcm: bytes) -> np.ndarray:
    """Decode 16 kHz s16le mono PCM bytes to float32 samples in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def float32_to_wav(samples: np.ndarray) -> bytes:
    """Encode a mono float32 [-1, 1] window as a 16 kHz s16le WAV in memory.

    The wire format the Voxtral HTTP seam sends each window over: a
    self-describing container the receiver decodes with ``wav_to_float32`` — no
    out-of-band sample-rate metadata.
    """
    pcm16 = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm16.tobytes())
    return buf.getvalue()


def wav_to_float32(data: bytes) -> np.ndarray:
    """Decode a 16 kHz s16le mono WAV container back to float32 samples in [-1, 1].

    The inverse of ``float32_to_wav``.
    """
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
    return pcm16_to_float32(frames)


def rms(samples: np.ndarray) -> float:
    """Root-mean-square level of a sample window; used for energy-based VAD."""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
