"""Conversation + audio stores, WAV helpers, and backend factories."""

from __future__ import annotations

import pytest

from pathlib import Path

from api.persistence import audio_key, pcm16_to_wav, wav_to_pcm16
from api.persistence.audio import InMemoryAudioStore, LocalDiskAudioStore
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.models import Conversation, Segment, coerce_status, utcnow


def _seg(sid: str, text: str, start: int, end: int) -> Segment:
    return Segment(segment_id=sid, text=text, start_ms=start, end_ms=end, lang="en")


def test_conversation_model_derivations() -> None:
    conv = Conversation(id="c1", household="h")
    assert conv.transcript == "" and conv.duration_ms == 0
    conv.segments = [
        Segment("s1", "hello there", 0, 1000),
        Segment("s2", "general kenobi", 1200, 3000),
        Segment("s3", "again", 3000, 3500),
    ]
    assert conv.transcript == "hello there\ngeneral kenobi\nagain"
    assert conv.duration_ms == 3500


def test_store_create_is_idempotent_and_upserts_segments() -> None:
    store = InMemoryConversationStore()
    a = store.create("h", "c1", mic_source="phone-microphone", source_lang="en")
    b = store.create("h", "c1")  # same id -> same record (resume)
    assert a is b and a.mic_source == "phone-microphone"

    store.add_segment("h", "c1", _seg("s1", "first", 0, 1000))
    store.add_segment("h", "c1", _seg("s1", "first (revised)", 0, 1000))  # upsert
    store.add_segment("h", "c1", _seg("s2", "second", 1000, 2000))
    conv = store.get("h", "c1")
    assert [s.text for s in conv.segments] == ["first (revised)", "second"]


def test_store_finish_and_audio_key() -> None:
    store = InMemoryConversationStore()
    store.create("h", "c1")
    store.add_segment("h", "c1", _seg("s1", "rough", 0, 1000))

    store.set_audio_key("h", "c1", "h/c1.wav")
    finished = store.finish("h", "c1", status="ready")
    assert finished.status == "ready" and finished.ended_at is not None
    assert store.get("h", "c1").audio_key == "h/c1.wav"

    store.clear_audio_key("h", "c1")
    assert store.get("h", "c1").audio_key is None


def test_store_missing_targets_are_safe_noops() -> None:
    store = InMemoryConversationStore()
    # Operations against an unknown conversation must not raise.
    store.add_segment("h", "ghost", _seg("s1", "x", 0, 1))
    store.set_audio_key("h", "ghost", "k")
    store.clear_audio_key("h", "ghost")
    assert store.finish("h", "ghost") is None
    assert store.get("h", "ghost") is None
    assert store.delete("h", "ghost") is False


def test_store_list_is_newest_first_and_paginates() -> None:
    store = InMemoryConversationStore()
    for i in range(5):
        conv = store.create("h", f"c{i}")
        conv.started_at = utcnow().replace(microsecond=i)
    listed = store.list("h", limit=2, offset=1)
    assert [c.id for c in listed] == ["c3", "c2"]
    # Household isolation: another household sees nothing.
    assert store.list("other") == []


def test_store_search_ranks_by_match_count() -> None:
    store = InMemoryConversationStore()
    store.create("h", "c1")
    store.add_segment("h", "c1", _seg("s1", "budget budget planning", 0, 1000))
    store.create("h", "c2")
    store.add_segment("h", "c2", _seg("s1", "weekend plans", 0, 1000))
    store.create("h", "c3")
    store.add_segment("h", "c3", _seg("s1", "the budget review", 0, 1000))

    hits = store.search("h", "budget")
    assert [c.id for c in hits] == ["c1", "c3"]  # c1 has two matches, ranked first
    assert store.search("h", "nonexistent") == []
    # Empty query falls back to the recency list.
    assert {c.id for c in store.search("h", "   ")} == {"c1", "c2", "c3"}


def test_store_households_lists_tenants() -> None:
    store = InMemoryConversationStore()
    store.create("hA", "c1")
    store.create("hB", "c2")
    assert set(store.households()) == {"hA", "hB"}


def test_audio_store_roundtrip_and_delete() -> None:
    store = InMemoryAudioStore()
    assert store.get("k") is None and store.exists("k") is False
    store.put("k", b"\x01\x02")
    assert store.get("k") == b"\x01\x02" and store.exists("k") is True
    assert store.delete("k") is True and store.delete("k") is False


def test_audio_key_is_household_namespaced() -> None:
    assert audio_key("home", "abc") == "home/abc.wav"


def test_disk_audio_store_roundtrip_and_delete(tmp_path: Path) -> None:
    store = LocalDiskAudioStore(tmp_path)
    key = audio_key("home", "abc")
    assert store.get(key) is None and store.exists(key) is False
    assert store.delete(key) is False  # deleting a missing key is a safe no-op
    store.put(key, b"\x01\x02")
    assert store.get(key) == b"\x01\x02" and store.exists(key) is True
    # The household segment of the key becomes an on-disk subdirectory.
    assert (tmp_path / "home" / "abc.wav").read_bytes() == b"\x01\x02"
    assert store.delete(key) is True and store.delete(key) is False


def test_disk_audio_store_put_is_atomic_overwrite(tmp_path: Path) -> None:
    store = LocalDiskAudioStore(tmp_path)
    key = audio_key("home", "abc")
    store.put(key, b"first")
    store.put(key, b"second, longer")  # overwrite in place, no leftover temp files
    assert store.get(key) == b"second, longer"
    assert list((tmp_path / "home").glob("*.tmp")) == []


def test_disk_audio_store_ready_creates_root(tmp_path: Path) -> None:
    root = tmp_path / "audio"
    assert not root.exists()
    LocalDiskAudioStore(root).ready()  # readiness probe creates a missing root
    assert root.is_dir()


def test_disk_audio_store_rejects_keys_escaping_root(tmp_path: Path) -> None:
    store = LocalDiskAudioStore(tmp_path)
    with pytest.raises(ValueError, match="escapes store root"):
        store.put("../evil.wav", b"x")
    with pytest.raises(ValueError, match="escapes store root"):
        store.get("../../etc/passwd")


def test_wav_roundtrip_preserves_pcm() -> None:
    pcm = bytes(range(0, 256)) * 4  # arbitrary even-length PCM
    wav = pcm16_to_wav(pcm)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert wav_to_pcm16(wav) == pcm


def test_conversation_store_factory_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings
    from api.persistence import _build_conversation_store
    from api.persistence.postgres import SqlConversationStore

    monkeypatch.setattr(settings, "persistence_backend", "off")
    assert _build_conversation_store() is None
    monkeypatch.setattr(settings, "persistence_backend", "memory")
    assert isinstance(_build_conversation_store(), InMemoryConversationStore)
    monkeypatch.setattr(settings, "persistence_backend", "postgres")
    assert isinstance(_build_conversation_store(), SqlConversationStore)
    monkeypatch.setattr(settings, "persistence_backend", "bogus")
    with pytest.raises(ValueError, match="unknown persistence backend"):
        _build_conversation_store()


def test_audio_store_factory_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings
    from api.persistence import _build_audio_store

    monkeypatch.setattr(settings, "audio_backend", "off")
    assert _build_audio_store() is None
    monkeypatch.setattr(settings, "audio_backend", "memory")
    assert isinstance(_build_audio_store(), InMemoryAudioStore)
    monkeypatch.setattr(settings, "audio_backend", "disk")
    assert isinstance(_build_audio_store(), LocalDiskAudioStore)
    monkeypatch.setattr(settings, "audio_backend", "bogus")
    with pytest.raises(ValueError, match="unknown audio backend"):
        _build_audio_store()


def test_coerce_status_normalizes_values_from_older_schemas() -> None:
    # Current vocabulary passes through untouched.
    assert coerce_status("live") == "live"
    assert coerce_status("ready", ended=True) == "ready"
    # Anything else (a status written before this build, or a NULL) is read from
    # whether the conversation has an end time — never handed on as-is (XERK-58).
    assert coerce_status("processing", ended=True) == "ready"
    assert coerce_status("processing", ended=False) == "live"
    assert coerce_status(None) == "live"
