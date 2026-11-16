"""Re-captioning a stored conversation into a copy (XERK-2958)."""

from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator

import pytest

import api.reprocess as rp
from api.contract import CaptionFinal, CaptionPartial, Lang
from api.persistence import get_audio_store, get_conversation_store
from api.persistence.audio import InMemoryAudioStore, audio_key
from api.persistence.conversations import InMemoryConversationStore
from api.persistence.models import Segment


@pytest.fixture(autouse=True)
def _clean() -> None:
    convs, audio = get_conversation_store(), get_audio_store()
    assert isinstance(convs, InMemoryConversationStore) and isinstance(audio, InMemoryAudioStore)
    convs._by_household.clear()
    audio._blobs.clear()


def _wav(seconds: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(int(32000 * seconds)))
    return buf.getvalue()


class _Scripted:
    """Stands in for the live transcriber: records the audio, answers two speakers' turns."""

    def __init__(self) -> None:
        self.pushed = 0
        self.closed = False

    async def push(self, pcm: bytes) -> None:
        self.pushed += len(pcm)

    async def flush(self) -> None: ...

    async def close(self) -> None:
        self.closed = True

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        while not self.closed:
            import asyncio

            await asyncio.sleep(0)
        for i, (text, lang, spk) in enumerate(
            [("hola qué tal", Lang.es, "Speaker 1"), ("fine thanks", Lang.en, "Speaker 2")]
        ):
            yield CaptionFinal(
                type="caption.final",
                segmentId=f"s{i}",
                text=text,
                lang=lang,
                startMs=i * 1000,
                endMs=i * 1000 + 900,
                speaker=spk,
            )


class _Upper:
    def translate(self, text: str, *, source_lang: str | None = None, run_lang=None) -> str:
        return text.upper()


def _seed() -> None:
    store, audio = get_conversation_store(), get_audio_store()
    store.create("default", "orig", owner="u1", mic_source="phone-microphone")
    store.add_segment("default", "orig", Segment("old", "unlabelled", 0, 900, "es"))
    audio.put(audio_key("default", "orig"), _wav(2.0))
    store.set_audio_key("default", "orig", audio_key("default", "orig"))
    store.finish("default", "orig")


def test_a_reprocess_writes_a_labelled_copy_and_leaves_the_original(monkeypatch) -> None:
    _seed()
    seen = {}

    def make(lang=None, **kw):
        seen.update(kw)
        seen["t"] = _Scripted()
        return seen["t"]

    monkeypatch.setattr(rp, "make_transcriber", make)
    monkeypatch.setattr(rp, "make_translator", lambda: _Upper())
    r = rp.reprocess("default", "orig")

    store, audio = get_conversation_store(), get_audio_store()
    copy = store.get("default", r.copy_id)
    assert copy is not None and copy.owner == "u1" and copy.status == "ready"
    assert [(s.text, s.speaker, s.translation) for s in copy.segments] == [
        ("hola qué tal", "Speaker 1", "HOLA QUÉ TAL"),
        ("fine thanks", "Speaker 2", None),
    ]
    assert audio.get(copy.audio_key) == audio.get(audio_key("default", "orig"))
    assert copy.audio_key != audio_key("default", "orig")
    orig = store.get("default", "orig")
    assert [s.text for s in orig.segments] == ["unlabelled"]
    # Keyed by the copy (its own speaker numbering), fed the whole recording, finals only.
    assert seen["conversation_id"] == r.copy_id and seen["t"].pushed == 64000
    assert seen["partials"] is False
    assert (r.segments, r.speakers, r.translated) == (2, 2, 1)


def test_a_conversation_without_audio_is_refused() -> None:
    get_conversation_store().create("default", "noaudio")
    with pytest.raises(LookupError):
        rp.reprocess("default", "noaudio")
    with pytest.raises(LookupError):
        rp.reprocess("default", "missing")


def test_audio_in_another_format_is_refused() -> None:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(64))
    with pytest.raises(ValueError):
        rp._pcm(buf.getvalue())


def test_main_prints_one_line_per_conversation(monkeypatch, capsys) -> None:
    monkeypatch.setattr(rp, "reprocess", lambda h, c: rp.Result(c, "new-" + c, 3, 2, 1))
    rp.main(["a", "b"])
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "a -> new-a: 3 turns, 2 speakers, 1 translated",
        "b -> new-b: 3 turns, 2 speakers, 1 translated",
    ]
