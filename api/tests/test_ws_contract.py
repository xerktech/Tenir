"""Phase 0 smoke test: the api boots and speaks the WS contract end to end."""

import json

from fastapi.testclient import TestClient

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


def test_rejects_unparseable_message() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "nonsense"}))
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert msg["code"] == "bad_request"
