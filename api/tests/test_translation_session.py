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


def test_unknown_lang_outside_a_run_decides_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        # No run open: a turn with no detected language neither translates nor
        # opens one — translating it would be a guess.
        session._consider_translation(_final("mumble mumble", segment_id="a", lang=None))
        assert not session._translation_active
        await _drain_translations(session)
        await session.close()
        assert _translations(sent) == []

    asyncio.run(run())


def test_unknown_lang_mid_run_inherits_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # The session-a6ef5cad gap: "Mercurio, Venus, Tierra, Marte." between two
    # Spanish turns carries no textual evidence, but the open run says the
    # speaker is mid-Spanish — the turn is translated with the rest, without a
    # claimed source language, and the run stays open.
    monkeypatch.setattr(settings, "translation_backend", "stub")

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._consider_translation(_final("los planetas", segment_id="a", lang="es"))
        assert session._translation_active
        session._consider_translation(
            _final("Mercurio, Venus, Tierra, Marte.", segment_id="b", lang=None)
        )
        assert session._translation_active
        await _drain_translations(session)
        await session.close()
        got = _translations(sent)
        assert [t.segmentId for t in got] == ["a", "b"]
        # The inherited turn claims no source language — the model reads the text.
        assert got[1].sourceLang is None

    asyncio.run(run())


def test_inherited_turn_carries_the_run_language(monkeypatch: pytest.MonkeyPatch) -> None:
    # XERK-1354: a completion-prompt backend must name a source language. The
    # inherited turn has none of its own, so the session hands the translator the
    # run's language; an opener passes its own, and a closed run forgets it.
    monkeypatch.setattr(settings, "translation_backend", "stub")
    calls: list[tuple[str, str | None, str | None]] = []

    class Recorder:
        def translate(
            self, text: str, *, source_lang: str | None = None, run_lang: str | None = None
        ) -> str | None:
            calls.append((text, source_lang, run_lang))
            return f"EN {text}"

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._translator = Recorder()
        session._consider_translation(_final("los planetas", segment_id="a", lang="pt"))
        session._consider_translation(_final("hola amigo", segment_id="b", lang="es"))
        session._consider_translation(_final("Mercurio, Venus.", segment_id="c", lang=None))
        session._consider_translation(_final("ok then", segment_id="d", lang="en"))
        assert session._translation_run_lang is None
        await _drain_translations(session)
        await session.close()

    asyncio.run(run())
    # The run language follows the LAST non-English turn, captured at queue time.
    assert calls == [
        ("los planetas", "pt", "pt"),
        ("hola amigo", "es", "es"),
        ("Mercurio, Venus.", None, "es"),
    ]


def test_echoed_translation_is_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    # An English turn that reached the queue as an ambiguous run-continuation
    # comes back unchanged from the model ("return it unchanged"); rendering it
    # would just duplicate the caption, so it is dropped.
    monkeypatch.setattr(settings, "translation_backend", "stub")

    class EchoTranslator:
        def translate(
            self, text: str, *, source_lang: str | None = None, run_lang: str | None = None
        ) -> str | None:
            return f"  {text.upper()}  "  # cosmetic differences only

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        session._translator = EchoTranslator()
        session._consider_translation(_final("hola", segment_id="a", lang="es"))
        session._consider_translation(_final("what is happening", segment_id="b", lang=None))
        await _drain_translations(session)
        await session.close()
        assert _translations(sent) == []
        assert metrics.snapshot()["counters"].get("translation.echo_drops") == 2

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
        def translate(
            self, text: str, *, source_lang: str | None = None, run_lang: str | None = None
        ) -> str | None:
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


def test_slow_final_decode_keeps_the_run_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """XERK-1377: a final whose decode outlasts the hold must still land inside the
    run. The hold only restarted on captions, so a slow whole-turn decode (Parakeet
    finals at ~6 s against a 3 s hold) read as silence: the run closed first and the
    inherited (undetected-language) turn arrived to no run and was dropped."""
    import numpy as np

    import api.session as session_mod
    from api.stt.engine import EngineResult
    from api.stt.streaming import StreamingTranscriber

    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "translation_hold_ms", 150)

    class SlowSecondFinal:
        """Turn 1 decodes fast as Spanish; turn 2 is an undecidable proper-noun list
        whose final decode takes longer than the hold."""

        def __init__(self) -> None:
            self.finals = 0

        def transcribe(
            self, samples: np.ndarray, *, language: str | None, want_words: bool = True
        ) -> EngineResult:
            if samples.size == 0 or float(np.abs(samples).max()) == 0.0:
                return EngineResult(text="", words=[], language=None)
            self.finals += 1
            if self.finals == 1:
                return EngineResult(
                    text="hola, ¿cómo estás? me llamo Juan y vivo en Madrid",
                    words=[],
                    language="es",
                )
            time.sleep(0.5)  # > the 150 ms hold
            return EngineResult(text="Mercurio, Venus, Tierra, Marte.", words=[], language=None)

    def transcriber(*_a, **_kw) -> StreamingTranscriber:
        # Partials off: only finals drive the hold, as when intake lags (XERK-1414).
        return StreamingTranscriber(
            SlowSecondFinal(), partial_interval_ms=60_000, final_words=False
        )

    monkeypatch.setattr(session_mod, "make_transcriber", transcriber)

    def pcm(ms: int, amplitude: int) -> bytes:
        return np.full(16 * ms, amplitude, dtype=np.int16).tobytes()

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        for _ in range(2):
            await session.on_audio(pcm(600, 3000))
            await session.on_audio(pcm(600, 0))
            await asyncio.sleep(0.05)  # let the pump deliver the final
        # Finals decode off the audio path (XERK-1424): wait for the slow one to be
        # decoded and handled. The 150 ms hold expires many times meanwhile.
        assert session._transcriber is not None
        while session._transcriber.finalizing:
            await asyncio.sleep(0.01)
        await _drain_translations(session)
        translated = [t.segmentId for t in _translations(sent)]
        finals = [m.segmentId for m in sent if isinstance(m, CaptionFinal)]
        assert len(finals) == 2
        # Both turns translated inside one run: the hold didn't close it mid-decode.
        assert translated == finals
        assert _dones(sent) == []
        await session.close()

    asyncio.run(run())


def test_dead_pump_does_not_hold_the_run_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # XERK-1377: a final still marked in flight is only worth waiting for while the
    # pump can deliver it; once the pump has died the hold closes the run as before.
    monkeypatch.setattr(settings, "translation_backend", "stub")
    monkeypatch.setattr(settings, "translation_hold_ms", 0)

    class StuckFinalizing:
        finalizing = True

    async def run() -> None:
        sent: list[ServerMessage] = []
        session = await _fresh_session(sent)
        assert session._pump is not None
        session._pump.cancel()
        await asyncio.gather(session._pump, return_exceptions=True)
        # A pump that exited (as _pump_results does after logging an STT failure).
        session._pump = asyncio.create_task(asyncio.sleep(0))
        await session._pump
        real = session._transcriber
        session._transcriber = StuckFinalizing()  # type: ignore[assignment]
        session._consider_translation(_final("hola", lang="es"))
        for _ in range(50):
            if not session._translation_active:
                break
            await asyncio.sleep(0.01)
        assert not session._translation_active
        session._transcriber = real
        await _drain_translations(session)
        assert len(_dones(sent)) == 1
        await session.close()

    asyncio.run(run())
