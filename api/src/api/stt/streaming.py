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
from collections import deque
from collections.abc import AsyncIterator

from api.contract import CaptionFinal, CaptionPartial, Lang, Word
from api.metrics import metrics
from api.stt.agreement import LocalAgreement
from api.stt.engine import BYTES_PER_SEC, WhisperEngine, pcm16_to_float32, rms

log = logging.getLogger("api.stt.streaming")

# Ceiling on the adaptive speech threshold, as a fraction of the loudest frame in the
# VAD window. Without it, a stretch of uniformly loud speech (no gaps to pull the
# measured floor down) would push the threshold above the speaker's own level and
# read them as silence. Half the recent peak keeps a talker at that peak always
# detected, whatever the floor estimate says.
_VAD_PEAK_FRACTION = 0.5


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
        vad_adaptive: bool = True,
        vad_noise_ratio: float = 3.0,
        vad_window_ms: int = 3000,
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

        # Adaptive VAD (XERK-115). `silence_rms` on its own is an absolute gate, so a
        # room noisier than it reads as wall-to-wall speech: nothing ever closes a
        # turn and every caption waits out max_segment_ms. Tracking the background
        # level turns that into a *floor* under a threshold that follows the room.
        #
        # The estimate is min/max over a trailing window of frame energies (classic
        # minimum-statistics noise tracking) rather than something updated only on
        # frames already judged non-speech: in the very room this is meant to fix,
        # NO frame is judged non-speech, so such an estimator could never start.
        self._vad_adaptive = vad_adaptive
        self._vad_noise_ratio = vad_noise_ratio
        self._vad_window_bytes = _ms_to_bytes(vad_window_ms)
        # (chunk length, chunk RMS) for the trailing window, oldest first.
        self._levels: deque[tuple[int, float]] = deque()
        self._levels_bytes = 0

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

    def _speech_threshold(self) -> float:
        """The RMS a frame must reach to count as speech.

        `silence_rms` is the absolute floor. With adaptive VAD on, the live threshold
        rides `vad_noise_ratio` above the quietest frame in the trailing window — so a
        noisy room raises the bar instead of drowning the gate — but never past
        `_VAD_PEAK_FRACTION` of the loudest frame in it, so it can't climb over the
        speaker. In a quiet room the measured floor is ~0 and this is exactly the old
        fixed threshold.
        """
        if not self._vad_adaptive or not self._levels:
            return self._silence_rms
        levels = [lvl for _, lvl in self._levels]
        adaptive = min(levels) * self._vad_noise_ratio
        adaptive = min(adaptive, max(levels) * _VAD_PEAK_FRACTION)
        return max(self._silence_rms, adaptive)

    def _track_level(self, pcm: bytes) -> None:
        """Add this chunk's energy to the trailing VAD window, dropping what fell out."""
        self._levels.append((len(pcm), rms(pcm16_to_float32(pcm))))
        self._levels_bytes += len(pcm)
        # Keep at least one entry: a chunk longer than the whole window still has to
        # describe the current level.
        while len(self._levels) > 1 and self._levels_bytes - self._levels[0][0] >= (
            self._vad_window_bytes
        ):
            self._levels_bytes -= self._levels.popleft()[0]

    def _update_vad(self, pcm: bytes) -> None:
        self._track_level(pcm)
        if self._levels[-1][1] >= self._speech_threshold():
            self._has_speech = True
            self._trailing_silence = 0
        else:
            self._trailing_silence += len(pcm)

    async def _run_engine(
        self, *, window_bytes: int = 0, stage: str = "final", want_words: bool = True
    ):
        # A partial may decode only the trailing window_bytes of the segment so its
        # cost doesn't grow with turn length; a final (window_bytes=0) decodes the
        # whole segment for a stable transcript. The inference time is recorded so the
        # caption-path latency budget (master plan §6) can actually be measured/tuned.
        buf = self._buf
        if window_bytes and len(buf) > window_bytes:
            buf = buf[-window_bytes:]
        samples = pcm16_to_float32(bytes(buf))
        t0 = time.perf_counter()
        result = await asyncio.to_thread(
            self._engine.transcribe, samples, language=self._language, want_words=want_words
        )
        metrics.observe(f"stage.stt.{stage}_latency_ms", (time.perf_counter() - t0) * 1000)
        return result

    async def _emit_partial(self) -> None:
        self._since_partial = 0

        if self._agreement is None:
            # Legacy path: decode the trailing window and emit it verbatim, which
            # rewrites the whole caption line each cadence.
            result = await self._run_engine(
                window_bytes=self._partial_window_bytes, stage="partial", want_words=False
            )
            text = result.text.strip()
            if not text:
                return
            lang = _lang(result.language or self._language)
            await self._queue.put(CaptionPartial(type="caption.partial", text=text, lang=lang))
            return

        # LocalAgreement-2 needs every hypothesis anchored at the same audio start so
        # successive decodes share a stable prefix — a sliding trailing window never
        # lines up and nothing commits. So partials decode the whole in-flight segment
        # here (still bounded by max_segment_ms and, in practice, short because a pause
        # finalizes the turn). The running commit then keeps already-shown words fixed
        # and only the trailing word or two can still change.
        result = await self._run_engine(window_bytes=0, stage="partial", want_words=False)
        lang = _lang(result.language or self._language)
        self._agreement.commit(result.text.split())
        caption = self._agreement.caption_text()
        if not caption:
            return
        await self._queue.put(CaptionPartial(type="caption.partial", text=caption, lang=lang))

    async def _finalize(self) -> None:
        result = await self._run_engine(stage="final")
        start = self._segment_start_ms
        end = start + _bytes_to_ms(len(self._buf))

        # Reset for the next segment before emitting so timing stays monotonic. The VAD
        # level window deliberately survives: it describes the *room*, which doesn't
        # change at a turn boundary, and re-learning it every turn would put the first
        # pause of each one back on the fixed threshold.
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
