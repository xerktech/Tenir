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
from api.stt.streaming import StreamingTranscriber, _lang


class FakeEngine:
    """Returns fixed text for audio with energy, empty for silence."""

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
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
        def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
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
        def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
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

    def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
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

    def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
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


# ----- factory --------------------------------------------------------------


def test_factory_defaults_to_stub() -> None:
    from api.stt.stub import StubTranscriber

    assert isinstance(make_transcriber(), StubTranscriber)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "bogus")
    with pytest.raises(ValueError, match="unknown STT backend"):
        make_transcriber()
