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

An optional `stream_engine` (XERK-115) turns this into the *hybrid* path: live
partials come from a persistent cache-aware stream (Nemotron, low latency) while
finals still decode the whole turn on the offline `WhisperEngine` (Parakeet, higher
accuracy) — the split the GPU benchmark recommends
(`docs/stt-model-gpu-benchmark.md`). All the windowing/VAD/finalize logic is shared;
only the partial *source* differs, and it degrades to the re-decode path if the
stream is unavailable.
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
from api.stt.streaming_engine import StreamingSttEngine

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
        stream_engine: StreamingSttEngine | None = None,
        start_offset_ms: int = 0,
        final_words: bool = True,
    ) -> None:
        self._engine = engine
        self._language = language
        # Whether final decodes ask the engine for per-word timestamps. Computing
        # them dominates final-decode latency on the deployed server (~5.5x, see
        # Settings.stt_final_word_timestamps), so production runs with this off and
        # CaptionFinal.words stays None.
        self._final_words = final_words
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

        # Hybrid partials (XERK-115). When a streaming engine is given, live partials
        # come from a persistent cache-aware stream (Nemotron) instead of re-decoding
        # the segment each cadence: one turn = one session, fed every chunk, emitting
        # a transcript that only grows. Finals still decode the whole turn on the
        # offline `engine` (Parakeet) for the accurate stored transcript — the offline
        # model is more accurate, the stream is lower-latency (see
        # docs/stt-model-gpu-benchmark.md). If the stream can't be reached or errors
        # mid-session, `_stream_failed` flips and partials fall back to the re-decode
        # cadence path below, so captions degrade to Parakeet-only rather than stop.
        self._stream_engine = stream_engine
        self._stream_session = None
        self._stream_failed = False
        self._last_partial = ""
        # The stream is opened OFF the caption path (a background task, kicked off by
        # warmup() and/or the first push), never inline — connecting it, and the
        # server building this connection's decoder, can take a moment or hang on an
        # unhealthy/misconfigured stream server, and the api reads audio frames
        # serially, so an inline open stalls every caption behind it (XERK-128). Until
        # the open completes, partials come from the fast re-decode path; captions
        # never wait on the stream.
        self._open_task: asyncio.Task[bool] | None = None
        # Latched at each turn boundary: does THIS turn's partials come from the stream?
        # Fixing the source for a whole turn avoids a visible rewrite when the stream
        # finishes opening mid-turn (its transcript starts empty and would briefly be
        # shorter than the re-decode caption already on screen).
        self._stream_turn = False

        self._buf = bytearray()
        self._since_partial = 0
        self._trailing_silence = 0
        self._has_speech = False
        # Segment times count audio bytes from here on. A resumed conversation
        # (a new Session on an existing conversation id) seeds this with the
        # duration already retained, so its segments continue the conversation's
        # timeline instead of restarting at 0 — restarting made the merged
        # transcript interleave sittings and desynced it from the stored audio,
        # which appends across sittings (see Session._persist).
        self._segment_start_ms = start_offset_ms

        self._queue: asyncio.Queue[CaptionPartial | CaptionFinal] = asyncio.Queue()
        self._closed = False

    async def warmup(self) -> None:
        """Start opening the live stream ahead of the first audio, off the caption
        path (XERK-128).

        On the hybrid path the stream's per-session setup — connecting the socket and
        the server building this connection's decoder — used to be paid inline on the
        first spoken chunk. Because the api reads audio frames serially, that stall
        held up every frame behind it, so captions appeared late and then in a burst
        carrying the whole backlog. Worse, while the stream was still "coming up" the
        re-decode fallback was suppressed, so an unhealthy or misconfigured stream
        server stalled captions for its whole connect timeout.

        Opening from here (in the background) means the first spoken words are captioned
        from the fast re-decode path immediately and only switch to the stream once it
        is confirmed up — so a slow or dead stream never delays the first caption. This
        just kicks the open off and waits for it; a no-op without a streaming engine."""
        self._ensure_open_started()
        if self._open_task is not None:
            await self._open_task

    async def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        # Latch the partial source for this turn at its first chunk (buffer empty),
        # so a stream that finishes opening mid-turn doesn't rewrite the caption.
        if not self._buf:
            self._stream_turn = self._stream_ready()
        self._buf.extend(pcm)
        self._since_partial += len(pcm)
        self._update_vad(pcm)

        # Make sure the stream is opening (off the caption path); harmless once open or
        # once it has failed. Never awaited here — captions must not wait on it.
        self._ensure_open_started()

        # Live partials from the cache-aware stream are driven by the audio itself
        # (fed every chunk), not by the re-decode cadence timer below — but only for a
        # turn that started with the stream ready.
        if self._stream_turn and self._stream_ready():
            await self._feed_stream(pcm)

        # Re-checked AFTER the feed: a feed that failed mid-turn drops the stream, and
        # the rest of this turn must fall straight back to re-decode partials rather
        # than go silent until the turn ends.
        stream_driving = self._stream_turn and self._stream_ready()

        if len(self._buf) >= self._max_segment_bytes:
            await self._finalize()
        elif (
            self._has_speech
            and self._trailing_silence >= self._silence_bytes
            and len(self._buf) >= self._min_segment_bytes
        ):
            await self._finalize()
        elif self._has_speech and self._since_partial >= self._partial_bytes and not stream_driving:
            # Re-decode cadence partial: whenever the stream isn't driving — no stream
            # configured, it hasn't finished opening, or it failed (before this turn or
            # part-way through it) and we fell back.
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

    def _stream_ready(self) -> bool:
        """Whether the stream is open and healthy, i.e. it can drive partials right
        now. False keeps captions on the re-decode path — before the stream has
        finished opening as well as after it has failed."""
        return (
            self._stream_engine is not None
            and not self._stream_failed
            and self._stream_session is not None
        )

    def _ensure_open_started(self) -> None:
        """Kick off opening the stream in the background, at most once at a time.

        Never blocks the caller: the caption path keeps serving re-decode partials
        until the open completes (see `push`). Called from both `warmup` and every
        `push`, so the stream still opens even if `warmup` was never called."""
        if self._stream_engine is None or self._stream_failed:
            return
        if self._stream_session is not None:
            return
        if self._open_task is not None and not self._open_task.done():
            return
        self._open_task = asyncio.create_task(self._open_and_prime())

    async def _open_and_prime(self) -> bool:
        """Open the streaming session and prime it, off the caption path. False =
        unusable (the caption path then just stays on re-decode partials)."""
        try:
            self._stream_session = await self._stream_engine.open(language=self._language)
        except Exception:  # noqa: BLE001 — a dead stream must never stop captions
            log.warning("stream open failed; using re-decode partials", exc_info=True)
            self._stream_failed = True
            return False
        try:
            # A scrap of silence pays the stream's one-time cold-decode cost now, off
            # the caption path, so the first spoken chunk gets a fast partial. No reset
            # afterward: leading silence is benign to a cache-aware model (a real turn
            # opens with some too) and the turn-boundary reset clears it regardless.
            await self._stream_session.feed(bytes(_ms_to_bytes(100)))
        except Exception:  # noqa: BLE001 — a failed prime drops us to re-decode partials
            log.warning("stream prime failed; using re-decode partials", exc_info=True)
            await self._drop_stream()
            self._stream_failed = True
            return False
        return True

    async def _drop_stream(self) -> None:
        session, self._stream_session = self._stream_session, None
        if session is not None:
            try:
                await session.aclose()
            except Exception:  # noqa: BLE001 — closing a broken socket is best-effort
                pass

    async def _feed_stream(self, pcm: bytes) -> None:
        """Feed one chunk to the already-open live stream and emit a partial if the
        turn grew. The caller only invokes this once the stream is confirmed open
        (`_stream_ready`); opening happens off the caption path in `_open_and_prime`."""
        try:
            text = await self._stream_session.feed(pcm)
        except Exception:  # noqa: BLE001 — fall back to re-decode partials on any error
            log.warning("stream feed failed; using re-decode partials", exc_info=True)
            await self._drop_stream()
            self._stream_failed = True
            return
        text = (text or "").strip()
        if not text or text == self._last_partial:
            return
        self._last_partial = text
        await self._queue.put(
            CaptionPartial(type="caption.partial", text=text, lang=_lang(self._language))
        )

    async def _reset_stream(self) -> None:
        """Clear the live stream's per-turn cache at a segment boundary."""
        self._last_partial = ""
        if self._stream_session is None:
            return
        try:
            await self._stream_session.reset()
        except Exception:  # noqa: BLE001 — a failed reset drops us to re-decode partials
            log.warning("stream reset failed; using re-decode partials", exc_info=True)
            await self._drop_stream()
            self._stream_failed = True

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
        result = await self._run_engine(stage="final", want_words=self._final_words)
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
        # The live stream is per-turn: clear its encoder cache for the next turn so
        # the next partial doesn't carry the closed turn's context.
        if self._stream_session is not None:
            await self._reset_stream()

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
        # A still-running background open would race close()/drop below (both touch the
        # stream session): cancel it and let it settle before dropping the session.
        if self._open_task is not None and not self._open_task.done():
            self._open_task.cancel()
            try:
                await self._open_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 — teardown swallows a late open error
                pass
        self._open_task = None
        await self._drop_stream()
        # Unblock a pending results() get with a skipped (empty) sentinel.
        await self._queue.put(CaptionPartial(type="caption.partial", text="", lang=None))
