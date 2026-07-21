"""Deployment-readiness fixes (cross-household isolation, resilient start, auth
holes, and the production-backend selectors).

These cover the gaps that only bite a real deployment — CI's stub/in-memory path
otherwise never exercises them: a session-id collision across households evicting a
live session, a failing model/DB backend aborting the socket on start, the
admin-gated /metrics endpoint, and the query-token audio download.
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import registry
from api.auth import Principal, issue_token, reset_user_store
from api.auth.deps import principal_from_request
from api.config import DEFAULT_AUTH_SECRET, settings
from api.main import app
from api.persistence import get_audio_store, get_conversation_store


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_user_store()
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    for s in registry.active():
        registry.unregister(s)
    yield
    reset_user_store()
    for s in registry.active():
        registry.unregister(s)


def _enable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_secret", "test-secret")


def _token(household: str, role: str = "member") -> str:
    return issue_token(Principal("u", household, role), secret="test-secret", ttl_seconds=60)


# --- cross-household session-id collision ------------------------------------


@pytest.mark.real_auth
def test_session_id_collision_across_households_does_not_evict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client in household B presenting household A's live session id must not
    overwrite A's session in the id-keyed registry — it gets a fresh id instead."""
    _enable_auth(monkeypatch)
    start = json.dumps({"type": "session.start", "micSource": "phone-microphone"})

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws?token={_token('hA')}") as ws_a:
            ws_a.send_text(start)
            sid = ws_a.receive_json()["sessionId"]
            assert registry.get(sid).household == "hA"

            # B replays A's id: must NOT resume or evict A's session.
            with client.websocket_connect(f"/ws?token={_token('hB')}") as ws_b:
                ws_b.send_text(
                    json.dumps(
                        {"type": "session.start", "micSource": "phone-microphone", "sessionId": sid}
                    )
                )
                ready_b = ws_b.receive_json()
                assert ready_b["sessionId"] != sid  # fresh server id, not A's
                assert not ready_b.get("resumed")
                # A's session is untouched: still registered under its id, still hA.
                assert registry.get(sid).household == "hA"


# --- resilient session start -------------------------------------------------


def test_session_start_failure_sends_error_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """A backend that raises on start (model/DB/GPU init) returns an error frame and
    keeps the socket open, instead of aborting the connection unhandled."""
    from api import session as session_mod

    def boom(source_lang=None):
        raise RuntimeError("model backend unavailable")

    monkeypatch.setattr(session_mod, "make_transcriber", boom)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
        msg = ws.receive_json()
        assert msg["type"] == "error" and msg["code"] == "internal"
        # The socket is still usable: a ping is still answered.
        ws.send_text(json.dumps({"type": "ping", "t": 7}))
        assert ws.receive_json() == {"type": "pong", "t": 7}


# --- /metrics auth gate ------------------------------------------------------


@pytest.mark.real_auth
def test_metrics_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        member = {"Authorization": f"Bearer {_token('acme', 'member')}"}
        assert client.get("/metrics", headers=member).status_code == 403
        admin = {"Authorization": f"Bearer {_token('acme', 'admin')}"}
        assert client.get("/metrics", headers=admin).status_code == 200


# --- principal_from_request (query-token resolver) ---------------------------


def test_principal_from_request_accepts_query_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    p = principal_from_request(authorization=None, token=_token("acme"))
    assert p.household == "acme"


def test_principal_from_request_rejects_missing_and_bad_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_auth(monkeypatch)
    with pytest.raises(HTTPException):
        principal_from_request(authorization=None, token=None)
    with pytest.raises(HTTPException):
        principal_from_request(authorization=None, token="not-a-token")


# --- tokenized audio download ------------------------------------------------


@pytest.mark.real_auth
def test_audio_download_accepts_query_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_auth(monkeypatch)
    convs = get_conversation_store()
    audio = get_audio_store()
    convs.create("acme", "c1")
    convs.set_audio_key("acme", "c1", "acme/c1.wav")
    audio.put("acme/c1.wav", b"RIFFdata")

    with TestClient(app) as client:
        # No credentials at all -> 401 (plain navigation can't set a header).
        assert client.get("/conversations/c1/audio").status_code == 401
        # The token in the query param authenticates the download.
        r = client.get(f"/conversations/c1/audio?token={_token('acme')}")
        assert r.status_code == 200 and r.content == b"RIFFdata"


# --- startup auth-secret guard (moved from import time to lifespan) ----------


@pytest.mark.real_auth
def test_lifespan_refuses_default_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_secret", DEFAULT_AUTH_SECRET)
    # Entering the TestClient runs the lifespan startup, which must refuse to boot.
    with pytest.raises(RuntimeError, match="API_AUTH_SECRET"), TestClient(app):
        pass


# --- /ready backend probe ----------------------------------------------------


def test_ready_reports_ok_with_memory_backends() -> None:
    with TestClient(app) as client:
        r = client.get("/ready")
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] is True
        assert body["checks"]["conversations"] == "ok"
        assert body["checks"]["audio"] == "ok"


def test_ready_returns_503_when_a_backend_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import readiness

    class _DeadAudio:
        def ready(self) -> None:
            raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(readiness, "get_audio_store", lambda: _DeadAudio())
    with TestClient(app) as client:
        r = client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["ready"] is False
        assert body["checks"]["audio"].startswith("error")
