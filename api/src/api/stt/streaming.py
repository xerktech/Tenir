"""Realtime streaming transcriber (master plan §5.2, Phase 1).

Turns a stream of small PCM chunks into the two caption flavours the contract
defines:

- `caption.partial` — fast, unstable hypothesis re-run on a cadence for the live
  caption band.
- `caption.final` — a stable segment with word timestamps, emitted when an
  energy-based VAD sees enough trailing silence (a turn boundary) or the segment
  hits a max length.

All model inference is delegated to a `WhisperEngine` and run off the event loop
via `asyncio.to_thread`, so a busy model applies natural per-connection
backpressure without stalling other sessions. The windowing/VAD logic here is
model-agnostic and unit-tested with a fake engine.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator

from api.contract import CaptionFinal, CaptionPartial, Lang, Word
from api.metrics import metrics
from api.stt.agreement import LocalAgreement
from api.stt.engine import BYTES_PER_SEC, WhisperEngine, pcm16_to_float32, rms

log = logging.getLogger("api.stt.streaming")


def _ms_to_bytes(ms: int) -> int:
    return ms * BYTES_PER_SEC // 1000


def _bytes_to_ms(n: int) -> int:
    return n * 1000 // BYTES_PER_SEC


def _lang(value: str | None) -> Lang | None:
    """Map an engine language string to the contract Lang enum, else None."""
    try:
        return Lang(value) if value is not None else None
    except ValueError:
        return None


class StreamingTranscriber:
    def __init__(
        self,
        engine: WhisperEngine,
        *,
        language: str | None = None,
        partial_interval_ms: int = 700,
        partial_window_ms: int = 6000,
        max_segment_ms: int = 12000,
        min_segment_ms: int = 400,
        silence_ms: int = 700,
        silence_rms: float = 0.005,
        local_agreement: bool = True,
    ) -> None:
        self._engine = engine
        self._language = language
        self._partial_bytes = _ms_to_bytes(partial_interval_ms)
        # Partials decode only this trailing window so their latency stays bounded
        # regardless of how long the in-flight turn has grown (master plan §10);
        # 0 means "decode the whole segment" (the legacy behaviour).
        self._partial_window_bytes = _ms_to_bytes(partial_window_ms) if partial_window_ms else 0
        self._max_segment_bytes = _ms_to_bytes(max_segment_ms)
        self._min_segment_bytes = _ms_to_bytes(min_segment_ms)
        self._silence_bytes = _ms_to_bytes(silence_ms)
        self._silence_rms = silence_rms

        # LocalAgreement-2 makes partials grow word by word instead of rewriting the
        # whole line each cadence (XERK-90). One buffer per in-flight segment; reset
        # at every finalize. None disables it (legacy: emit each raw window verbatim).
        self._agreement = LocalAgreement() if local_agreement else None

        self._buf = bytearray()
        self._since_partial = 0
        self._trailing_silence = 0
        self._has_speech = False
        self._segment_start_ms = 0

        self._queue: asyncio.Queue[CaptionPartial | CaptionFinal] = asyncio.Queue()
        self._closed = False

    async def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._buf.extend(pcm)
        self._since_partial += len(pcm)
        self._update_vad(pcm)

        if len(self._buf) >= self._max_segment_bytes:
            await self._finalize()
        elif (
            self._has_speech
            and self._trailing_silence >= self._silence_bytes
            and len(self._buf) >= self._min_segment_bytes
        ):
            await self._finalize()
        elif self._has_speech and self._since_partial >= self._partial_bytes:
            await self._emit_partial()

    def _update_vad(self, pcm: bytes) -> None:
        if rms(pcm16_to_float32(pcm)) >= self._silence_rms:
            self._has_speech = True
            self._trailing_silence = 0
        else:
            self._trailing_silence += len(pcm)

    async def _run_engine(self, *, window_bytes: int = 0, stage: str = "final"):
        # A partial may decode only the trailing window_bytes of the segment so its
        # cost doesn't grow with turn length; a final (window_bytes=0) decodes the
        # whole segment for a stable transcript. The inference time is recorded so the
        # caption-path latency budget (master plan §6) can actually be measured/tuned.
        buf = self._buf
        if window_bytes and len(buf) > window_bytes:
            buf = buf[-window_bytes:]
        samples = pcm16_to_float32(bytes(buf))
        t0 = time.perf_counter()
        result = await asyncio.to_thread(self._engine.transcribe, samples, language=self._language)
        metrics.observe(f"stage.stt.{stage}_latency_ms", (time.perf_counter() - t0) * 1000)
        return result

    async def _emit_partial(self) -> None:
        self._since_partial = 0
        result = await self._run_engine(window_bytes=self._partial_window_bytes, stage="partial")
        text = result.text.strip()
        lang = _lang(result.language or self._language)

        if self._agreement is None:
            # Legacy path: emit the raw window hypothesis, which rewrites the whole
            # caption line each cadence.
            if not text:
                return
            await self._queue.put(CaptionPartial(type="caption.partial", text=text, lang=lang))
            return

        # LocalAgreement-2: fold this window's hypothesis into the running commit so
        # already-shown words stay put and only the trailing word or two can still
        # change. `caption_text` is the stable prefix plus that tentative tail.
        self._agreement.commit(text.split())
        caption = self._agreement.caption_text()
        if not caption:
            return
        await self._queue.put(CaptionPartial(type="caption.partial", text=caption, lang=lang))

    async def _finalize(self) -> None:
        result = await self._run_engine(stage="final")
        start = self._segment_start_ms
        end = start + _bytes_to_ms(len(self._buf))

        # Reset for the next segment before emitting so timing stays monotonic.
        self._segment_start_ms = end
        self._buf.clear()
        self._since_partial = 0
        self._trailing_silence = 0
        self._has_speech = False
        # The committed prefix belongs to the turn just closed; start the next turn's
        # word-by-word commit from scratch.
        if self._agreement is not None:
            self._agreement = LocalAgreement()

        text = result.text.strip()
        if not text:
            return  # silence / no speech in this window — nothing to surface

        words = [
            Word(
                text=w.text,
                startMs=max(0, start + int(w.start * 1000)),
                endMs=max(0, start + int(w.end * 1000)),
                confidence=w.probability,
            )
            for w in result.words
        ] or None
        await self._queue.put(
            CaptionFinal(
                type="caption.final",
                segmentId=str(uuid.uuid4()),
                text=text,
                lang=_lang(result.language or self._language),
                startMs=start,
                endMs=end,
                words=words,
            )
        )

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        while not self._closed:
            yield await self._queue.get()

    async def flush(self) -> None:
        if self._buf and self._has_speech:
            await self._finalize()

    async def close(self) -> None:
        self._closed = True
        # Unblock a pending results() get with a skipped (empty) sentinel.
        await self._queue.put(CaptionPartial(type="caption.partial", text="", lang=None))
