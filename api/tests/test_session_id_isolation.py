"""Client-supplied session ids must never address another household's data.

XERK-236 regression. ``session.start`` accepts an optional ``sessionId`` so a
dropped client can resume, and that id is used verbatim as BOTH the conversation
key and the audio object key (``{household}/{id}.wav``). An id shaped like
``../other-household/<their-id>`` therefore resolved to another household's
retained audio: the api read it back as this session's resume offset (leaking
its duration) and, on ``session.end``, prepended and rewrote it — silently
corrupting a different tenant's recording. Only server-issued UUIDs are honored
now, and the audio store refuses a key that is not household-scoped.
"""

from __future__ import annotations

import json
import time
import uuid

import numpy as np
import pytest
from fastapi.testclient import TestClient

from api import main, registry
from api.auth import Principal
from api.config import settings
from api.main import app
from api.persistence import audio_key, get_audio_store, get_conversation_store
from api.session import is_valid_session_id

# Deliberately carries hex LETTERS: an all-digit uuid is unchanged by
# .upper(), which would make the case-sensitivity assertion below vacuous.
VICTIM_ID = "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


@pytest.fixture(autouse=True)
def _reset() -> None:
    get_conversation_store()._by_household.clear()
    get_audio_store()._blobs.clear()
    for s in registry.active():
        registry.unregister(s)
    yield
    for s in registry.active():
        registry.unregister(s)


def _voice(freq: int = 200, *, ms: int = 100, amp: int = 8000) -> bytes:
    n = 16000 * ms // 1000
    t = np.arange(n) / 16000.0
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def _as_household(monkeypatch: pytest.MonkeyPatch, household: str) -> None:
    """Route the next WS connection's principal to ``household``."""
    monkeypatch.setattr(
        main,
        "_ws_principal",
        lambda ws: Principal(
            user_id=f"u-{household}", username=household, household=household, role="admin"
        ),
    )


def _capture(
    client: TestClient, *, household: str, session_id: str | None, freq: int, chunks: int = 25
) -> dict:
    """One full session: start, stream audio until a final lands, end, and wait
    for the conversation to be finalized (`session.end` flushes on threads)."""
    start: dict[str, object] = {"type": "session.start", "micSource": "phone-microphone"}
    if session_id is not None:
        start["sessionId"] = session_id
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps(start))
        ready = ws.receive_json()
        for _ in range(chunks):
            ws.send_bytes(_voice(freq))
        for _ in range(80):
            if ws.receive_json()["type"] == "caption.final":
                break
        # session.end is handled inline by the WS loop, but nothing is sent back;
        # a ping afterwards is answered only once the loop has moved past it, so
        # the pong is our proof that close() (and its persist) already ran.
        ws.send_text(json.dumps({"type": "session.end"}))
        ws.send_text(json.dumps({"type": "ping", "t": 1}))
        for _ in range(80):
            if ws.receive_json()["type"] == "pong":
                break

    store = get_conversation_store()
    for _ in range(100):
        conv = store.get(household, ready["sessionId"])
        if conv is not None and conv.status == "ready":
            break
        time.sleep(0.05)
    return ready


def test_is_valid_session_id_accepts_only_server_shaped_ids() -> None:
    assert is_valid_session_id(str(uuid.uuid4()))
    assert is_valid_session_id(VICTIM_ID)
    for bad in (
        f"../alpha/{VICTIM_ID}",
        f"alpha/{VICTIM_ID}",
        f"{VICTIM_ID}/../x",
        f"{VICTIM_ID}\x00",
        VICTIM_ID.upper(),  # parses, but uuid4() stringifies lowercase — not ours
        VICTIM_ID.replace("-", ""),  # parses too; the server never emits this form
        "",
        "not-a-uuid",
        "..",
    ):
        assert not is_valid_session_id(bad), bad


def test_traversal_resume_id_cannot_touch_another_households_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = get_audio_store()
    client = TestClient(app)

    # Household alpha records a session under a normal, server-shaped id.
    _as_household(monkeypatch, "alpha")
    _capture(client, household="alpha", session_id=VICTIM_ID, freq=200)
    victim_key = audio_key("alpha", VICTIM_ID)
    victim_audio = audio.get(victim_key)
    assert victim_audio, "alpha's audio should have been retained"

    # Household beta tries to resume onto it by traversing out of its own namespace.
    _as_household(monkeypatch, "beta")
    ready = _capture(client, household="beta", session_id=f"../alpha/{VICTIM_ID}", freq=900)

    # The traversal id is not echoed back, not treated as a resume, and beta's
    # audio landed under beta — alpha's recording is byte-identical.
    assert ready["sessionId"] != f"../alpha/{VICTIM_ID}"
    assert is_valid_session_id(ready["sessionId"])
    assert ready.get("resumed") is not True
    assert audio.get(victim_key) == victim_audio
    assert audio.get(audio_key("beta", ready["sessionId"])) is not None
    # Nothing was written outside a "{household}/{id}.wav" shape.
    assert all(k.count("/") == 1 and ".." not in k for k in audio._blobs)


def test_malformed_resume_id_still_starts_a_working_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejecting the id must not reject the client: it gets a fresh session."""
    _as_household(monkeypatch, settings.household_id)
    client = TestClient(app)
    ready = _capture(
        client, household=settings.household_id, session_id="not-a-uuid", freq=300
    )
    assert ready["type"] == "session.ready"
    assert is_valid_session_id(ready["sessionId"])
    conv = get_conversation_store().get(settings.household_id, ready["sessionId"])
    assert conv is not None and conv.status == "ready"


def test_a_genuine_uuid_resume_still_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not break the feature it protects."""
    _as_household(monkeypatch, "alpha")
    client = TestClient(app)
    first = _capture(client, household="alpha", session_id=None, freq=200)
    sid = first["sessionId"]
    second = _capture(client, household="alpha", session_id=sid, freq=200)
    assert second["sessionId"] == sid
    conv = get_conversation_store().get("alpha", sid)
    assert conv is not None
