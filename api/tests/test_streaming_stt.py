"""Phase 1: realtime streaming STT windowing, VAD and the backend factory.

The model is faked so the windowing/VAD/cadence logic is tested deterministically
with no GPU or model download.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from api.contract import CaptionFinal, CaptionPartial
from api.stt import make_transcriber
from api.stt.engine import (
    EngineResult,
    EngineWord,
    pcm16_to_float32,
    rms,
)
from api.stt.streaming import StreamingTranscriber, _lang, _ms_to_bytes


class FakeEngine:
    """Returns fixed text for audio with energy, empty for silence."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        self.calls += 1
        if samples.size == 0 or float(np.abs(samples).max()) == 0.0:
            return EngineResult(text="", words=[], language=language)
        return EngineResult(
            text="hello world",
            words=[
                EngineWord("hello", 0.0, 0.5, 0.9),
                EngineWord("world", 0.5, 1.0, 0.8),
            ],
            language="en",
        )


def _pcm(ms: int, *, amplitude: int) -> bytes:
    """ms of 16 kHz s16le mono PCM at a constant amplitude (0 == silence)."""
    n = 16000 * ms // 1000
    return np.full(n, amplitude, dtype=np.int16).tobytes()


def _drain(t: StreamingTranscriber) -> list[CaptionPartial | CaptionFinal]:
    out: list[CaptionPartial | CaptionFinal] = []
    while not t._queue.empty():
        out.append(t._queue.get_nowait())
    return out


# ----- helpers --------------------------------------------------------------


def test_pcm_and_rms_helpers() -> None:
    assert pcm16_to_float32(b"").size == 0
    assert rms(np.zeros(0, dtype=np.float32)) == 0.0
    samples = pcm16_to_float32(_pcm(10, amplitude=16384))
    assert rms(samples) == pytest.approx(0.5, abs=1e-3)


def test_lang_mapping() -> None:
    assert _lang("en") is not None
    assert _lang("es") is not None
    assert _lang("fr") is not None
    # An unsupported code (outside the contract Lang enum) maps to None.
    assert _lang("xx") is None
    assert _lang(None) is None


# ----- streaming behaviour --------------------------------------------------


def test_partials_then_final_on_silence() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=200,
            silence_ms=300,
            min_segment_ms=100,
            max_segment_ms=5000,
        )
        for _ in range(3):  # 300ms speech -> at least one partial (200ms cadence)
            await t.push(_pcm(100, amplitude=4000))
        msgs = _drain(t)
        partials = [m for m in msgs if isinstance(m, CaptionPartial)]
        assert partials and partials[0].text == "hello world"
        assert partials[0].lang is not None

        for _ in range(3):  # 300ms trailing silence -> finalize
            await t.push(_pcm(100, amplitude=0))
        finals = [m for m in _drain(t) if isinstance(m, CaptionFinal)]
        assert len(finals) == 1
        f = finals[0]
        assert f.text == "hello world"
        assert f.startMs == 0
        assert f.endMs == 600  # 300ms speech + 300ms silence
        assert f.words is not None
        assert f.words[0].text == "hello" and f.words[0].startMs == 0
        assert f.words[1].startMs == 500  # offset from segment start

    asyncio.run(run())


def test_partial_with_empty_text_is_suppressed() -> None:
    class EmptyEngine:
        def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
            return EngineResult(text="   ", words=[], language=language)

    async def run() -> None:
        t = StreamingTranscriber(
            EmptyEngine(),
            language="en",
            partial_interval_ms=100,
            silence_ms=10000,
            max_segment_ms=10000,
        )
        await t.push(_pcm(200, amplitude=4000))  # speech triggers a partial
        assert not _drain(t)  # but the empty hypothesis is suppressed

    asyncio.run(run())


def test_legacy_partial_with_empty_text_is_suppressed() -> None:
    """Same suppression on the LocalAgreement-off path: a blank hypothesis emits
    no partial."""

    class EmptyEngine:
        def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
            return EngineResult(text="   ", words=[], language=language)

    async def run() -> None:
        t = StreamingTranscriber(
            EmptyEngine(),
            language="en",
            partial_interval_ms=100,
            silence_ms=10000,
            max_segment_ms=10000,
            local_agreement=False,
        )
        await t.push(_pcm(200, amplitude=4000))
        assert not _drain(t)

    asyncio.run(run())


def test_max_segment_forces_final() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=10000,  # never via cadence
            silence_ms=10000,  # never via silence
            max_segment_ms=300,
        )
        for _ in range(3):  # hits the 300ms max on the 3rd chunk
            await t.push(_pcm(100, amplitude=4000))
        finals = [m for m in _drain(t) if isinstance(m, CaptionFinal)]
        assert len(finals) == 1
        assert finals[0].endMs == 300

    asyncio.run(run())


def test_flush_finalizes_remainder() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng, language="en", partial_interval_ms=10000, silence_ms=10000, max_segment_ms=10000
        )
        await t.push(_pcm(200, amplitude=4000))
        assert not _drain(t)  # nothing emitted yet
        await t.flush()
        finals = [m for m in _drain(t) if isinstance(m, CaptionFinal)]
        assert len(finals) == 1 and finals[0].text == "hello world"

    asyncio.run(run())


def test_silence_only_emits_nothing_and_advances_time() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng, language="en", partial_interval_ms=10000, silence_ms=10000, max_segment_ms=300
        )
        for _ in range(3):  # 300ms pure silence -> max-segment finalize, empty result
            await t.push(_pcm(100, amplitude=0))
        assert not _drain(t)  # silence produces no caption
        assert t._segment_start_ms == 300  # but the clock still advanced

    asyncio.run(run())


def test_empty_push_is_ignored() -> None:
    async def run() -> None:
        t = StreamingTranscriber(FakeEngine())
        await t.push(b"")
        assert len(t._buf) == 0 and not _drain(t)

    asyncio.run(run())


def test_flush_noop_when_no_speech() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(eng)
        await t.flush()  # nothing buffered
        assert eng.calls == 0 and not _drain(t)

    asyncio.run(run())


def test_results_stream_and_close_sentinel() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng, language="en", partial_interval_ms=10000, silence_ms=10000, max_segment_ms=10000
        )
        await t.push(_pcm(200, amplitude=4000))
        await t.flush()

        received: list[CaptionPartial | CaptionFinal] = []

        async def consume() -> None:
            async for msg in t.results():
                received.append(msg)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)  # let the consumer drain the queued final
        await t.close()
        await task

        finals = [m for m in received if isinstance(m, CaptionFinal)]
        assert len(finals) == 1
        assert t._closed is True

    asyncio.run(run())


class RecordingEngine:
    """Records the sample count of every decode so we can assert what was decoded."""

    def __init__(self) -> None:
        self.sizes: list[int] = []

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        self.sizes.append(int(samples.size))
        return EngineResult(text="hello", words=[], language="en")


def test_legacy_partial_decodes_only_trailing_window() -> None:
    """With LocalAgreement off, a partial re-decodes at most partial_window_ms; a
    final decodes the whole segment — so partial latency stays bounded as a turn
    grows (master plan §10)."""

    async def run() -> None:
        eng = RecordingEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=500,
            partial_window_ms=1000,  # 16000 samples
            silence_ms=100000,
            max_segment_ms=100000,
            local_agreement=False,
        )
        for _ in range(20):  # 2000ms of speech -> partials at 500/1000/1500/2000ms
            await t.push(_pcm(100, amplitude=4000))
        partial_sizes = list(eng.sizes)
        # No partial ever decodes more than the trailing window, even once the
        # buffer (2000ms = 32000 samples) has grown well past it.
        assert partial_sizes and max(partial_sizes) == 16000

        await t.flush()  # the final decodes the whole 2000ms segment
        assert eng.sizes[-1] == 32000

    asyncio.run(run())


def test_local_agreement_partials_are_anchored_at_segment_start() -> None:
    """LocalAgreement needs a stable prefix, so its partials decode the WHOLE in-flight
    segment (ignoring partial_window_ms), growing with the turn — not a sliding window
    that would never line up between passes."""

    async def run() -> None:
        eng = RecordingEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=500,
            partial_window_ms=1000,  # would cap at 16000 — but LA ignores it
            silence_ms=100000,
            max_segment_ms=100000,
        )
        for _ in range(20):  # 2000ms of speech -> partials at 500/1000/1500/2000ms
            await t.push(_pcm(100, amplitude=4000))
        # Each partial decodes the whole segment so far: 8000, 16000, 24000, 32000.
        assert eng.sizes == [8000, 16000, 24000, 32000]

    asyncio.run(run())


def test_partial_window_zero_decodes_whole_segment() -> None:
    async def run() -> None:
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=200,
            partial_window_ms=0,  # legacy: whole-segment partials
            silence_ms=100000,
            max_segment_ms=100000,
        )
        for _ in range(4):
            await t.push(_pcm(100, amplitude=4000))
        assert [m for m in _drain(t) if isinstance(m, CaptionPartial)]

    asyncio.run(run())


def test_stt_inference_latency_is_recorded() -> None:
    """The caption-path inference time is measured so the §6 budget can be tuned."""
    from api.metrics import metrics

    async def run() -> None:
        metrics.reset()
        eng = FakeEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,
            silence_ms=300,
            min_segment_ms=100,
            max_segment_ms=5000,
        )
        for _ in range(3):  # speech -> a partial
            await t.push(_pcm(100, amplitude=4000))
        for _ in range(3):  # trailing silence -> a final
            await t.push(_pcm(100, amplitude=0))
        snap = metrics.snapshot()["latency_ms"]
        assert snap["stage.stt.partial_latency_ms"]["count"] >= 1
        assert snap["stage.stt.final_latency_ms"]["count"] >= 1
        metrics.reset()

    asyncio.run(run())


# ----- LocalAgreement-2 word-by-word partials (XERK-90) ---------------------


class ScriptedEngine:
    """Emits a fixed sequence of hypotheses, one per call — a growing utterance."""

    def __init__(self, script: list[str]) -> None:
        self.script = script
        self.calls = 0

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return EngineResult(text=self.script[idx], words=[], language="en")


def test_local_agreement_partials_grow_stably() -> None:
    """With LocalAgreement on (the default), the caption grows word by word and an
    already-shown word is never rewritten — the fix the ticket asks for."""

    async def run() -> None:
        eng = ScriptedEngine(["one", "one two", "one two three", "one two three four"])
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,  # one partial per 100ms push
            silence_ms=100000,
            max_segment_ms=100000,
        )
        texts: list[str] = []
        for _ in range(4):
            await t.push(_pcm(100, amplitude=4000))
            texts.extend(m.text for m in _drain(t) if isinstance(m, CaptionPartial))

        assert texts == ["one", "one two", "one two three", "one two three four"]
        # Each emitted caption extends the previous one — pure growth, no rewriting.
        for earlier, later in zip(texts, texts[1:]):
            assert later.startswith(earlier)
        # Only the last word trails as tentative; everything before it is committed.
        assert t._agreement is not None
        assert t._agreement.committed == ["one", "two", "three"]
        assert t._agreement.tentative == ["four"]

    asyncio.run(run())


def test_local_agreement_does_not_commit_a_revised_tail() -> None:
    """A word the model changes its mind about must not be shown as stable."""

    async def run() -> None:
        # The tail flips "beach" -> "bench" before settling — classic partial churn.
        eng = ScriptedEngine(["go to the beach", "go to the bench", "go to the bench now"])
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,
            silence_ms=100000,
            max_segment_ms=100000,
        )
        for _ in range(3):
            await t.push(_pcm(100, amplitude=4000))
            _drain(t)

        assert t._agreement is not None
        # "go to the" agreed every time; "bench" only settled once it repeated.
        assert t._agreement.committed == ["go", "to", "the", "bench"]
        assert t._agreement.tentative == ["now"]

    asyncio.run(run())


def test_local_agreement_resets_between_segments() -> None:
    async def run() -> None:
        eng = ScriptedEngine(["hello world"])
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,
            silence_ms=300,
            min_segment_ms=100,
            max_segment_ms=100000,
        )
        for _ in range(2):  # speech -> partials build up a committed prefix
            await t.push(_pcm(100, amplitude=4000))
        assert t._agreement is not None and t._agreement.committed == ["hello", "world"]
        for _ in range(3):  # trailing silence -> finalize, which resets the commit
            await t.push(_pcm(100, amplitude=0))
        assert t._agreement.committed == []
        assert t._agreement.tentative == []

    asyncio.run(run())


def test_local_agreement_off_emits_raw_window() -> None:
    """The toggle restores the legacy behaviour: each raw window hypothesis verbatim."""

    async def run() -> None:
        eng = ScriptedEngine(["alpha", "alpha beta", "totally different"])
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,
            silence_ms=100000,
            max_segment_ms=100000,
            local_agreement=False,
        )
        texts: list[str] = []
        for _ in range(3):
            await t.push(_pcm(100, amplitude=4000))
            texts.extend(m.text for m in _drain(t) if isinstance(m, CaptionPartial))

        assert t._agreement is None
        # Raw path emits exactly what the engine returned — including the full rewrite.
        assert texts == ["alpha", "alpha beta", "totally different"]

    asyncio.run(run())


# ----- word timestamps only where they're used (XERK-115) -------------------


class WantWordsEngine:
    """Records the want_words hint of every decode."""

    def __init__(self) -> None:
        self.wants: list[bool] = []

    def transcribe(
        self, samples: np.ndarray, *, language: str | None, want_words: bool = True
    ) -> EngineResult:
        self.wants.append(want_words)
        return EngineResult(text="hello", words=[EngineWord("hello", 0.0, 0.5)], language="en")


def test_partials_skip_word_timestamps_finals_ask_for_them() -> None:
    """Partials are pure text, so they tell the engine not to spend decode time on
    word timing; only the final — which carries word timing into the transcript —
    asks for it."""

    async def run() -> None:
        eng = WantWordsEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,
            silence_ms=300,
            min_segment_ms=100,
            max_segment_ms=5000,
        )
        for _ in range(3):  # speech -> partials
            await t.push(_pcm(100, amplitude=4000))
        assert eng.wants and all(w is False for w in eng.wants)

        for _ in range(3):  # trailing silence -> one final
            await t.push(_pcm(100, amplitude=0))
        assert eng.wants[-1] is True

    asyncio.run(run())


def test_legacy_partials_also_skip_word_timestamps() -> None:
    """Same on the LocalAgreement-off path — it emits raw text too."""

    async def run() -> None:
        eng = WantWordsEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=100,
            silence_ms=100000,
            max_segment_ms=100000,
            local_agreement=False,
        )
        await t.push(_pcm(200, amplitude=4000))
        assert eng.wants == [False]

    asyncio.run(run())


# ----- adaptive VAD (XERK-115) ----------------------------------------------


def _noisy_room(adaptive: bool) -> StreamingTranscriber:
    """One turn in a room whose background sits well above the absolute silence_rms:
    speech at rms ~0.244, then a pause at rms ~0.037 — quiet relative to the speaker,
    but 7x the 0.005 fixed gate. Only a detected pause can close this turn."""

    async def run() -> StreamingTranscriber:
        t = StreamingTranscriber(
            FakeEngine(),
            language="en",
            partial_interval_ms=100000,  # keep partials out of it
            silence_ms=300,
            min_segment_ms=100,
            max_segment_ms=100000,  # no max-segment escape hatch
            silence_rms=0.005,
            vad_adaptive=adaptive,
        )
        for _ in range(10):
            await t.push(_pcm(100, amplitude=8000))  # speech over the room
        for _ in range(10):
            await t.push(_pcm(100, amplitude=1200))  # the room alone — a real pause
        return t

    return asyncio.run(run())


def test_fixed_vad_never_closes_a_turn_in_a_noisy_room() -> None:
    """The regression this fixes: 0.037 >= 0.005, so every frame of the pause reads as
    speech and the turn can only ever end on max_segment_ms."""
    t = _noisy_room(adaptive=False)
    assert not [m for m in _drain(t) if isinstance(m, CaptionFinal)]
    assert t._trailing_silence == 0  # not one frame counted as silence


def test_adaptive_vad_closes_the_turn_in_the_same_noisy_room() -> None:
    """Same audio, adaptive threshold: it rides above the measured background, so the
    pause is found and the turn finalizes on it — on the FIRST turn, without waiting
    for a max-segment close to teach it the room."""
    t = _noisy_room(adaptive=True)
    assert len([m for m in _drain(t) if isinstance(m, CaptionFinal)]) == 1


def test_adaptive_vad_is_a_no_op_in_a_quiet_room() -> None:
    """With a genuinely silent background the measured floor is 0, so the threshold is
    exactly the old fixed silence_rms — existing behaviour is unchanged."""

    async def run() -> None:
        t = StreamingTranscriber(FakeEngine(), silence_rms=0.005)
        assert t._speech_threshold() == pytest.approx(0.005)  # no audio seen yet
        for _ in range(5):
            await t.push(_pcm(100, amplitude=0))
        assert t._speech_threshold() == pytest.approx(0.005)
        # And speech over silence still reads as speech.
        await t.push(_pcm(100, amplitude=4000))
        assert t._has_speech is True

    asyncio.run(run())


def test_threshold_cannot_climb_over_the_speaker() -> None:
    """Uniformly loud speech with no gaps gives min == max, so the raw floor*ratio
    threshold would land above the speaker's own level and read them as silent. The
    peak-fraction ceiling keeps them detected."""

    async def run() -> None:
        t = StreamingTranscriber(
            FakeEngine(),
            partial_interval_ms=100000,
            silence_ms=100000,
            max_segment_ms=100000,
            silence_rms=0.005,
        )
        level = rms(pcm16_to_float32(_pcm(100, amplitude=8000)))
        for _ in range(50):  # 5s of flat, continuous speech — no pause anywhere
            await t.push(_pcm(100, amplitude=8000))
        # floor*ratio would be 3x the speaker; the ceiling pins it to half of them.
        assert t._speech_threshold() == pytest.approx(level * 0.5, rel=1e-3)
        assert t._has_speech is True
        assert t._trailing_silence == 0  # never mistook the speaker for silence

    asyncio.run(run())


def test_vad_level_window_is_bounded() -> None:
    """The level history is a trailing window, so it can't grow with session length."""

    async def run() -> None:
        t = StreamingTranscriber(FakeEngine(), vad_window_ms=500)
        for _ in range(50):  # 5s of audio through a 500ms window
            await t.push(_pcm(100, amplitude=4000))
        assert t._levels_bytes <= _ms_to_bytes(500) + _ms_to_bytes(100)
        assert len(t._levels) <= 6

    asyncio.run(run())


def test_vad_window_keeps_one_entry_for_an_oversized_chunk() -> None:
    """A chunk longer than the whole window still has to describe the current level."""

    async def run() -> None:
        t = StreamingTranscriber(FakeEngine(), vad_window_ms=100)
        await t.push(_pcm(500, amplitude=4000))
        await t.push(_pcm(500, amplitude=4000))
        assert len(t._levels) == 1

    asyncio.run(run())


def test_vad_level_window_survives_a_turn_boundary() -> None:
    """The window describes the room, not the turn — dropping it at each finalize
    would put the first pause of every turn back on the fixed threshold."""

    async def run() -> None:
        t = StreamingTranscriber(
            FakeEngine(),
            partial_interval_ms=100000,
            silence_ms=100000,
            max_segment_ms=300,  # force a finalize
            silence_rms=0.005,
        )
        for _ in range(3):
            await t.push(_pcm(100, amplitude=8000))
        assert not t._buf  # the turn closed
        assert t._levels  # ...but the room estimate carried over
        assert t._speech_threshold() > 0.005

    asyncio.run(run())


def test_adaptive_vad_can_be_turned_off() -> None:
    async def run() -> None:
        t = StreamingTranscriber(FakeEngine(), silence_rms=0.005, vad_adaptive=False)
        await t.push(_pcm(100, amplitude=8000))  # would raise an adaptive threshold
        assert t._speech_threshold() == pytest.approx(0.005)

    asyncio.run(run())


# ----- factory --------------------------------------------------------------


def test_factory_defaults_to_stub() -> None:
    from api.stt.stub import StubTranscriber

    assert isinstance(make_transcriber(), StubTranscriber)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "bogus")
    with pytest.raises(ValueError, match="unknown STT backend"):
        make_transcriber()


# ----- hybrid streaming partials (XERK-115) ---------------------------------


class FakeStreamSession:
    """Records how it's driven; grows a transcript token per non-silent feed, like a
    cache-aware stream would (silence yields no update)."""

    def __init__(self, *, language: str | None) -> None:
        self.language = language
        self.feeds = 0
        self.resets = 0
        self.closed = False
        self.fail_feed = False
        self._tokens: list[str] = []

    async def feed(self, pcm: bytes) -> str | None:
        self.feeds += 1
        if self.fail_feed:
            raise RuntimeError("stream feed exploded")
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0 or int(np.abs(samples).max()) == 0:
            return None  # silence -> no partial update
        self._tokens.append(f"p{len(self._tokens)}")
        return " ".join(self._tokens)

    async def reset(self) -> None:
        self.resets += 1
        self._tokens = []

    async def aclose(self) -> None:
        self.closed = True


class FakeStreamEngine:
    def __init__(self, *, fail_open: bool = False) -> None:
        self.sessions: list[FakeStreamSession] = []
        self.fail_open = fail_open

    async def open(self, *, language: str | None) -> FakeStreamSession:
        if self.fail_open:
            raise RuntimeError("no route to stream server")
        s = FakeStreamSession(language=language)
        self.sessions.append(s)
        return s


def test_hybrid_partials_from_stream_finals_from_offline() -> None:
    async def run() -> None:
        eng = FakeEngine()
        se = FakeStreamEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=200,
            silence_ms=300,
            min_segment_ms=100,
            stream_engine=se,
        )
        for _ in range(3):  # speech: three chunks -> three growing stream partials
            await t.push(_pcm(100, amplitude=8000))
        for _ in range(3):  # trailing silence closes the turn
            await t.push(_pcm(100, amplitude=0))
        msgs = _drain(t)

        partials = [m for m in msgs if isinstance(m, CaptionPartial)]
        finals = [m for m in msgs if isinstance(m, CaptionFinal)]
        assert [p.text for p in partials] == ["p0", "p0 p1", "p0 p1 p2"]
        assert len(finals) == 1
        assert finals[0].text == "hello world"  # final came from the offline engine
        # The offline engine decoded ONLY the final — partials never touched it.
        assert eng.calls == 1
        assert len(se.sessions) == 1
        assert se.sessions[0].feeds == 6  # every chunk, speech and silence, was fed
        assert se.sessions[0].resets == 1  # cache cleared at the turn boundary

    asyncio.run(run())


def test_hybrid_suppresses_unchanged_and_empty_partials() -> None:
    class StickySession(FakeStreamSession):
        async def feed(self, pcm: bytes) -> str | None:
            self.feeds += 1
            return "same text"  # never advances

    class StickyEngine(FakeStreamEngine):
        async def open(self, *, language: str | None) -> FakeStreamSession:
            s = StickySession(language=language)
            self.sessions.append(s)
            return s

    async def run() -> None:
        t = StreamingTranscriber(
            FakeEngine(), language="en", stream_engine=StickyEngine(), silence_ms=100000
        )
        await t.push(_pcm(100, amplitude=8000))
        await t.push(_pcm(100, amplitude=8000))
        partials = [m for m in _drain(t) if isinstance(m, CaptionPartial)]
        assert [p.text for p in partials] == ["same text"]  # emitted once, then deduped

    asyncio.run(run())


def test_hybrid_falls_back_to_redecode_when_stream_open_fails() -> None:
    async def run() -> None:
        eng = FakeEngine()
        se = FakeStreamEngine(fail_open=True)
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=200,
            silence_ms=100000,
            local_agreement=False,
            stream_engine=se,
        )
        await t.push(_pcm(100, amplitude=8000))
        await t.push(_pcm(100, amplitude=8000))  # 200ms cadence -> re-decode partial
        partials = [m for m in _drain(t) if isinstance(m, CaptionPartial)]
        assert t._stream_failed is True
        assert partials and partials[0].text == "hello world"  # offline fallback

    asyncio.run(run())


def test_hybrid_falls_back_when_stream_feed_fails_midsession() -> None:
    async def run() -> None:
        eng = FakeEngine()
        se = FakeStreamEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=200,
            silence_ms=100000,
            local_agreement=False,
            stream_engine=se,
        )
        await t.push(_pcm(100, amplitude=8000))  # opens + one good stream partial
        se.sessions[0].fail_feed = True
        await t.push(_pcm(100, amplitude=8000))  # feed raises -> drop + fall back
        msgs = [m for m in _drain(t) if isinstance(m, CaptionPartial)]
        assert t._stream_failed is True
        assert se.sessions[0].closed is True  # broken session was closed
        assert any(p.text == "hello world" for p in msgs)  # re-decode fallback kicked in

    asyncio.run(run())


def test_hybrid_reset_failure_drops_to_redecode() -> None:
    class BadResetSession(FakeStreamSession):
        async def reset(self) -> None:
            raise RuntimeError("reset failed")

    class BadResetEngine(FakeStreamEngine):
        async def open(self, *, language: str | None) -> FakeStreamSession:
            s = BadResetSession(language=language)
            self.sessions.append(s)
            return s

    async def run() -> None:
        se = BadResetEngine()
        t = StreamingTranscriber(
            FakeEngine(),
            language="en",
            silence_ms=300,
            min_segment_ms=100,
            stream_engine=se,
        )
        for _ in range(3):
            await t.push(_pcm(100, amplitude=8000))
        for _ in range(3):  # close the turn -> finalize -> reset() raises
            await t.push(_pcm(100, amplitude=0))
        assert t._stream_failed is True
        assert se.sessions[0].closed is True

    asyncio.run(run())


def test_hybrid_close_closes_stream_session() -> None:
    async def run() -> None:
        se = FakeStreamEngine()
        t = StreamingTranscriber(FakeEngine(), language="en", stream_engine=se)
        await t.push(_pcm(100, amplitude=8000))
        await t.close()
        assert se.sessions[0].closed is True

    asyncio.run(run())


# ----- warmup: pay the stream's per-session startup cost early (XERK-128) ----


def test_warmup_opens_stream_ahead_of_audio() -> None:
    async def run() -> None:
        se = FakeStreamEngine()
        t = StreamingTranscriber(FakeEngine(), language="en", stream_engine=se)

        await t.warmup()
        # The socket is open and primed before a single audio frame arrived: one
        # silent feed paid the cold-decode cost, one reset cleared it.
        assert len(se.sessions) == 1
        assert se.sessions[0].language == "en"
        assert se.sessions[0].feeds == 1
        assert se.sessions[0].resets == 1
        # Priming emitted no caption — silence yields no partial, and the reset wipes it.
        assert _drain(t) == []

        # First real speech reuses the warmed session instead of opening a second one.
        await t.push(_pcm(100, amplitude=8000))
        assert len(se.sessions) == 1
        assert se.sessions[0].feeds == 2
        partials = [m for m in _drain(t) if isinstance(m, CaptionPartial)]
        assert [p.text for p in partials] == ["p0"]  # numbering restarted after reset

    asyncio.run(run())


def test_warmup_is_noop_without_stream_engine() -> None:
    async def run() -> None:
        t = StreamingTranscriber(FakeEngine(), language="en")  # offline-only, no stream
        await t.warmup()
        assert t._stream_session is None
        assert _drain(t) == []

    asyncio.run(run())


def test_warmup_is_idempotent() -> None:
    async def run() -> None:
        se = FakeStreamEngine()
        t = StreamingTranscriber(FakeEngine(), language="en", stream_engine=se)
        await t.warmup()
        await t.warmup()  # already open -> does nothing more
        assert len(se.sessions) == 1
        assert se.sessions[0].feeds == 1

    asyncio.run(run())


def test_warmup_open_failure_falls_back_to_lazy() -> None:
    async def run() -> None:
        se = FakeStreamEngine(fail_open=True)
        t = StreamingTranscriber(
            FakeEngine(),
            language="en",
            partial_interval_ms=200,
            silence_ms=100000,
            local_agreement=False,
            stream_engine=se,
        )
        await t.warmup()
        assert t._stream_failed is True  # couldn't open -> marked failed
        # Captions still flow via the offline re-decode fallback.
        await t.push(_pcm(100, amplitude=8000))
        await t.push(_pcm(100, amplitude=8000))
        partials = [m for m in _drain(t) if isinstance(m, CaptionPartial)]
        assert partials and partials[0].text == "hello world"

    asyncio.run(run())


def test_warmup_feed_failure_falls_back_to_lazy() -> None:
    class BadFeedSession(FakeStreamSession):
        async def feed(self, pcm: bytes) -> str | None:
            raise RuntimeError("warm feed exploded")

    class BadFeedEngine(FakeStreamEngine):
        async def open(self, *, language: str | None) -> FakeStreamSession:
            s = BadFeedSession(language=language)
            self.sessions.append(s)
            return s

    async def run() -> None:
        se = BadFeedEngine()
        t = StreamingTranscriber(
            FakeEngine(),
            language="en",
            partial_interval_ms=200,
            silence_ms=100000,
            local_agreement=False,
            stream_engine=se,
        )
        await t.warmup()
        assert t._stream_failed is True
        assert se.sessions[0].closed is True  # the broken session was dropped
        await t.push(_pcm(100, amplitude=8000))
        await t.push(_pcm(100, amplitude=8000))
        partials = [m for m in _drain(t) if isinstance(m, CaptionPartial)]
        assert partials and partials[0].text == "hello world"

    asyncio.run(run())


def test_stub_warmup_is_noop() -> None:
    from api.stt.stub import StubTranscriber

    async def run() -> None:
        t = StubTranscriber()
        await t.warmup()  # nothing to warm, must not raise or emit
        assert _drain_stub(t) == []

    asyncio.run(run())


def _drain_stub(t: object) -> list[object]:
    out: list[object] = []
    while not t._queue.empty():  # type: ignore[attr-defined]
        out.append(t._queue.get_nowait())  # type: ignore[attr-defined]
    return out


def test_stream_locale_mapping() -> None:
    from api.stt.streaming_engine import to_locale

    assert to_locale("en") == "en-US"
    assert to_locale("es") == "es-ES"
    assert to_locale("EN") == "en-US"  # case-insensitive
    assert to_locale("fr") == "auto"  # unmapped -> auto-detect
    assert to_locale(None) == "auto"


def test_factory_hybrid_builds_stream_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "hybrid")
    monkeypatch.setattr(settings, "stt_stream_endpoint", "ws://nemotron:8000")
    t = make_transcriber()
    assert isinstance(t, StreamingTranscriber)
    assert t._stream_engine is not None  # wired, but opens no socket until first audio


def test_factory_hybrid_requires_stream_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "hybrid")
    monkeypatch.setattr(settings, "stt_stream_endpoint", "")
    with pytest.raises(ValueError, match="hybrid"):
        make_transcriber()
