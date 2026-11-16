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
    """A translator whose output shares no words with its input (so it is never an echo)."""

    def translate(self, text: str, *, source_lang: str | None = None, run_lang=None) -> str:
        return {"hola qué tal": "hello how are you"}.get(text, "something else entirely")


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
        ("hola qué tal", "Speaker 1", "hello how are you"),
        ("fine thanks", "Speaker 2", None),
    ]
    assert audio.get(copy.audio_key) == audio.get(audio_key("default", "orig"))
    assert copy.audio_key != audio_key("default", "orig")
    orig = store.get("default", "orig")
    assert [s.text for s in orig.segments] == ["unlabelled"]
    # Keyed by the copy (its own speaker numbering), fed the whole recording, finals only.
    assert seen["conversation_id"] == r.copy_id and seen["t"].pushed == 64000
    assert seen["recaption"] is True
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


def test_main_runs_every_id_and_fails_if_any_did(monkeypatch, capsys) -> None:
    def fake(h: str, c: str) -> rp.Result:
        if c == "bad":
            raise LookupError("no conversation bad")
        return rp.Result(c, "new-" + c, 3, 2, 1)

    monkeypatch.setattr(rp, "reprocess", fake)
    assert rp.main(["a", "bad", "b"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "a -> new-a: 3 turns, 2 speakers, 1 translated",
        "bad: FAILED, no copy kept: no conversation bad",
        "b -> new-b: 3 turns, 2 speakers, 1 translated",
    ]
    assert rp.main(["a"]) == 0


def _copies() -> list[str]:
    store = get_conversation_store()
    return [c for c in store._conversations("default") if c != "orig"]


def _patch(monkeypatch, *, transcriber=None, translator=None) -> None:
    monkeypatch.setattr(rp, "make_transcriber", lambda lang=None, **kw: transcriber or _Scripted())
    monkeypatch.setattr(rp, "make_translator", lambda: translator or _Upper())


def test_a_failure_while_writing_removes_the_partial_copy(monkeypatch) -> None:
    """QA: a copy cut short mid-write was later finished by the stale sweep and listed as a
    normal recording with half its transcript."""
    _seed()
    _patch(monkeypatch)
    store = get_conversation_store()
    real = store.add_segment
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("database went away")
        return real(*a, **k)

    monkeypatch.setattr(store, "add_segment", flaky)
    with pytest.raises(OSError):
        rp.reprocess("default", "orig")
    assert _copies() == []
    assert list(get_audio_store()._blobs) == [audio_key("default", "orig")]


def test_no_turns_from_stt_writes_no_copy(monkeypatch) -> None:
    class Silent(_Scripted):
        async def results(self):
            while not self.closed:
                import asyncio

                await asyncio.sleep(0)
            if False:  # pragma: no cover - makes this an async generator
                yield

    _seed()
    _patch(monkeypatch, transcriber=Silent())
    with pytest.raises(rp.ReprocessError, match="no turns"):
        rp.reprocess("default", "orig")
    assert _copies() == []


def test_a_failed_diarizer_call_writes_no_copy(monkeypatch) -> None:
    """QA: one timed-out call left 128 of 133 turns unlabelled and still reported success."""

    class Diar:
        failures = 1

    t = _Scripted()
    t._diarizer = Diar()
    _seed()
    _patch(monkeypatch, transcriber=t)
    with pytest.raises(rp.ReprocessError, match="diarizer"):
        rp.reprocess("default", "orig")
    assert _copies() == []


def test_a_lost_translation_writes_no_copy(monkeypatch) -> None:
    class Down:
        calls = 0

        def translate(self, text, **kw):
            Down.calls += 1
            return None

    _seed()
    _patch(monkeypatch, translator=Down())
    with pytest.raises(rp.ReprocessError, match="translation"):
        rp.reprocess("default", "orig")
    assert _copies() == [] and Down.calls == 2  # retried once


def _final(i: int, text: str, lang: Lang | None) -> CaptionFinal:
    return CaptionFinal(
        type="caption.final", segmentId=f"f{i}", text=text, lang=lang, startMs=i, endMs=i + 1
    )


def test_translations_follow_the_live_rules() -> None:
    """Echoes are dropped (session._same_text); an undecided turn inherits an open run; an
    English turn closes it."""

    class Tr:
        def __init__(self) -> None:
            self.seen = []

        def translate(self, text, *, source_lang=None, run_lang=None):
            self.seen.append((text, source_lang, run_lang))
            return text if text.startswith("echo") else {"hola": "hello"}.get(text, "the planets")

    tr = Tr()
    out = rp._translate(
        [
            _final(0, "hola", Lang.es),
            _final(1, "Mercurio, Venus", None),
            _final(2, "echo this sentence exactly", Lang.es),
            _final(3, "ok then", Lang.en),
            _final(4, "Marte", None),
        ],
        tr,
    )
    assert out == ["hello", "the planets", None, None, None]
    assert tr.seen[1] == ("Mercurio, Venus", None, "es")
    assert len(tr.seen) == 3  # nothing after the English turn closed the run
    assert rp._translate([_final(0, "hola", Lang.es)], None) == [None]
