"""Session-level live-translation behaviour (XERK-160): non-English finals are
translated off the caption path and paired to their segment, consecutive
non-English turns form a run that suppresses cues, and the run's end emits
`translation.done` — all against the model-free stub."""

from __future__ import annotations

import asyncio
import time

import pytest

from api.config import settings
from api.contract import (
    CaptionFinal,
    Cue,
    MicSource,
    ServerMessage,
    Translation,
    TranslationDone,
)
from api.cue.base import GeneratedCue
from api.metrics import metrics
from api.persistence import Segment, get_conversation_store
from api.session import Session


def _final(
    text: str, *, segment_id: str = "s1", lang: str | None = "en", end_ms: int = 2000
) -> CaptionFinal:
    return CaptionFinal(
        type="caption.final",
        segmentId=segment_id,
        text=text,
        startMs=0,
        endMs=end_ms,
        lang=lang,
    )


async def _fresh_session(sent: list[ServerMessage]) -> Session:
    async def sender(m: ServerMessage) -> None:
        sent.append(m)

    session = Session(sender, household="default")
    await session.start(mic_source=MicSource("phone-microphone"), source_lang=None)
    return session


async def _drain_translations(session: Session) -> None:
    assert session._translation_queue is not None
    await session._translation_queue.join()


def _translations(sent: list[ServerMessage]) -> list[Translation]:
    return [m for m in sent if isinstance(m, Translation)]


def _dones(sent: list[ServerMessage]) -> list[TranslationDone]:
    return [m for m in sent if isinstance(m, TranslationDone)]


def _cues(sent: list[ServerMessage]) -> list[Cue]:
    return [m for m in sent if isinstance(m, Cue)]


def test_non_english_final_is_translated_and_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        # Deliver the final through the same path the pump uses so the segment is
        # persisted before its translation lands.
        final = _final("hola, ¿qué tal?", segment_id="seg-es", lang="es")
        await session._send(final)
        session._conversations.add_segment(
            "default",
            session.session_id,
            Segment(
                segment_id=final.segmentId,
                text=final.text,
                start_ms=final.startMs,
                end_ms=final.endMs,
                lang="es",
            ),
        )
        session._consider_translation(final)
        await _drain_translations(session)

        got = _translations(sent)
        assert len(got) == 1
        assert got[0].segmentId == "seg-es"
        assert got[0].sourceLang is not None and got[0].sourceLang.value == "es"
        assert "hola" in got[0].text

        conv = get_conversation_store().get("default", session.session_id)
        assert conv is not None
        assert conv.segments[0].translation == got[0].text

        await session.close()

    asyncio.run(run())


def test_no_translation_when_backend_off() -> None:
    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        assert session._translator is None
        session._consider_translation(_final("hola", lang="es"))
        await session.close()
        assert _translations(sent) == []
        assert _dones(sent) == []

    asyncio.run(run())


def test_english_final_never_translates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("hello there", lang="en"))
        await _drain_translations(session)
        await session.close()
        assert _translations(sent) == []
        # No run was ever open, so nothing to close either.
        assert _dones(sent) == []

    asyncio.run(run())


def test_english_turn_closes_the_run_after_its_translations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("hola", segment_id="a", lang="es"))
        session._consider_translation(_final("¿cómo estás?", segment_id="b", lang="es"))
        session._consider_translation(_final("I'm fine, thanks", segment_id="c", lang="en"))
        await _drain_translations(session)
        await session.close()

        got = _translations(sent)
        assert [t.segmentId for t in got] == ["a", "b"]
        dones = _dones(sent)
        assert len(dones) == 1
        # done arrives AFTER the run's last translation — the queue orders it.
        assert sent.index(dones[0]) > sent.index(got[-1])

    asyncio.run(run())


def test_unknown_lang_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("hola", segment_id="a", lang="es"))
        assert session._translation_active
        # A turn with no detected language neither translates nor closes the run.
        session._consider_translation(_final("mumble", segment_id="b", lang=None))
        assert session._translation_active
        await _drain_translations(session)
        await session.close()
        assert [t.segmentId for t in _translations(sent)] == ["a"]

    asyncio.run(run())


def test_silence_hold_expiry_closes_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "translation_hold_ms", 0)

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("hola", lang="es"))
        # The 0ms hold fires on the next loop turns; wait for it to close the run.
        for _ in range(50):
            if not session._translation_active:
                break
            await asyncio.sleep(0.01)
        await _drain_translations(session)
        assert not session._translation_active
        assert len(_dones(sent)) == 1
        await session.close()

    asyncio.run(run())


def test_partial_activity_restarts_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "translation_hold_ms", 60_000)

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("hola", lang="es"))
        first_hold = session._translation_hold
        assert first_hold is not None
        # Speech still flowing (a partial) replaces the pending hold with a fresh one.
        session._touch_translation_hold()
        assert session._translation_hold is not first_hold
        assert first_hold.cancelled() or first_hold.cancelling()
        assert session._translation_active
        await session.close()

    asyncio.run(run())


def test_cues_suppressed_during_run_and_resume_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "cue_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        # Open the run, then a cue-worthy final lands mid-run: no cue.
        session._consider_translation(_final("hola", segment_id="a", lang="es"))
        session._consider_cue(_final("how far is the sun?", segment_id="b"))
        await asyncio.gather(*list(session._cue_tasks))
        assert _cues(sent) == []
        # An English turn closes the run; cues trigger again afterwards.
        session._consider_translation(_final("back to English", segment_id="c", lang="en"))
        session._consider_cue(_final("how far is the moon?", segment_id="c"))
        await asyncio.gather(*list(session._cue_tasks))
        await _drain_translations(session)
        await session.close()
        assert len(_cues(sent)) == 1

    asyncio.run(run())


def test_inflight_cue_dropped_when_run_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "cue_backend", "stub")

    class SlowGenerator:
        def generate(self, transcript, *, avoid_cues=(), evidence=()):
            time.sleep(0.05)  # long enough for the run to open mid-generation
            return GeneratedCue(title="Sun", body="149.6 million km away.")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._cue_generator = SlowGenerator()
        session._consider_cue(_final("how far is the sun?", segment_id="a"))
        # The run opens while the cue model is still generating.
        session._consider_translation(_final("hola", segment_id="b", lang="es"))
        await asyncio.gather(*list(session._cue_tasks))
        await _drain_translations(session)
        await session.close()
        assert _cues(sent) == []
        assert metrics.snapshot()["counters"].get("cue.translation_drops") == 1

    asyncio.run(run())


def test_translator_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    class ExplodingTranslator:
        def translate(self, text: str, *, source_lang: str | None = None) -> str | None:
            raise RuntimeError("boom")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._translator = ExplodingTranslator()
        session._consider_translation(_final("hola", lang="es"))
        await _drain_translations(session)
        await session.close()
        assert _translations(sent) == []
        assert metrics.snapshot()["counters"].get("translation.errors") == 1

    asyncio.run(run())


def test_empty_translation_emits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("   ", lang="es"))
        await _drain_translations(session)
        await session.close()
        assert _translations(sent) == []

    asyncio.run(run())


def test_close_drains_pending_translations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("hasta luego", segment_id="tail", lang="es"))
        # Close immediately: the pending translation still goes out, then the
        # still-open run is closed as part of teardown.
        await session.close()
        got = _translations(sent)
        assert [t.segmentId for t in got] == ["tail"]
        assert len(_dones(sent)) == 1

    asyncio.run(run())
