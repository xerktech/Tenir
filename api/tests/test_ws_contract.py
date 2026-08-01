"""Phase 0 smoke test: the api boots and speaks the WS contract end to end."""

import json

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app


def test_health() -> None:
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


def test_session_handshake_and_ping() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "session.start", "micSource": "g2-microphone"}))
        ready = ws.receive_json()
        assert ready["type"] == "session.ready"
        assert ready["sessionId"]

        ws.send_text(json.dumps({"type": "ping", "t": 42}))
        # A partial caption may interleave; drain until we see the pong.
        for _ in range(5):
            msg = ws.receive_json()
            if msg["type"] == "pong":
                assert msg["t"] == 42
                break
        else:
            raise AssertionError("no pong received")


def test_audio_produces_captions() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
        assert ws.receive_json()["type"] == "session.ready"

        # ~2.1s of silence -> at least one final segment from the stub.
        chunk = b"\x00" * 3200  # 100ms @ 16kHz s16le mono
        for _ in range(21):
            ws.send_bytes(chunk)

        saw_partial = saw_final = False
        for _ in range(60):
            msg = ws.receive_json()
            if msg["type"] == "caption.partial":
                saw_partial = True
            elif msg["type"] == "caption.final":
                saw_final = True
                break
        assert saw_partial and saw_final


def test_music_produces_a_song_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    # Full-stack exercise of Music ID (XERK-184): with the stub recognizer and a
    # fast scan cadence, a window of real (non-silent) audio drives a `song` frame
    # out over the live socket — ws_endpoint -> Session -> scan loop -> WS.
    monkeypatch.setattr(settings, "music_backend", "stub")
    monkeypatch.setattr(settings, "music_scan_interval_ms", 50)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
        assert ws.receive_json()["type"] == "session.ready"

        # >5s of non-silent audio so the scan window fills and the stub "hears" a song.
        chunk = b"\x01\x02" * 1600  # 100ms @ 16kHz s16le mono, non-zero
        for _ in range(60):
            ws.send_bytes(chunk)

        song = None
        for _ in range(200):
            msg = ws.receive_json()
            if msg["type"] == "song":
                song = msg
                break
        assert song is not None, "no song frame produced"
        assert song["artist"] and song["title"]
        assert song["lines"], "song carried no synced lyrics"
        assert song["offsetMs"] >= 0


def test_rejects_unparseable_message() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "nonsense"}))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "bad_request"
