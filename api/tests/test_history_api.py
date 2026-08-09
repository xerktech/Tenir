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

        _make_conversation("11111111-1111-4111-8111-111111111111", "the quarterly budget review")
        listed = client.get("/conversations").json()
        assert len(listed) == 1
        row = listed[0]
        assert row["id"] == "11111111-1111-4111-8111-111111111111" and row["status"] == "ready"
        assert row["segmentCount"] == 1
        assert row["micSource"] == "phone-microphone"
        assert row["durationMs"] == 2000
        assert "segments" not in row  # list view is the lightweight projection

        detail = client.get("/conversations/11111111-1111-4111-8111-111111111111").json()
        assert detail["segments"][0]["text"] == "the quarterly budget review"
        assert detail["segments"][0]["lang"] == "en"

        assert client.get("/conversations/ghost").status_code == 404


def test_search_filters_and_ranks() -> None:
    with TestClient(app) as client:
        _make_conversation("11111111-1111-4111-8111-111111111111", "let us discuss the budget")
        _make_conversation("22222222-2222-4222-8222-222222222222", "weekend plans only")

        hits = client.get("/conversations", params={"q": "budget"}).json()
        assert [c["id"] for c in hits] == ["11111111-1111-4111-8111-111111111111"]
        assert client.get("/conversations", params={"q": "nothing"}).json() == []


def test_search_honors_offset_for_pagination() -> None:
    # A search with an offset paginates instead of always returning page 1.
    with TestClient(app) as client:
        made = [
            f"{d}{d}{d}{d}{d}{d}{d}{d}-{d}{d}{d}{d}-4{d}{d}{d}-8{d}{d}{d}-{d*12}"
            for d in "123"
        ]
        for cid in made:
            _make_conversation(cid, "the budget meeting")  # all match "budget"
        page1 = client.get("/conversations", params={"q": "budget", "limit": 2}).json()
        page2 = client.get(
            "/conversations", params={"q": "budget", "limit": 2, "offset": 2}
        ).json()
        assert len(page1) == 2 and len(page2) == 1
        ids = {c["id"] for c in page1} | {c["id"] for c in page2}
        assert ids == set(made)  # full set across pages, no overlap


def test_list_409_when_persistence_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(history, "get_conversation_store", lambda: None)
    with TestClient(app) as client:
        assert client.get("/conversations").status_code == 409


def test_export_matches_detail() -> None:
    with TestClient(app) as client:
        _make_conversation("11111111-1111-4111-8111-111111111111", "exportable words")
        detail = client.get("/conversations/11111111-1111-4111-8111-111111111111").json()
        export = client.get("/conversations/11111111-1111-4111-8111-111111111111/export").json()
        assert export == detail


def test_audio_download_and_404_without_audio() -> None:
    with TestClient(app) as client:
        _make_conversation("66666666-6666-4666-8666-666666666666", "spoken words", with_audio=True)
        _make_conversation("77777777-7777-4777-8777-777777777777", "silent words")

        r = client.get("/conversations/66666666-6666-4666-8666-666666666666/audio")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content.startswith(b"RIFF")
        # Playable inline (not force-downloaded) and range-seekable so the native
        # web/Android seek bar can scrub (XERK-67).
        assert r.headers["accept-ranges"] == "bytes"
        assert r.headers["content-disposition"].startswith("inline")

        assert client.get("/conversations/99999999-9999-4999-8999-999999999999/audio").status_code == 404
        assert client.get("/conversations/88888888-8888-4888-8888-888888888888/audio").status_code == 404


def test_audio_range_request_serves_partial_content() -> None:
    with TestClient(app) as client:
        _make_conversation("11111111-1111-4111-8111-111111111111", "spoken words", with_audio=True)
        full = client.get("/conversations/11111111-1111-4111-8111-111111111111/audio").content
        size = len(full)

        # A leading range: 206 with the exact slice and a Content-Range header.
        r = client.get("/conversations/11111111-1111-4111-8111-111111111111/audio", headers={"Range": "bytes=0-3"})
        assert r.status_code == 206
        assert r.headers["content-range"] == f"bytes 0-3/{size}"
        assert r.headers["accept-ranges"] == "bytes"
        assert r.content == full[:4]

        # An open-ended range runs to the end of the clip.
        r = client.get("/conversations/11111111-1111-4111-8111-111111111111/audio", headers={"Range": "bytes=4-"})
        assert r.status_code == 206
        assert r.headers["content-range"] == f"bytes 4-{size - 1}/{size}"
        assert r.content == full[4:]

        # A suffix range returns the final N bytes.
        r = client.get("/conversations/11111111-1111-4111-8111-111111111111/audio", headers={"Range": "bytes=-5"})
        assert r.status_code == 206
        assert r.headers["content-range"] == f"bytes {size - 5}-{size - 1}/{size}"
        assert r.content == full[-5:]


def test_audio_unsatisfiable_or_malformed_range_falls_back_to_full() -> None:
    # RFC 7233 lets the server ignore a Range it can't (or won't) satisfy; we
    # serve the whole clip with 200 rather than erroring the player.
    with TestClient(app) as client:
        _make_conversation("11111111-1111-4111-8111-111111111111", "spoken words", with_audio=True)
        full = client.get("/conversations/11111111-1111-4111-8111-111111111111/audio").content
        for bad in ("bytes=99999-100000", "bytes=abc-def", "kilobytes=0-1", "bytes=5-1"):
            r = client.get("/conversations/11111111-1111-4111-8111-111111111111/audio", headers={"Range": bad})
            assert r.status_code == 200, bad
            assert r.content == full


def test_delete_removes_transcript_and_audio() -> None:
    with TestClient(app) as client:
        _make_conversation("11111111-1111-4111-8111-111111111111", "delete me", with_audio=True)
        key = "default/11111111-1111-4111-8111-111111111111.wav"
        assert get_audio_store().exists(key)

        assert client.delete("/conversations/11111111-1111-4111-8111-111111111111").status_code == 204
        assert client.get("/conversations/11111111-1111-4111-8111-111111111111").status_code == 404
        assert not get_audio_store().exists(key)

        assert client.delete("/conversations/11111111-1111-4111-8111-111111111111").status_code == 404


def test_legacy_status_row_does_not_break_the_listing() -> None:
    """A conversation stored by an older build (status 'processing', from the
    re-process pipeline that no longer exists) used to fail response validation and
    500 the whole listing, hiding every conversation in the household — including
    freshly recorded ones (XERK-58)."""
    with TestClient(app) as client:
        _make_conversation("44444444-4444-4444-8444-444444444444", "recorded under an older build")
        _make_conversation("55555555-5555-4555-8555-555555555555", "recorded just now")
        # Simulate the upgraded-database row: a status this build doesn't know.
        get_conversation_store().get("default", "44444444-4444-4444-8444-444444444444").status = "processing"  # type: ignore[union-attr,assignment]

        r = client.get("/conversations")
        assert r.status_code == 200
        rows = {c["id"]: c for c in r.json()}
        assert set(rows) == {"44444444-4444-4444-8444-444444444444", "55555555-5555-4555-8555-555555555555"}
        # Finished (it has an end time), so it reads as ready rather than live.
        assert rows["44444444-4444-4444-8444-444444444444"]["status"] == "ready"

        detail = client.get("/conversations/44444444-4444-4444-8444-444444444444")
        assert detail.status_code == 200 and detail.json()["status"] == "ready"
