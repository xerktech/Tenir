"""STT seam factory selection + the Voxtral WAV encoder.

The Voxtral engine is exercised only with a real model/endpoint, so here we cover
the *selection* logic and the pure WAV encoding the engine sends.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from api.stt import make_transcriber
from api.stt.engine import SAMPLE_RATE
from api.stt.streaming import StreamingTranscriber
from api.stt.stub import StubTranscriber
from api.stt.voxtral import VoxtralEngine


def test_factory_stub_is_default() -> None:
    assert isinstance(make_transcriber(), StubTranscriber)


def test_factory_voxtral_builds_streaming_transcriber(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    # Construction must not touch the network (the engine connects lazily on push).
    assert isinstance(make_transcriber(), StreamingTranscriber)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "bogus")
    with pytest.raises(ValueError, match="unknown STT backend"):
        make_transcriber()


def test_voxtral_engine_builds_transcriptions_url() -> None:
    engine = VoxtralEngine(endpoint="http://vllm-stt:8000/v1/", model="voxtral")
    assert engine._url == "http://vllm-stt:8000/v1/audio/transcriptions"


def test_voxtral_wav_encoding_is_16k_mono_s16le() -> None:
    samples = np.zeros(SAMPLE_RATE // 10, dtype=np.float32)  # 0.1s of silence
    data = VoxtralEngine._wav_bytes(samples)
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnframes() == samples.size


def test_voxtral_requests_json_not_verbose_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # vLLM-served Voxtral rejects response_format=verbose_json with a 400
    # ("do not support verbose_json for voxtral"); the engine must request "json".
    import httpx

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:  # noqa: D401
            pass

        def json(self) -> dict:
            return {"text": "hello", "language": "en"}

    def _fake_post(url, *, data, files, headers, timeout):  # noqa: ANN001
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)
    engine = VoxtralEngine(endpoint="http://vllm-stt:8000/v1", model="voxtral")
    result = engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)

    assert captured["data"]["response_format"] == "json"
    assert captured["data"]["response_format"] != "verbose_json"
    # No key configured → no auth header (a direct vLLM doesn't authenticate).
    assert "Authorization" not in captured["headers"]
    assert result.text == "hello"


def test_voxtral_sends_bearer_when_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Through the LiteLLM gateway the engine must authenticate with its key.
    import httpx

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"text": "hi", "language": "en"}

    def _fake_post(url, *, data, files, headers, timeout):  # noqa: ANN001
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "post", _fake_post)
    engine = VoxtralEngine(endpoint="http://litellm:4000/v1", model="voxtral", api_key="sk-key")
    engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)

    assert captured["headers"]["Authorization"] == "Bearer sk-key"


def test_voxtral_wav_encoding_clips_and_scales() -> None:
    # Out-of-range samples clip to the int16 extremes rather than wrapping.
    samples = np.array([2.0, -2.0], dtype=np.float32)
    data = VoxtralEngine._wav_bytes(samples)
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert frames[0] == 32767
    assert frames[1] == -32767
