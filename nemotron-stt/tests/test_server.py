"""Tests for the Nemotron streaming server's transport and protocol.

NeMo (and a GPU) are deliberately out of scope — everything inside
``NemotronStreamDecoder`` is the model's business and pulling the multi-GB NGC stack
into a PR gate isn't worth it. What *is* worth testing is the layer this repo owns:
the WebSocket handshake, the per-frame partial protocol, the reset control, language
pinning, readiness gating and decode-error resilience. So ``make_decoder`` is
replaced by a fake decoder that records how it was driven.
"""

from __future__ import annotations

import numpy as np
import pytest
import server
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
