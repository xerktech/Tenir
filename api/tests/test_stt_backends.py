"""STT seam factory selection + the offline engine's WAV encoder.

The Parakeet engine is exercised only with a real model/endpoint, so here we cover
the *selection* logic and the pure WAV encoding the engine sends.
"""

from __future__ import annotations

import io
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from api.stt import make_transcriber
from api.stt.engine import SAMPLE_RATE
from api.stt.streaming import StreamingTranscriber
from api.stt.stub import StubTranscriber
from api.stt.parakeet import ParakeetEngine


def test_factory_stub_is_default() -> None:
    assert isinstance(make_transcriber(), StubTranscriber)


def test_factory_parakeet_builds_streaming_transcriber(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    # Construction must not touch the network (the engine connects lazily on push).
    assert isinstance(make_transcriber(), StreamingTranscriber)


def test_factory_threads_start_offset_through_both_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.config import settings

    stub = make_transcriber(start_offset_ms=1234)
    assert isinstance(stub, StubTranscriber)
    assert stub._start_offset_ms == 1234

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    streaming = make_transcriber(start_offset_ms=1234)
    assert isinstance(streaming, StreamingTranscriber)
    assert streaming._segment_start_ms == 1234


def test_factory_final_word_timestamps_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production finals skip per-word timing (~5.5x faster on the deployed
    server, nothing consumes the words); the settings flag restores it."""
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    assert make_transcriber()._final_words is False

    monkeypatch.setattr(settings, "stt_final_word_timestamps", True)
    assert make_transcriber()._final_words is True


def test_factory_turn_close_windows_tuned_for_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the XERK-175 turn-close windows. Translations and cues run only on
    finalized turns, so these two values ARE their latency floor: measured over
    the July 2026 deployment audio, 500 ms silence / 8 s cap cuts the mean
    wait-for-final from 4.7 s to 3.0 s and closes more turns on a real pause
    (see the Settings comment). Don't regress them without re-running
    scripts/stt_eval/segment_sim.py on current session audio."""
    from api.config import settings
    from api.stt.engine import BYTES_PER_SEC

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    transcriber = make_transcriber()
    assert transcriber._silence_bytes == 500 * BYTES_PER_SEC // 1000
    assert transcriber._max_segment_bytes == 8000 * BYTES_PER_SEC // 1000


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "bogus")
    with pytest.raises(ValueError, match="unknown STT backend"):
        make_transcriber()


def test_parakeet_engine_builds_transcriptions_url() -> None:
    engine = ParakeetEngine(endpoint="http://vllm-stt:8000/v1/", model="parakeet")
    assert engine._url == "http://vllm-stt:8000/v1/audio/transcriptions"


def test_parakeet_wav_encoding_is_16k_mono_s16le() -> None:
    samples = np.zeros(SAMPLE_RATE // 10, dtype=np.float32)  # 0.1s of silence
    data = ParakeetEngine._wav_bytes(samples)
    with wave.open(io.BytesIO(data), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getnframes() == samples.size


def test_parakeet_requests_json_not_verbose_json(monkeypatch: pytest.MonkeyPatch) -> None:
    # The retired vLLM-Voxtral server rejected response_format=verbose_json with a
    # 400; the engine settled on "json" and keeps requesting it (word timestamps
    # ride the body's words field either way).
    import httpx

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:  # noqa: D401
            pass

        def json(self) -> dict:
            return {"text": "hello", "language": "en"}

    async def _fake_post(self, url, *, data, files, headers):  # noqa: ANN001
        captured["data"] = data
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    engine = ParakeetEngine(endpoint="http://vllm-stt:8000/v1", model="parakeet")
    result = engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)

    assert captured["data"]["response_format"] == "json"
    assert captured["data"]["response_format"] != "verbose_json"
    # No key configured → no auth header (a direct vLLM doesn't authenticate).
    assert "Authorization" not in captured["headers"]
    assert result.text == "hello"


def test_parakeet_sends_bearer_when_keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Through the LiteLLM gateway the engine must authenticate with its key.
    import httpx

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"text": "hi", "language": "en"}

    async def _fake_post(self, url, *, data, files, headers):  # noqa: ANN001
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    engine = ParakeetEngine(endpoint="http://litellm:4000/v1", model="parakeet", api_key="sk-key")
    engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)

    assert captured["headers"]["Authorization"] == "Bearer sk-key"


def test_parakeet_skips_word_timestamps_when_not_wanted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Partials never read the words array, so the engine tells the server not to
    # spend decode time producing it (XERK-115).
    import httpx

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {"text": "hello", "language": "en"}

    async def _fake_post(self, url, *, data, files, headers):  # noqa: ANN001
        captured["data"] = data
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    engine = ParakeetEngine(endpoint="http://parakeet:8000/v1", model="parakeet")

    samples = np.zeros(SAMPLE_RATE // 10, dtype=np.float32)
    engine.transcribe(samples, language=None, want_words=False)
    assert captured["data"]["timestamps"] == "false"

    # A final wants word timing, so the flag is absent and the server's default (on) wins.
    engine.transcribe(samples, language=None, want_words=True)
    assert "timestamps" not in captured["data"]


def _trickling_server(body: bytes, *, delay: float) -> ThreadingHTTPServer:
    """A local transcription server that sends its response one byte per ``delay`` s."""

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for b in body:
                    self.wfile.write(bytes([b]))
                    self.wfile.flush()
                    time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client gave up — that's the behaviour under test

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_parakeet_timeout_is_a_whole_request_deadline() -> None:
    # Regression (XERK-1448): httpx's timeout is per read, so a server trickling
    # one byte every 0.2 s (each read well inside the 0.5 s timeout) held a decode
    # for the full ~3.4 s body. The engine's timeout must bound the whole request.
    server = _trickling_server(b'{"text": "never finishes in time"}', delay=0.2)
    try:
        engine = ParakeetEngine(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="parakeet", timeout=0.5
        )
        t0 = time.monotonic()
        with pytest.raises(TimeoutError):
            engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)
        assert time.monotonic() - t0 < 1.5
    finally:
        server.shutdown()
        server.server_close()


def test_parakeet_returns_a_response_inside_the_deadline() -> None:
    # The deadline mustn't cost a healthy (if slowish) server its result.
    server = _trickling_server(b'{"text": " hi ", "language": "en"}', delay=0.001)
    try:
        engine = ParakeetEngine(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="parakeet", timeout=5.0
        )
        result = engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language="en")
        assert result.text == "hi"
        assert result.language == "en"
    finally:
        server.shutdown()
        server.server_close()


def test_parakeet_deadline_covers_a_hung_dns_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # getaddrinfo runs in the loop's executor thread; asyncio.run would join that
    # thread on exit, so a stuck resolver held the decode past the deadline.
    import socket

    real = socket.getaddrinfo

    def _hung(host, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if host in ("stt.invalid", b"stt.invalid"):  # anyio passes it IDNA-encoded
            time.sleep(3.0)
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _hung)
    engine = ParakeetEngine(endpoint="http://stt.invalid:8000/v1", model="parakeet", timeout=0.5)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)
    assert time.monotonic() - t0 < 1.5


def test_parakeet_raises_on_an_error_status() -> None:
    # A 5xx must fail the decode, not parse the error body as an empty transcript.
    import httpx

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        engine = ParakeetEngine(endpoint=f"http://127.0.0.1:{server.server_port}/v1", model="m")
        with pytest.raises(httpx.HTTPStatusError):
            engine.transcribe(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), language=None)
    finally:
        server.shutdown()
        server.server_close()


# ----- direct STT route (XERK-115) ------------------------------------------


def test_stt_route_defaults_to_the_litellm_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_endpoint", "")
    monkeypatch.setattr(settings, "litellm_endpoint", "http://litellm:4000/v1")
    monkeypatch.setattr(settings, "litellm_api_key", "sk-master")

    assert settings.stt_endpoint_url == "http://litellm:4000/v1"
    assert settings.stt_key == "sk-master"


def test_stt_route_prefers_a_direct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_endpoint", "http://parakeet:8000/v1")
    monkeypatch.setattr(settings, "stt_api_key", "")
    monkeypatch.setattr(settings, "litellm_endpoint", "http://litellm:4000/v1")
    monkeypatch.setattr(settings, "litellm_api_key", "sk-master")

    assert settings.stt_endpoint_url == "http://parakeet:8000/v1"
    # The gateway's master key must NOT leak onto a direct model-server call.
    assert settings.stt_key == ""


def test_stt_route_carries_its_own_key_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_endpoint", "http://stt.internal/v1")
    monkeypatch.setattr(settings, "stt_api_key", "sk-direct")
    monkeypatch.setattr(settings, "litellm_api_key", "sk-master")

    assert settings.stt_key == "sk-direct"


def test_factory_builds_the_engine_on_the_direct_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "parakeet")
    monkeypatch.setattr(settings, "stt_endpoint", "http://parakeet:8000/v1")
    monkeypatch.setattr(settings, "stt_api_key", "")

    transcriber = make_transcriber()
    assert isinstance(transcriber, StreamingTranscriber)
    engine = transcriber._engine
    assert engine._url == "http://parakeet:8000/v1/audio/transcriptions"
    assert engine._api_key == ""


def test_parakeet_wav_encoding_clips_and_scales() -> None:
    # Out-of-range samples clip to the int16 extremes rather than wrapping.
    samples = np.array([2.0, -2.0], dtype=np.float32)
    data = ParakeetEngine._wav_bytes(samples)
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
    assert frames[0] == 32767
    assert frames[1] == -32767
