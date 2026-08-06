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
model-agnostic and unit-tested with a fake engine. Partials re-decode a trailing
window (or the whole in-flight segment, for LocalAgreement) of the offline engine
on a cadence; finals decode the whole turn on the same engine (Parakeet in
production) for the accurate stored transcript.
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
from api.stt.langid import detect_lang

log = logging.getLogger("api.stt.streaming")

# Ceiling on the adaptive speech threshold, as a fraction of the loudest frame in the
# VAD window. Without it, a stretch of uniformly loud speech (no gaps to pull the
# measured floor down) would push the threshold above the speaker's own level and
# read them as silence. Half the recent peak keeps a talker at that peak always
# detected, whatever the floor estimate says.
_VAD_PEAK_FRACTION = 0.5

# Minimum words a partial must carry before an empty whole-turn decode may surface
# it as the final (the XERK-174 recovery). A recovered final is a *partial*
# hypothesis the offline decode rejected; on non-speech audio (music, room noise)
# those partials are overwhelmingly one-word hallucinated filler, and surfacing
# every one of them buries the transcript in junk turns that then feed the cue
# context and the translation queue (XERK-182). Calibrated by replaying retained
# session audio through the full pipeline against the production Parakeet server:
# hallucinated recoveries were 1-2 words in ~90% of cases (19/24 one-worders in
# the worst session), while every substantive recovery observed — real speech the
# offline decode blanked, the class XERK-174 exists to protect — was 3 words or
# longer. Below the gate the turn stays dropped, exactly as before XERK-174.
_RECOVERY_MIN_WORDS = 3


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
        max_segment_ms: int = 8000,
        min_segment_ms: int = 400,
        silence_ms: int = 500,
        silence_rms: float = 0.005,
        local_agreement: bool = True,
        vad_adaptive: bool = True,
        vad_noise_ratio: float = 3.0,
        vad_window_ms: int = 3000,
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

        # Adaptive VAD. `silence_rms` on its own is an absolute gate, so a room
        # noisier than it reads as wall-to-wall speech: nothing ever closes a turn
        # and every caption waits out max_segment_ms. Tracking the background level
        # turns that into a *floor* under a threshold that follows the room.
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
        # The most recent non-empty partial text shown to the client for the in-flight
        # turn. If the whole-turn final decode comes back empty for a turn the user
        # already watched being captioned word by word, this is surfaced as the final
        # instead of dropping the turn — the "words appear, then the whole turn
        # vanishes and the next one starts, as if never spoken" bug (XERK-174). It
        # shows up most on non-English speech, where the offline final decoder and the
        # cadence partials are most likely to disagree (the offline decode blanks a
        # turn the partial decode transcribed — e.g. a session pinned to one language
        # force-decoding another). Reset at every turn boundary.
        self._turn_partial = ""
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
        """Pay any per-session startup cost ahead of the first audio (XERK-128).

        The offline engine connects lazily per request and has no persistent
        per-session state to prime, so this is a no-op — kept to satisfy the
        `Transcriber` seam, which lets the session warm every backend uniformly."""
        return None

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
            self._turn_partial = text
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
        self._turn_partial = caption
        await self._queue.put(CaptionPartial(type="caption.partial", text=caption, lang=lang))

    async def _finalize(self) -> None:
        result = await self._run_engine(stage="final", want_words=self._final_words)
        start = self._segment_start_ms
        end = start + _bytes_to_ms(len(self._buf))

        # The last partial shown for this turn, captured before the per-turn state is
        # reset below — the fallback if the whole-turn decode comes back empty.
        fallback = self._turn_partial

        # Reset for the next segment before emitting so timing stays monotonic. The VAD
        # level window deliberately survives: it describes the *room*, which doesn't
        # change at a turn boundary, and re-learning it every turn would put the first
        # pause of each one back on the fixed threshold.
        self._segment_start_ms = end
        self._buf.clear()
        self._since_partial = 0
        self._trailing_silence = 0
        self._has_speech = False
        self._turn_partial = ""
        # The committed prefix belongs to the turn just closed; start the next turn's
        # word-by-word commit from scratch.
        if self._agreement is not None:
            self._agreement = LocalAgreement()

        text = result.text.strip()
        # An empty whole-turn decode used to drop the turn outright. But the client
        # already painted this turn word by word from the partials; dropping the final
        # now makes those words vanish as the next turn overwrites them — the turn
        # appears never to have been spoken (XERK-174). Most common on non-English
        # speech, where the offline final decoder and the cadence partials disagree.
        # If we actually showed the user a partial this turn, surface it as the final so
        # the words become a stable turn instead of disappearing; only a turn with no
        # partial at all (true silence / no speech) is still dropped. A recovered turn
        # has no reliable per-word timing, so it carries none — production runs with
        # word timing off regardless.
        recovered = False
        if not text:
            text = fallback.strip()
            if not text:
                return  # silence / no speech in this window — nothing to surface
            if len(text.split()) < _RECOVERY_MIN_WORDS:
                # The offline decode heard nothing in the whole turn and the
                # partial never got past bare filler: on real speech the partial
                # builds a clause, so a 1-2 word partial against an empty
                # whole-turn decode is overwhelmingly a hallucination on
                # non-speech audio (XERK-182), not a lost turn. Keep it dropped.
                metrics.incr("stage.stt.final_recovery_suppressed")
                return
            recovered = True
            metrics.incr("stage.stt.final_recovered")

        words = (
            None
            if recovered
            else (
                [
                    Word(
                        text=w.text,
                        startMs=max(0, start + int(w.start * 1000)),
                        endMs=max(0, start + int(w.end * 1000)),
                        confidence=w.probability,
                    )
                    for w in result.words
                ]
                or None
            )
        )
        # The finalized turn's language, engine-reported first. The deployed
        # Parakeet server transcribes multilingual speech but reports no detected
        # language (the NeMo hypothesis exposes none — recorded on session
        # a6ef5cad, a fully-Spanish conversation stored with every lang NULL, so
        # live translation XERK-160 never triggered). When neither the engine nor
        # a pinned session language names one, fall back to conservative
        # text-based identification of the final itself; an ambiguous turn stays
        # None, which decides nothing downstream.
        lang = _lang(result.language or self._language) or _lang(detect_lang(text))
        await self._queue.put(
            CaptionFinal(
                type="caption.final",
                segmentId=str(uuid.uuid4()),
                text=text,
                lang=lang,
                startMs=start,
                endMs=end,
                words=words,
            )
        )

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        # Keep draining after close: flush() queues the tail final right before
        # close() flips the flag (and puts the sentinel), and a consumer that is
        # still catching up must not drop them — stopping at the flag alone
        # lost the final turns of a session closed mid-drain.
        while not (self._closed and self._queue.empty()):
            yield await self._queue.get()

    async def flush(self) -> None:
        if self._buf and self._has_speech:
            await self._finalize()

    async def close(self) -> None:
        self._closed = True
        # Unblock a pending results() get with a skipped (empty) sentinel.
        await self._queue.put(CaptionPartial(type="caption.partial", text="", lang=None))
