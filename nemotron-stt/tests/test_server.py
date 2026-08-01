"""Tests for the Nemotron streaming server's transport and protocol.

NeMo (and a GPU) are deliberately out of scope — everything inside
``NemotronStreamDecoder`` is the model's business and pulling the multi-GB NGC stack
into a PR gate isn't worth it. What *is* worth testing is the layer this repo owns:
the WebSocket handshake, the per-frame partial protocol, the reset control, language
pinning, readiness gating and decode-error resilience. So ``make_decoder`` is
replaced by a fake decoder that records how it was driven.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import numpy as np
import pytest
import server
import uvicorn
from fastapi.testclient import TestClient


class FakeDecoder:
    """Appends one token per feed so partials visibly grow; records every call."""

    def __init__(self, *, target_lang: str) -> None:
        self.target_lang = target_lang
        self.feeds: list[int] = []
        self.resets = 0
        self._tokens: list[str] = []

    def feed(self, samples: np.ndarray) -> str:
        self.feeds.append(len(samples))
        self._tokens.append(f"w{len(self._tokens)}")
        return " ".join(self._tokens)

    def reset(self) -> None:
        self.resets += 1
        self._tokens = []


@pytest.fixture
def decoders(monkeypatch: pytest.MonkeyPatch) -> list[FakeDecoder]:
    """Install a fake decoder factory; return the list of decoders it hands out."""
    made: list[FakeDecoder] = []

    def factory(*, target_lang: str) -> FakeDecoder:
        d = FakeDecoder(target_lang=target_lang)
        made.append(d)
        return d

    monkeypatch.setattr(server, "make_decoder", factory)
    server._ready.set()
    yield made
    server._ready.clear()


def _pcm(seconds: float = 0.1) -> bytes:
    return np.zeros(int(server.TARGET_SR * seconds), dtype="<i2").tobytes()


def test_health_503_until_ready() -> None:
    server._ready.clear()
    client = TestClient(server.app)
    assert client.get("/health").status_code == 503
    server._ready.set()
    try:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["model"] == server.MODEL_NAME
    finally:
        server._ready.clear()


def test_stream_emits_growing_partials(decoders: list[FakeDecoder]) -> None:
    client = TestClient(server.app)
    with client.websocket_connect("/v1/audio/stream") as ws:
        ws.send_json({"target_lang": "es-ES"})
        ws.send_bytes(_pcm())
        first = ws.receive_json()
        ws.send_bytes(_pcm())
        second = ws.receive_json()

    assert first == {"type": "partial", "text": "w0"}
    assert second == {"type": "partial", "text": "w0 w1"}
    # The stream pinned the requested language and fed both chunks to one decoder.
    assert len(decoders) == 1
    assert decoders[0].target_lang == "es-ES"
    assert len(decoders[0].feeds) == 2


def test_reset_clears_running_text(decoders: list[FakeDecoder]) -> None:
    client = TestClient(server.app)
    with client.websocket_connect("/v1/audio/stream") as ws:
        ws.send_json({"target_lang": "en-US"})
        ws.send_bytes(_pcm())
        assert ws.receive_json()["text"] == "w0"
        ws.send_json({"type": "reset"})
        assert ws.receive_json() == {"type": "reset_ok"}
        ws.send_bytes(_pcm())
        after = ws.receive_json()

    assert after == {"type": "partial", "text": "w0"}  # numbering restarts after reset
    assert decoders[0].resets == 1


def test_missing_target_lang_defaults_to_auto(decoders: list[FakeDecoder]) -> None:
    client = TestClient(server.app)
    with client.websocket_connect("/v1/audio/stream") as ws:
        ws.send_json({})
        ws.send_bytes(_pcm())
        ws.receive_json()
    assert decoders[0].target_lang == "auto"


def test_decode_error_yields_empty_partial(
    monkeypatch: pytest.MonkeyPatch, decoders: list[FakeDecoder]
) -> None:
    def boom(self: FakeDecoder, samples: np.ndarray) -> str:
        raise RuntimeError("cuda said no")

    monkeypatch.setattr(FakeDecoder, "feed", boom)
    client = TestClient(server.app)
    with client.websocket_connect("/v1/audio/stream") as ws:
        ws.send_json({"target_lang": "en-US"})
        ws.send_bytes(_pcm())
        msg = ws.receive_json()
    assert msg == {"type": "partial", "text": ""}  # socket survives a decode failure


def test_stream_refused_while_loading() -> None:
    server._ready.clear()
    client = TestClient(server.app)
    with client.websocket_connect("/v1/audio/stream") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("uvicorn did not start listening in time")


def test_real_uvicorn_ws_handshake_and_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive a REAL uvicorn server over its configured WS impl (server.WS_IMPL).

    This is the regression guard for XERK-128: the handshake wedged in production —
    uvicorn accepted the socket and served /health but never completed the
    /v1/audio/stream upgrade — and TestClient's in-memory WebSocket transport can't
    see that, because it bypasses uvicorn's WS adapter entirely. Only a real server
    exercises the layer that broke. A wedged handshake fails this test (open_timeout)
    rather than hanging it.
    """
    import websockets  # noqa: PLC0415 — only needed for this real-network test

    made: list[FakeDecoder] = []

    def factory(*, target_lang: str) -> FakeDecoder:
        d = FakeDecoder(target_lang=target_lang)
        made.append(d)
        return d

    monkeypatch.setattr(server, "make_decoder", factory)
    server._ready.set()

    port = _free_port()
    config = uvicorn.Config(
        server.app, host="127.0.0.1", port=port, ws=server.WS_IMPL, log_level="error"
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        _wait_listening(port)

        async def drive() -> tuple[dict, dict, dict]:
            url = f"ws://127.0.0.1:{port}/v1/audio/stream"
            async with websockets.connect(url, open_timeout=5) as ws:
                await ws.send(json.dumps({"target_lang": "en-US"}))
                await ws.send(_pcm())
                m1 = json.loads(await asyncio.wait_for(ws.recv(), 5))
                await ws.send(_pcm())
                m2 = json.loads(await asyncio.wait_for(ws.recv(), 5))
                await ws.send(json.dumps({"type": "reset"}))
                m3 = json.loads(await asyncio.wait_for(ws.recv(), 5))
                return m1, m2, m3

        m1, m2, m3 = asyncio.run(drive())
        assert m1 == {"type": "partial", "text": "w0"}  # handshake completed; partials flow
        assert m2 == {"type": "partial", "text": "w0 w1"}
        assert m3 == {"type": "reset_ok"}
        assert made and made[0].target_lang == "en-US"
    finally:
        srv.should_exit = True
        thread.join(timeout=5)
        server._ready.clear()


# ---- chunk planning (the mel-frame slicing behind NemotronStreamDecoder.feed) ----

# The real model's values at [56,3]/320 ms: first chunk 25 frames (no pre-encode
# cache), then 32-frame chunks with a 9-frame pre-encode overlap.
PLAN = dict(first_chunk=25, chunk=32, pre=9)


def test_plan_first_chunk_is_short_with_no_pre_cache() -> None:
    spans = server.plan_chunks(0, 0, 60, **PLAN)
    assert spans[0] == (0, 0, 25)  # pre_start == start: nothing prepended


def test_plan_later_chunks_carry_the_pre_encode_overlap() -> None:
    spans = server.plan_chunks(0, 0, 100, **PLAN)
    assert spans == [(0, 0, 25), (16, 25, 57), (48, 57, 89)]


def test_plan_withholds_incomplete_chunks() -> None:
    # 56 available: the 25-frame first chunk fits, the next 32 don't (25+32 > 56).
    assert server.plan_chunks(0, 0, 56, **PLAN) == [(0, 0, 25)]
    # Nothing at all fits before the first chunk is complete.
    assert server.plan_chunks(0, 0, 24, **PLAN) == []


def test_plan_resumes_mid_stream_without_replanning_the_first_chunk() -> None:
    # Picking up at frame 57 after 2 steps: chunk 3 starts exactly where step 2 ended.
    assert server.plan_chunks(57, 2, 130, **PLAN) == [(48, 57, 89), (80, 89, 121)]


def test_plan_pre_cache_is_clamped_at_zero() -> None:
    # A resumed stream whose index is smaller than the overlap must not go negative.
    assert server.plan_chunks(5, 1, 60, **PLAN)[0] == (0, 5, 37)


# ---- the CUDA-graph env knob ----


def test_env_flag_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEMOTRON_CUDA_GRAPHS", raising=False)
    assert server._env_flag("NEMOTRON_CUDA_GRAPHS") is True


@pytest.mark.parametrize("value", ["0", "false", "False", "NO"])
def test_env_flag_off_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("NEMOTRON_CUDA_GRAPHS", value)
    assert server._env_flag("NEMOTRON_CUDA_GRAPHS") is False


@pytest.mark.parametrize("value", ["1", "true", "yes", ""])
def test_env_flag_on_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("NEMOTRON_CUDA_GRAPHS", value)
    assert server._env_flag("NEMOTRON_CUDA_GRAPHS") is True
