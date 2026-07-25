"""Cues over the wire: the history detail exposes them inline, and a live session
with the stub backend pushes a `cue` frame (XERK-81)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from api.config import settings
from api.main import app
from api.persistence import get_conversation_store
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.models import Cue, Segment


@pytest.fixture(autouse=True)
def _reset_store() -> None:
    convs = get_conversation_store()
    assert isinstance(convs, InMemoryConversationStore)
    convs._by_household.clear()


def test_history_detail_includes_cues_inline() -> None:
    convs = get_conversation_store()
    convs.create("default", "c1", mic_source="phone-microphone", source_lang="en")
    convs.add_segment("default", "c1", Segment("c1-s1", "how far is the sun", 0, 2000, lang="en"))
    convs.add_cue("default", "c1", Cue("cue-1", "Sun", "About 150 million km away.", 1500))
    convs.finish("default", "c1", status="ready")

    with TestClient(app) as client:
        detail = client.get("/conversations/c1").json()
        assert len(detail["cues"]) == 1
        cue = detail["cues"][0]
        assert cue["cueId"] == "cue-1"
        assert cue["title"] == "Sun"
        assert cue["body"] == "About 150 million km away."
        assert cue["atMs"] == 1500
        # Cues are private context: kept out of the transcript search corpus.
        assert client.get("/conversations", params={"q": "million"}).json() == []


def test_history_detail_has_empty_cues_by_default() -> None:
    convs = get_conversation_store()
    convs.create("default", "c2")
    convs.add_segment("default", "c2", Segment("c2-s1", "just talking", 0, 1000))
    convs.finish("default", "c2", status="ready")
    with TestClient(app) as client:
        assert client.get("/conversations/c2").json()["cues"] == []


def test_live_session_pushes_cue_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the stub cue backend on, any finalized stub segment is cue-worthy
    # (XERK-114: fixed aggressive setting, no cueLevel on session.start), so the
    # session emits a `cue` frame alongside the captions.
    monkeypatch.setattr(settings, "cue_backend", "stub")

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "session.start", "micSource": "phone-microphone"}))
        assert ws.receive_json()["type"] == "session.ready"

        chunk = b"\x00" * 3200  # 100ms @ 16kHz s16le mono
        for _ in range(21):
            ws.send_bytes(chunk)

        saw_cue = False
        for _ in range(120):
            msg = ws.receive_json()
            if msg["type"] == "cue":
                saw_cue = True
                assert msg["cueId"] and msg["title"] and msg["body"]
                assert isinstance(msg["atMs"], int)
                break
        assert saw_cue


def test_history_detail_carries_cue_source() -> None:
    # XERK-120: a grounded cue's attribution survives into history; an ungrounded
    # one serializes with source null.
    convs = get_conversation_store()
    convs.create("default", "c3")
    convs.add_segment("default", "c3", Segment("c3-s1", "who is the PM", 0, 1000))
    convs.add_cue(
        "default", "c3", Cue("cue-g", "PM", "Andy Burnham took office.", 900, source="BBC News")
    )
    convs.add_cue("default", "c3", Cue("cue-u", "Sun", "150M km away.", 950))
    convs.finish("default", "c3", status="ready")
    with TestClient(app) as client:
        cues = client.get("/conversations/c3").json()["cues"]
        assert {c["cueId"]: c["source"] for c in cues} == {"cue-g": "BBC News", "cue-u": None}
