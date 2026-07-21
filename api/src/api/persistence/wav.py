"""WAV container helpers for retained audio (master plan §5.7, Phase 3).

The api streams and buffers raw 16 kHz s16le mono PCM (the G2 mic's native
format, no resampling). For retention we wrap it in a WAV container so the stored
object is a self-describing, re-playable file the re-process worker — and any
``/conversations/{id}/audio`` download — can decode without out-of-band metadata.
"""

from __future__ import annotations

import io
import wave

from api.stt.engine import SAMPLE_RATE


def pcm16_to_wav(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap 16 kHz s16le mono PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def wav_to_pcm16(data: bytes) -> bytes:
    """Extract raw 16 kHz s16le mono PCM frames from a WAV container."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        return wav.readframes(wav.getnframes())
