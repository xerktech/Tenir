"""Conversation history: list, detail, search, export, audio download, delete."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api import history
from api.main import app
from api.persistence import get_audio_store, get_conversation_store, pcm16_to_wav
from api.persistence.audio import InMemoryAudioStore
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.models import Segment


@pytest.fixture(autouse=True)
def _reset_stores() -> None:
    convs = get_conversation_store()
    audio = get_audio_store()
    assert isinstance(convs, InMemoryConversationStore)
    assert isinstance(audio, InMemoryAudioStore)
    convs._by_household.clear()
    audio._blobs.clear()


def _make_conversation(cid: str, text: str, *, with_audio: bool = False) -> None:
    convs = get_conversation_store()
    convs.create("default", cid, mic_source="phone-microphone", source_lang="en")
    convs.add_segment("default", cid, Segment(f"{cid}-s1", text, 0, 2000, lang="en"))
    if with_audio:
        key = f"default/{cid}.wav"
        get_audio_store().put(key, pcm16_to_wav(b"\x00\x01" * 1600))
        convs.set_audio_key("default", cid, key)
    convs.finish("default", cid, status="ready")


def test_list_get_and_detail() -> None:
    with TestClient(app) as client:
        assert client.get("/conversations").json() == []

        _make_conversation("c1", "the quarterly budget review")
        listed = client.get("/conversations").json()
        assert len(listed) == 1
        row = listed[0]
        assert row["id"] == "c1" and row["status"] == "ready"
        assert row["segmentCount"] == 1
        assert row["micSource"] == "phone-microphone"
        assert row["durationMs"] == 2000
        assert "segments" not in row  # list view is the lightweight projection

        detail = client.get("/conversations/c1").json()
        assert detail["segments"][0]["text"] == "the quarterly budget review"
        assert detail["segments"][0]["lang"] == "en"

        assert client.get("/conversations/ghost").status_code == 404


def test_search_filters_and_ranks() -> None:
    with TestClient(app) as client:
        _make_conversation("c1", "let us discuss the budget")
        _make_conversation("c2", "weekend plans only")

        hits = client.get("/conversations", params={"q": "budget"}).json()
        assert [c["id"] for c in hits] == ["c1"]
        assert client.get("/conversations", params={"q": "nothing"}).json() == []


def test_search_honors_offset_for_pagination() -> None:
    # A search with an offset paginates instead of always returning page 1.
    with TestClient(app) as client:
        for i in range(3):
            _make_conversation(f"c{i}", "the budget meeting")  # all match "budget"
        page1 = client.get("/conversations", params={"q": "budget", "limit": 2}).json()
        page2 = client.get(
            "/conversations", params={"q": "budget", "limit": 2, "offset": 2}
        ).json()
        assert len(page1) == 2 and len(page2) == 1
        ids = {c["id"] for c in page1} | {c["id"] for c in page2}
        assert ids == {"c0", "c1", "c2"}  # full set across pages, no overlap


def test_list_409_when_persistence_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "get_conversation_store", lambda: None)
    with TestClient(app) as client:
        assert client.get("/conversations").status_code == 409


def test_export_matches_detail() -> None:
    with TestClient(app) as client:
        _make_conversation("c1", "exportable words")
        detail = client.get("/conversations/c1").json()
        export = client.get("/conversations/c1/export").json()
        assert export == detail


def test_audio_download_and_404_without_audio() -> None:
    with TestClient(app) as client:
        _make_conversation("with-audio", "spoken words", with_audio=True)
        _make_conversation("no-audio", "silent words")

        r = client.get("/conversations/with-audio/audio")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content.startswith(b"RIFF")

        assert client.get("/conversations/no-audio/audio").status_code == 404
        assert client.get("/conversations/ghost/audio").status_code == 404


def test_delete_removes_transcript_and_audio() -> None:
    with TestClient(app) as client:
        _make_conversation("c1", "delete me", with_audio=True)
        key = "default/c1.wav"
        assert get_audio_store().exists(key)

        assert client.delete("/conversations/c1").status_code == 204
        assert client.get("/conversations/c1").status_code == 404
        assert not get_audio_store().exists(key)

        assert client.delete("/conversations/c1").status_code == 404


def test_legacy_status_row_does_not_break_the_listing() -> None:
    """A conversation stored by an older build (status 'processing', from the
    re-process pipeline that no longer exists) used to fail response validation and
    500 the whole listing, hiding every conversation in the household — including
    freshly recorded ones (XERK-58)."""
    with TestClient(app) as client:
        _make_conversation("legacy", "recorded under an older build")
        _make_conversation("fresh", "recorded just now")
        # Simulate the upgraded-database row: a status this build doesn't know.
        get_conversation_store().get("default", "legacy").status = "processing"  # type: ignore[union-attr,assignment]

        r = client.get("/conversations")
        assert r.status_code == 200
        rows = {c["id"]: c for c in r.json()}
        assert set(rows) == {"legacy", "fresh"}
        # Finished (it has an end time), so it reads as ready rather than live.
        assert rows["legacy"]["status"] == "ready"

        detail = client.get("/conversations/legacy")
        assert detail.status_code == 200 and detail.json()["status"] == "ready"
