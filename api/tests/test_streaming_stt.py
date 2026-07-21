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


def test_partial_decodes_only_trailing_window() -> None:
    """A partial re-decodes at most partial_window_ms; a final decodes the whole
    segment — so partial latency stays bounded as a turn grows (master plan §10)."""

    class RecordingEngine:
        def __init__(self) -> None:
            self.sizes: list[int] = []

        def transcribe(self, samples: np.ndarray, *, language: str | None) -> EngineResult:
            self.sizes.append(int(samples.size))
            return EngineResult(text="hello", words=[], language="en")

    async def run() -> None:
        eng = RecordingEngine()
        t = StreamingTranscriber(
            eng,
            language="en",
            partial_interval_ms=500,
            partial_window_ms=1000,  # 16000 samples
            silence_ms=100000,
            max_segment_ms=100000,
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


# ----- factory --------------------------------------------------------------


def test_factory_defaults_to_stub() -> None:
    from api.stt.stub import StubTranscriber

    assert isinstance(make_transcriber(), StubTranscriber)


def test_factory_rejects_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.config import settings

    monkeypatch.setattr(settings, "stt_backend", "bogus")
    with pytest.raises(ValueError, match="unknown STT backend"):
        make_transcriber()
