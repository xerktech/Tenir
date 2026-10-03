"""Realtime streaming transcriber (master plan §5.2, Phase 1).

Turns a stream of small PCM chunks into the two caption flavours the contract
defines:

- `caption.partial` — fast, unstable hypothesis re-run on a cadence for the live
  caption band.
- `caption.final` — a stable segment with word timestamps, emitted when an
  energy-based VAD sees enough trailing silence (a turn boundary) or the segment
  hits a max length.

All model inference is delegated to a `WhisperEngine`, run off the event loop via
`asyncio.to_thread` by one per-session decode worker. `push()` only buffers audio,
runs the VAD and queues decode jobs — it never awaits a decode — so the WebSocket
frame loop keeps reading the socket (and its keepalive pongs) while a slow or hung
STT upstream is outstanding. Decoding inline on that loop dropped every open
session with 1011 whenever the upstream stalled past the ping timeout (XERK-1414,
XERK-1424). A slow model now degrades partials (at most one is ever outstanding;
cadences that fall due while one is pending are coalesced, so no partial decodes
seconds-old audio behind a backlog) instead of stalling the session; finals always
queue, in order, and a final that raises is retried through a bounded outage
(_FINAL_RETRY_BUDGET_S) rather than dropped. The windowing/VAD logic here is
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

import numpy as np

from api.contract import CaptionFinal, CaptionPartial, Lang, Word
from api.metrics import metrics
from api.stt.agreement import LocalAgreement
from api.stt.engine import BYTES_PER_SEC, EngineResult, WhisperEngine, pcm16_to_float32, rms
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

# Silence padded onto each side of a turn whose final decode came back empty, for one
# retry. The deployed Parakeet (TDT) deterministically decodes some windows of clear
# speech to nothing — ~3.5% of final decodes on an hour of continuous Spanish
# conversation (XERK-1414) — and the same audio with a little silence around it
# decodes normally (6/6 sampled 9 s windows with 30-41 reference words came back
# with text). Retrying first means those turns keep the accurate whole-turn decode
# instead of falling back to the partial text, or being dropped below the gate.
_EMPTY_FINAL_RETRY_PAD_MS = 500

# A partial whose audio has waited longer than this in the decode queue (behind a
# slow final) is dropped instead of decoded: a caption of seconds-old audio is not a
# live caption, and decoding it delays the next turn's final (XERK-1414's 1 s bound,
# kept now that decodes run off the intake path).
_PARTIAL_STALE_S = 1.0

# A final decode that raises (STT timeout/connect error) is retried with backoff
# instead of being treated as empty: during an outage no partial decodes either, so
# the XERK-174 partial fallback is empty and every turn spoken while STT was down
# vanished from the stored transcript (XERK-1499). Retrying holds the ordered worker,
# so later finals queue behind it in order and stale partials are dropped as usual.
# The budget counts from the first failure of the outage, not per turn, and resets on
# any successful decode: once an outage outlasts it, each queued turn gets one attempt
# and takes the old fallback, so a dead upstream can't hold the worker (or the
# translation hold on `finalizing`) for budget x turns. Session.close still caps the
# end-of-session flush at 15 s and drops whatever is left (XERK-1424).
#
# Only an outage is retried. The upstream can also fail one input deterministically
# (Parakeet 500s on 10-20 ms tails), and retrying that would hold every later turn for
# the whole budget — on a healthy upstream — and lose them all if the session ended
# meanwhile. So after a turn's first failure one second of silence is decoded as a
# probe: if that answers, the upstream is up and the turn takes the fallback at once.
# No probe while an outage is already known (_outage_since set).
_FINAL_RETRY_BUDGET_S = 60.0
_FINAL_RETRY_BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0)  # the last repeats
_PROBE_PCM = bytes(BYTES_PER_SEC)
_retry_sleep = asyncio.sleep  # module seam so tests drive the backoff clock


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
        # Final-in-flight tracking (Transcriber.finalizing): speech turns queued for
        # or in their final decode, and caption.finals queued that the consumer
        # hasn't finished handling yet.
        self._speech_finals_pending = 0
        self._finals_undelivered = 0
        # Segment times count audio bytes from here on. A resumed conversation
        # (a new Session on an existing conversation id) seeds this with the
        # duration already retained, so its segments continue the conversation's
        # timeline instead of restarting at 0 — restarting made the merged
        # transcript interleave sittings and desynced it from the stored audio,
        # which appends across sittings (see Session._persist).
        self._segment_start_ms = start_offset_ms

        self._queue: asyncio.Queue[CaptionPartial | CaptionFinal] = asyncio.Queue()
        self._closed = False

        # Decode jobs, run strictly in order by one worker task (started on first use):
        # ("partial", pcm, queued_at, True) or ("final", pcm, segment_start_ms,
        # has_speech); queued_at is perf_counter() at submit, for _PARTIAL_STALE_S.
        # Ordering is what keeps a turn's partials ahead of its final and lets the
        # worker own the per-turn state (_turn_partial, _agreement) without locking.
        self._jobs: asyncio.Queue[tuple[str, bytes, float, bool]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._partial_pending = False
        # perf_counter() of the first failed final decode of the current outage, or
        # None while the upstream answers (see _FINAL_RETRY_BUDGET_S).
        self._outage_since: float | None = None

    async def warmup(self) -> None:
        """Pay any per-session startup cost ahead of the first audio (XERK-128).

        The offline engine connects lazily per request and has no persistent
        per-session state to prime, so this is a no-op — kept to satisfy the
        `Transcriber` seam, which lets the session warm every backend uniformly."""
        return None

    @property
    def finalizing(self) -> bool:
        return self._speech_finals_pending > 0 or self._finals_undelivered > 0

    async def push(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._buf.extend(pcm)
        self._since_partial += len(pcm)
        self._update_vad(pcm)

        if len(self._buf) >= self._max_segment_bytes:
            self._close_turn()
        elif (
            self._has_speech
            and self._trailing_silence >= self._silence_bytes
            and len(self._buf) >= self._min_segment_bytes
        ):
            self._close_turn()
        elif self._has_speech and self._since_partial >= self._partial_bytes:
            if self._partial_pending:
                # The engine is behind: skip this cadence rather than queue stale
                # decodes. _since_partial keeps running, so the first chunk after the
                # pending partial lands schedules a fresh one.
                metrics.incr("stage.stt.partial_skipped_busy")
                return
            self._since_partial = 0
            buf = self._buf
            if self._agreement is None and self._partial_window_bytes:
                buf = buf[-self._partial_window_bytes :]
            self._partial_pending = True
            self._submit("partial", bytes(buf), time.perf_counter(), True)

    def _submit(self, kind: str, pcm: bytes, arg: float, has_speech: bool) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._work())
        self._jobs.put_nowait((kind, pcm, arg, has_speech))

    def _close_turn(self) -> None:
        """Hand the in-flight segment to the worker for its final decode and reset the
        ingestion state so the next turn's audio buffers immediately.

        The VAD level window deliberately survives: it describes the *room*, which
        doesn't change at a turn boundary, and re-learning it every turn would put the
        first pause of each one back on the fixed threshold."""
        start = self._segment_start_ms
        self._segment_start_ms = start + _bytes_to_ms(len(self._buf))
        if self._has_speech:
            self._speech_finals_pending += 1
        self._submit("final", bytes(self._buf), start, self._has_speech)
        self._buf.clear()
        self._since_partial = 0
        self._trailing_silence = 0
        self._has_speech = False

    async def _work(self) -> None:
        while True:
            kind, pcm, arg, has_speech = await self._jobs.get()
            try:
                if kind == "partial":
                    if time.perf_counter() - arg > _PARTIAL_STALE_S:
                        metrics.incr("stage.stt.partial_skipped_stale")
                    else:
                        await self._emit_partial(pcm)
                else:
                    try:
                        await self._finalize(pcm, int(arg), has_speech)
                    finally:
                        # After _finalize queued its caption.final (counted as
                        # undelivered), so `finalizing` never dips in between.
                        if has_speech:
                            self._speech_finals_pending -= 1
            except Exception:
                # A failed decode must not kill the worker: every later turn would
                # silently stop captioning. Count it and move on to the next job.
                log.exception("STT %s decode failed", kind)
                metrics.incr("stage.stt.errors")
            finally:
                if kind == "partial":
                    self._partial_pending = False
                self._jobs.task_done()

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
        self, pcm: bytes, *, stage: str, want_words: bool, pad_ms: int = 0
    ) -> EngineResult:
        # A legacy partial carries only the trailing partial window so its cost
        # doesn't grow with turn length; a final carries the whole segment for a
        # stable transcript. The inference time is recorded so the caption-path
        # latency budget (master plan §6) can actually be measured/tuned.
        samples = pcm16_to_float32(pcm)
        if pad_ms:
            pad = np.zeros(_ms_to_bytes(pad_ms) // 2, dtype=np.float32)
            samples = np.concatenate([pad, samples, pad])
        t0 = time.perf_counter()
        result = await asyncio.to_thread(
            self._engine.transcribe, samples, language=self._language, want_words=want_words
        )
        self._outage_since = None  # the upstream answered: any outage is over
        metrics.observe(f"stage.stt.{stage}_latency_ms", (time.perf_counter() - t0) * 1000)
        if pad_ms:
            # Word times back onto the unpadded turn's timeline (rounded to the ms so
            # float error can't shave a millisecond off when they're truncated later),
            # clamped to the turn: a word the model placed in either pad still lies
            # within the caption it belongs to.
            off = pad_ms / 1000
            dur = round(len(pcm) / BYTES_PER_SEC, 3)
            for w in result.words:
                w.start = min(dur, max(0.0, round(w.start - off, 3)))
                w.end = min(dur, max(0.0, round(w.end - off, 3)))
        return result

    async def _emit_partial(self, pcm: bytes) -> None:
        if self._agreement is None:
            # Legacy path: decode the trailing window and emit it verbatim, which
            # rewrites the whole caption line each cadence.
            result = await self._run_engine(pcm, stage="partial", want_words=False)
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
        result = await self._run_engine(pcm, stage="partial", want_words=False)
        lang = _lang(result.language or self._language)
        self._agreement.commit(result.text.split())
        caption = self._agreement.caption_text()
        if not caption:
            return
        self._turn_partial = caption
        await self._queue.put(CaptionPartial(type="caption.partial", text=caption, lang=lang))

    async def _retry_blank_final(self, pcm: bytes, blank: EngineResult) -> EngineResult:
        """Speech, but the whole-turn decode is blank: decode once more with silence
        padding (see _EMPTY_FINAL_RETRY_PAD_MS). Returns the retry's result when it
        carries a real turn, else ``blank`` so _finalize falls through to the XERK-174
        partial fallback exactly as without the retry:
        - a retry that raises (STT timeout/connect error) must not lose the turn — the
          exception would skip the per-turn reset and re-enter _finalize every frame;
        - a retry below _RECOVERY_MIN_WORDS is held to the same filler gate as a
          recovered partial (XERK-182): padding non-speech can conjure 1-2 words."""
        try:
            retry = await self._run_engine(
                pcm,
                stage="final_retry",
                want_words=self._final_words,
                pad_ms=_EMPTY_FINAL_RETRY_PAD_MS,
            )
        except Exception:
            log.warning("padded retry of a blank final failed", exc_info=True)
            metrics.incr("stage.stt.final_retry_errors")
            return blank
        if len(retry.text.split()) < _RECOVERY_MIN_WORDS:
            metrics.incr("stage.stt.final_retry_empty")
            return blank
        metrics.incr("stage.stt.final_retry_recovered")
        return retry

    async def _decode_final(self, pcm: bytes) -> EngineResult:
        """Whole-turn decode, retried with backoff while the upstream is down and the
        outage is within _FINAL_RETRY_BUDGET_S. Raises once the budget is spent, or at
        once when the upstream answers a probe (a failure of this input, not an outage)."""
        attempt = 0
        while True:
            try:
                return await self._run_engine(pcm, stage="final", want_words=self._final_words)
            except Exception:
                # Probe once, and not mid-outage: a dead upstream would only fail
                # the probe too, at the cost of another request timeout per turn.
                if attempt == 0 and self._outage_since is None and await self._upstream_answers():
                    metrics.incr("stage.stt.final_input_errors")
                    raise
                now = time.perf_counter()
                if self._outage_since is None:
                    self._outage_since = now
                metrics.incr("stage.stt.errors")
                delay = _FINAL_RETRY_BACKOFF_S[min(attempt, len(_FINAL_RETRY_BACKOFF_S) - 1)]
                if now + delay - self._outage_since > _FINAL_RETRY_BUDGET_S:
                    raise
                log.warning("STT final decode failed; retrying in %.1fs", delay, exc_info=True)
                metrics.incr("stage.stt.final_retries")
                attempt += 1
                await _retry_sleep(delay)

    async def _upstream_answers(self) -> bool:
        """Probe with silence after a failed final: True if the upstream is up."""
        try:
            await self._run_engine(_PROBE_PCM, stage="probe", want_words=False)
        except Exception:
            return False
        return True

    async def _finalize(self, pcm: bytes, start: int, has_speech: bool) -> None:
        try:
            result = await self._decode_final(pcm)
        except Exception:
            # The upstream rejects this input, or the outage outlasted the retry
            # budget: treat the turn as an empty decode — no padded retry against an
            # upstream that just failed it — so it still falls back to the partial the
            # user already watched, if there was one.
            log.exception("STT final decode failed")
            metrics.incr("stage.stt.final_retry_exhausted")
            result = EngineResult(text="", words=[], language=None)
        else:
            if not result.text.strip() and has_speech:
                result = await self._retry_blank_final(pcm, result)
        end = start + _bytes_to_ms(len(pcm))

        # The last partial shown for this turn, captured before the per-turn state is
        # reset below — the fallback if the whole-turn decode comes back empty.
        fallback = self._turn_partial
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
        self._finals_undelivered += 1
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
            result = await self._queue.get()
            yield result
            # Resumed only once the consumer has handled the final, so `finalizing`
            # stays set until the session has seen it (no gap for its hold to expire).
            if isinstance(result, CaptionFinal):
                self._finals_undelivered -= 1

    async def flush(self) -> None:
        """Finalize the in-flight turn and wait for every queued decode to land.
        Session.close bounds this wait, so a hung upstream can't hold teardown."""
        if self._buf and self._has_speech:
            self._close_turn()
        if self._worker is not None:
            await self._jobs.join()

    async def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
        # Unblock a pending results() get with a skipped (empty) sentinel.
        await self._queue.put(CaptionPartial(type="caption.partial", text="", lang=None))
