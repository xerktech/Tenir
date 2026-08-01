"""Per-connection session.

Holds the live session identity and the STT seam, fans audio in, and pumps
caption results back out. Every finalized turn is persisted to the conversation
store as it lands, the full audio is retained in memory for the session and
flushed to the audio store on end — a recorded, stored STT session.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable

from api.config import settings
from api.contract import (
    CaptionFinal,
    CaptionPartial,
    Cue,
    Lang,
    LyricLine,
    MicSource,
    ServerMessage,
    SessionReady,
    Song,
    SongDone,
    SongSync,
    Translation,
    TranslationDone,
)
from api.cue import CueGenerator, make_cue_generator, min_interval_ms, normalize_cue_title
from api.cue.base import (
    CUE_SUBSTANCE_MIN_TOKENS,
    GeneratedCue,
    cue_subject_tokens,
    cue_substance_similarity,
    cue_substance_tokens,
)
from api.cue.retrieval import EvidenceRetriever, make_evidence_retriever
from api.metrics import metrics
from api.music import (
    MusicMatch,
    MusicService,
    make_music_service,
    scan_backoff_ms,
    track_key,
    window_bytes,
)
from api.persistence import (
    Cue as CueRecord,
    Segment,
    Song as SongRecord,
    audio_key,
    get_audio_store,
    get_conversation_store,
    pcm16_to_wav,
    wav_to_pcm16,
)
from api.stt import Transcriber, make_transcriber
from api.stt.engine import BYTES_PER_SEC
from api.translate import Translator, make_translator

log = logging.getLogger("api.session")

# Send a server message to the client. Returns when the frame is queued.
Sender = Callable[[ServerMessage], Awaitable[None]]

# Cap on messages buffered while detached (resume grace window): captions produced
# during the gap are replayed on rebind, but a never-resumed session must not grow
# without bound — keep the most recent ones.
_DETACHED_BUFFER_MAX = 500

# How many already-surfaced cue titles to hand the generator as "don't repeat"
# context (XERK-102). Bounds the prompt in a long conversation; the full set is
# still enforced by the post-hoc de-dupe, so nothing repeats beyond this window —
# it only limits how many the model is explicitly reminded of.
_CUE_AVOID_PROMPT_LIMIT = 40

# A new cue whose content-word fingerprint overlaps a surfaced cue's at or above
# this Jaccard similarity is the same fact reworded, not a new cue. Calibrated
# on recorded production sessions: near-verbatim rewords measure 0.57-0.87 and
# retitled same-fact paraphrases 0.30-0.38, while the closest genuinely distinct
# pairs (different facts about the same entity) top out at 0.26-0.27 — the two
# classes overlap below 0.30, so 0.35 is as low as the hard drop can safely go.
# Paraphrases that share fewer content words than that are the prompt
# avoid-list's job, not this backstop's (see cue/base.py).
_CUE_SUBSTANCE_DUP_THRESHOLD = 0.35


def _enum_str(value: object | None) -> str | None:
    """StrEnum members stringify to their value; plain strings pass through."""
    return str(value) if value is not None else None


def _same_text(a: str, b: str) -> bool:
    """Whether a translation is just its source echoed back (XERK-160): compared
    case-insensitively with whitespace collapsed, so cosmetic differences don't
    disguise an echo."""
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


class Session:
    def __init__(
        self, send: Sender, *, session_id: str | None = None, household: str | None = None
    ) -> None:
        self._send = send
        self.session_id = session_id or str(uuid.uuid4())
        self.resumed = bool(session_id)
        self.mic_source: MicSource | None = None
        self.source_lang: Lang | None = None
        self._transcriber: Transcriber | None = None
        self._pump: asyncio.Task[None] | None = None
        self._warmup: asyncio.Task[None] | None = None
        # Cues (XERK-81): a private context card the api derives from the running
        # transcript. Generation is off unless a backend is configured; when on, each
        # finalized turn feeds a rolling window to the cue model. Kept off the caption
        # path — a cue is a best-effort aside, produced in a background task so a slow
        # or failing model never stalls captions.
        self._cue_generator: CueGenerator | None = None
        # Evidence retrieval (XERK-120): live-source grounding for cue facts. None
        # when retrieval is off — cues then run ungrounded exactly as before.
        self._cue_retriever: EvidenceRetriever | None = None
        self._recent_finals: deque[str] = deque(maxlen=max(1, settings.cue_context_segments))
        # De-dupe cues for the WHOLE conversation, not a short rolling window
        # (XERK-102): a cue surfaced once must not pop up again later, however far
        # apart. ``_surfaced_cue_norms`` is the normalized-title membership set;
        # ``_surfaced_cues`` keeps the surfaced title+body pairs (order-preserving)
        # to hand the generator so it can steer clear of them and find fresh
        # context; ``_surfaced_cue_substance`` holds their content-word
        # fingerprints, the backstop against the same fact returning under a
        # fresh title and reworded body; ``_surfaced_cue_subjects`` is the
        # union of surfaced titles' distinctive subject words — one subject
        # gets one cue per conversation, so "Grafana origin" cannot follow
        # "Grafana" and a tenth SAML-titled cue cannot follow the first.
        self._surfaced_cue_norms: set[str] = set()
        self._surfaced_cues: list[GeneratedCue] = []
        self._surfaced_cue_substance: list[frozenset[str]] = []
        self._surfaced_cue_subjects: set[str] = set()
        self._last_cue_monotonic: float | None = None
        self._cue_inflight = False
        self._cue_tasks: set[asyncio.Task[None]] = set()
        # Live translations (XERK-160): when a finalized turn's detected language
        # isn't English, it is translated off the caption path and delivered as a
        # `translation` message paired to the segment. Consecutive non-English
        # turns form a RUN: while one is live, cues are suppressed; the run ends
        # when an English turn arrives or speech goes quiet past the hold window,
        # which emits `translation.done` (the glasses start their dismiss
        # countdown on it) and lets cues resume. Translation calls are serialized
        # through a single worker + queue so translations reach the client in
        # transcript order even when the model is slow; the `done` marker rides
        # the same queue, so it always follows the run's last translation.
        self._translator: Translator | None = None
        self._translation_queue: asyncio.Queue[tuple[str, CaptionFinal | None]] | None = None
        self._translation_worker: asyncio.Task[None] | None = None
        self._translation_hold: asyncio.Task[None] | None = None
        self._translation_active = False
        # Music ID (XERK-184): when a song is playing, the session periodically
        # fingerprints a short window of the live audio, identifies the track, and
        # shows its time-synced lyrics in the cue box, auto-scrolling as the song
        # plays. Unlike cues/translations this is AUDIO-driven — it taps on_audio
        # into a bounded rolling window and runs on its OWN scan loop, not off the
        # transcript. A confident match opens a `song` run (full lyrics + a sync
        # anchor); while the same song stays locked, periodic re-identification
        # sends `song.sync` to correct scroll drift; when it stops matching past
        # the hold window the run ends with `song.done`. A live song run suppresses
        # cues, exactly as a translation run does (the box is shared).
        self._music: MusicService | None = None
        self._music_scan: asyncio.Task[None] | None = None
        self._music_audio = bytearray()
        self._music_window_bytes = 0
        self._music_active = False
        self._music_run_id: str | None = None
        self._music_track_key: str | None = None
        self._music_last_match_monotonic: float | None = None
        self._music_misses = 0
        # A different track seen ONCE while a song is locked (XERK-187). Crossfades
        # and DJ blends make single scans flap between two tracks; a takeover only
        # happens when the newcomer matches twice in a row, so the box doesn't
        # reset on every blended window.
        self._music_pending_key: str | None = None
        # Running total of audio pushed this sitting, so the music scan can stamp a
        # recognized song at the current session-timeline position (the same
        # timeline cues/segments use), independent of the STT seam.
        self._start_offset_ms = 0
        self._audio_bytes_pushed = 0
        # Persistence: the household scopes the conversation store; with auth on it
        # comes from the authenticated principal, else the configured default. The
        # full-audio buffer is the retained record, flushed to the audio store on end.
        self._household = household or settings.household_id
        self._conversations = get_conversation_store()
        self._audio_store = get_audio_store()
        self._full_audio = bytearray()
        # Resume support: on a socket drop (not an explicit session.end) the
        # connection is *detached* and the session is kept alive for a grace window
        # so a reconnect carrying the same id rebinds to it — preserving the
        # transcriber state instead of resetting it.
        self._closed = False
        self._detached = False
        self._grace_task: asyncio.Task[None] | None = None
        # Messages produced while detached are buffered here and replayed on rebind
        # so a brief drop doesn't silently lose captions.
        self._detached_buffer: list[ServerMessage] = []

    @property
    def household(self) -> str | None:
        return self._household

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def current_send(self) -> Sender:
        """The sender this session is currently bound to (identity-compared by the
        WS handler so a stale handler never detaches a freshly-resumed session)."""
        return self._send

    async def start(
        self,
        *,
        mic_source: MicSource,
        source_lang: Lang | None,
    ) -> None:
        self.mic_source = mic_source
        self.source_lang = source_lang
        # Build the transcriber now that we know the source language. A resumed
        # conversation seeds the segment timeline with the duration already
        # retained: this Session's transcriber starts a fresh byte count, but the
        # conversation's transcript and audio don't — _persist appends this
        # sitting's audio after the existing recording, so segments must continue
        # that timeline too or the merged transcript interleaves sittings and
        # History playback desyncs from the audio.
        start_offset_ms = await self._resume_offset_ms() if self.resumed else 0
        self._start_offset_ms = start_offset_ms
        self._transcriber = make_transcriber(
            source_lang=source_lang, start_offset_ms=start_offset_ms
        )
        # Build the cue generator (None when API_CUE_BACKEND=off — the default —
        # so the stripped core does no cue work at all).
        self._cue_generator = make_cue_generator()
        # And its evidence retriever (XERK-120); only meaningful with cues on.
        if self._cue_generator is not None:
            self._cue_retriever = make_evidence_retriever()
        # The translator (XERK-160); None when API_TRANSLATION_BACKEND=off, so the
        # stripped core does no translation work at all.
        self._translator = make_translator()
        if self._translator is not None:
            self._translation_queue = asyncio.Queue()
            self._translation_worker = asyncio.create_task(self._translation_worker_loop())
        # The music identifier (XERK-184); None when API_MUSIC_BACKEND=off, so the
        # stripped core does no music work — on_audio doesn't even buffer for it.
        self._music = make_music_service()
        if self._music is not None:
            self._music_window_bytes = window_bytes(settings.music_window_seconds)
            self._music_scan = asyncio.create_task(self._music_scan_loop())
        if self._conversations is not None:
            # Idempotent: a resumed session keeps appending to its existing record.
            # Offloaded: a real (Postgres) store blocks, and this is on the connect
            # path — never run a blocking store call on the event loop.
            await asyncio.to_thread(
                self._conversations.create,
                self._household,
                self.session_id,
                mic_source=_enum_str(mic_source),
                source_lang=_enum_str(source_lang),
            )
        self._pump = asyncio.create_task(self._pump_results())
        # Warm the transcriber's per-session startup cost now, off the caption path,
        # so the first spoken words don't wait behind it (XERK-128). Best-effort and
        # backgrounded: the ready message and the first audio never block on it, and a
        # resumed session already has a warm transcriber so there's nothing to redo —
        # but re-warming is a cheap no-op there anyway.
        self._warmup = asyncio.create_task(self._transcriber.warmup())
        self._warmup.add_done_callback(self._on_warmup_done)
        await self._send(
            SessionReady(type="session.ready", sessionId=self.session_id, resumed=self.resumed)
        )
        log.info("session %s ready (mic=%s)", self.session_id, mic_source)

    async def _resume_offset_ms(self) -> int:
        """Where a resumed conversation's timeline stands, in ms.

        The retained audio is authoritative when it exists — _persist prepends it
        before this sitting's audio, so its duration is exactly where the new
        audio (and therefore the new segments) begins. Without retained audio
        (audio backend off, or a memory backend emptied by a restart) fall back to
        the last persisted segment's end so the transcript at least stays
        monotonic. Store reads are offloaded: both backends block, and this runs
        on the connect path.
        """
        if self._audio_store is not None:
            existing = await asyncio.to_thread(
                self._audio_store.get, audio_key(self._household, self.session_id)
            )
            if existing:
                return len(wav_to_pcm16(existing)) * 1000 // BYTES_PER_SEC
        if self._conversations is not None:
            conv = await asyncio.to_thread(
                self._conversations.get, self._household, self.session_id
            )
            if conv is not None and conv.segments:
                return max(s.end_ms for s in conv.segments)
        return 0

    def _on_warmup_done(self, task: asyncio.Task[None]) -> None:
        # warmup() swallows its own errors, but retrieve any exception (incl. a
        # cancel on teardown) so it never surfaces as an unretrieved task warning.
        if task.cancelled():
            return
        if (exc := task.exception()) is not None:
            log.warning("session %s STT warmup failed", self.session_id, exc_info=exc)

    async def on_audio(self, pcm: bytes) -> None:
        # Retain the full audio for the stored session: buffered in memory for the
        # session, flushed to the audio store on end.
        if self._audio_store is not None:
            self._full_audio.extend(pcm)
        # Track the session-timeline position so the music scan can stamp a song
        # at "now" (cheap counter; independent of any backend being on).
        self._audio_bytes_pushed += len(pcm)
        # Music ID (XERK-184): keep a bounded rolling window of the most recent
        # audio for the scan loop to fingerprint. The one place music diverges
        # from cues/translations — it needs the audio, not the transcript.
        if self._music is not None:
            self._music_audio.extend(pcm)
            excess = len(self._music_audio) - self._music_window_bytes
            if excess > 0:
                del self._music_audio[:excess]
        if self._transcriber is not None:
            await self._transcriber.push(pcm)

    async def _buffer_send(self, msg: ServerMessage) -> None:
        """Sink used while detached: hold messages for replay on resume, capped so a
        never-resumed session can't grow without bound (keeps the most recent)."""
        self._detached_buffer.append(msg)
        if len(self._detached_buffer) > _DETACHED_BUFFER_MAX:
            del self._detached_buffer[: len(self._detached_buffer) - _DETACHED_BUFFER_MAX]

    async def rebind(self, send: Sender) -> None:
        """Reattach a resumed connection's sender, cancelling any pending grace close.

        The live transcriber and buffers are untouched, so captions pick up where
        the drop left off; messages produced during the gap are replayed to the new
        socket in order.
        """
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
        self._send = send
        self._detached = False
        self.resumed = True
        if self._detached_buffer:
            buffered, self._detached_buffer = self._detached_buffer, []
            for msg in buffered:
                await send(msg)

    def detach(self, *, grace_seconds: float) -> None:
        """Connection dropped without an explicit end: keep the session alive for a
        grace window so a resume can rebind it, instead of finalizing immediately.

        Until then sends are buffered (the socket is gone) and replayed on resume; if
        no resume arrives the grace task finalizes and unregisters the session.
        """
        if self._closed or self._detached:
            return
        self._detached = True
        self._send = self._buffer_send
        if grace_seconds <= 0:
            # Resume disabled — finalize on the next loop turn.
            self._grace_task = asyncio.create_task(self._grace_close(0))
        else:
            self._grace_task = asyncio.create_task(self._grace_close(grace_seconds))

    async def _grace_close(self, grace_seconds: float) -> None:
        try:
            if grace_seconds > 0:
                await asyncio.sleep(grace_seconds)
        except asyncio.CancelledError:
            return  # resumed — rebind() cancelled us
        if not self._detached or self._closed:
            return
        # Import here to avoid a circular import at module load (registry only needs
        # Session for typing).
        from api import registry

        registry.unregister(self)
        await self.close()

    def set_mic_source(self, mic_source: MicSource) -> None:
        self.mic_source = mic_source
        log.info("session %s mic -> %s", self.session_id, mic_source)

    async def _pump_results(self) -> None:
        assert self._transcriber is not None
        try:
            await self._drain_results()
        except Exception:
            # A failing STT seam must not take the whole connection down with an
            # unretrieved task exception. Log, count, and let the pump exit cleanly —
            # captions stop, the session lives.
            log.exception("session %s STT pump failed", self.session_id)
            metrics.incr("stage.stt.errors")

    async def _drain_results(self) -> None:
        assert self._transcriber is not None
        async for result in self._transcriber.results():
            if isinstance(result, CaptionPartial) and not result.text:
                continue  # close() sentinel
            try:
                await self._send(result)
            except Exception:
                # The socket can be gone before the drain finishes: a client that
                # sends session.end and closes immediately is torn down while the
                # end-of-session flush is still producing finals. Delivery is
                # best-effort, the transcript is not — swallow the send failure and
                # keep draining so those turns are still persisted below (XERK-58).
                log.warning("session %s could not deliver a caption (client gone)", self.session_id)
                metrics.incr("caption.send_errors")
            metrics.incr(
                "caption.partial" if isinstance(result, CaptionPartial) else "caption.final"
            )
            if isinstance(result, CaptionFinal) and self._conversations is not None:
                # Persist the finalized turn to the conversation transcript.
                # Offloaded: a real (Postgres) store does a blocking round-trip;
                # running it on the loop would freeze every live session for its
                # duration. The caption was already sent (or dropped) above.
                await asyncio.to_thread(
                    self._conversations.add_segment,
                    self._household,
                    self.session_id,
                    Segment(
                        segment_id=result.segmentId,
                        text=result.text,
                        start_ms=result.startMs,
                        end_ms=result.endMs,
                        lang=result.lang.value if result.lang is not None else None,
                    ),
                )
            if isinstance(result, CaptionPartial):
                # Speech is still flowing: keep a live translation run's silence
                # hold from expiring mid-utterance (finals only land at pauses).
                self._touch_translation_hold()
            if isinstance(result, CaptionFinal):
                # A non-English turn opens/extends a translation run (XERK-160);
                # an English one closes it. Considered before the cue so the same
                # turn that opens a run never also produces a cue.
                self._consider_translation(result)
                # A finalized turn may be cue-worthy; consider it out of band.
                self._consider_cue(result)

    def _consider_translation(self, result: CaptionFinal) -> None:
        """Track the translation run across finalized turns (XERK-160).

        A non-English turn (re)opens the run and queues its translation; an
        English turn while a run is live means the other language is done being
        spoken, so the run closes. A turn with no detected language INHERITS an
        open run — the speaker is mid-run and only an explicit English turn
        closes one, so an undecidable turn between two Spanish turns (a
        proper-noun list like "Mercurio, Venus, Tierra, Marte.") is
        overwhelmingly a continuation and gets translated with the rest; it is
        queued without a claimed source language so the model reads the text
        for itself. Outside a run an undecidable turn decides nothing — there
        the same call would be a guess.
        """
        if self._translator is None:
            return
        lang = result.lang.value if result.lang is not None else None
        if lang is not None and lang != "en":
            assert self._translation_queue is not None
            self._translation_active = True
            self._touch_translation_hold()
            self._translation_queue.put_nowait(("translate", result))
        elif lang == "en" and self._translation_active:
            self._end_translation_run()
        elif lang is None and self._translation_active:
            assert self._translation_queue is not None
            self._touch_translation_hold()
            self._translation_queue.put_nowait(("translate", result))

    def _touch_translation_hold(self) -> None:
        """Restart the run's silence hold: any speech activity (a partial or a
        final) means the speaker isn't done yet, so the countdown starts over."""
        if not self._translation_active:
            return
        if self._translation_hold is not None:
            self._translation_hold.cancel()
        self._translation_hold = asyncio.create_task(self._translation_hold_expire())

    async def _translation_hold_expire(self) -> None:
        try:
            await asyncio.sleep(max(0, settings.translation_hold_ms) / 1000)
        except asyncio.CancelledError:
            return
        self._end_translation_run()

    def _end_translation_run(self) -> None:
        """The other language is done being spoken: close the run and queue the
        `translation.done` marker behind any still-pending translations, so the
        client's dismiss countdown never starts before the run's last translation
        is on screen. Cues resume from here (the gate reads this flag)."""
        if not self._translation_active:
            return
        assert self._translation_queue is not None
        self._translation_active = False
        if self._translation_hold is not None:
            self._translation_hold.cancel()
            self._translation_hold = None
        self._translation_queue.put_nowait(("done", None))

    async def _translation_worker_loop(self) -> None:
        """Serialized translation delivery: one queue, one worker, transcript
        order preserved no matter how slow individual model calls are."""
        assert self._translation_queue is not None
        while True:
            kind, final = await self._translation_queue.get()
            try:
                if kind == "stop":
                    return
                if kind == "done":
                    try:
                        await self._send(TranslationDone(type="translation.done"))
                    except Exception:
                        log.warning(
                            "session %s could not deliver translation.done (client gone)",
                            self.session_id,
                        )
                        metrics.incr("translation.send_errors")
                    continue
                assert final is not None
                await self._translate_final(final)
            finally:
                # Matched to the get() above so Queue.join() tracks the backlog
                # (tests await it to know the worker is idle).
                self._translation_queue.task_done()

    async def _translate_final(self, final: CaptionFinal) -> None:
        """Translate one finalized turn and deliver + persist the result.
        Best-effort throughout, like cues: any failure is logged/counted and
        swallowed so the caption stream is never disturbed."""
        assert self._translator is not None
        lang = final.lang.value if final.lang is not None else None
        try:
            with metrics.timer("translation.ms"):
                translated = await asyncio.to_thread(
                    self._translator.translate, final.text, source_lang=lang
                )
        except Exception:
            log.warning("session %s translation failed", self.session_id, exc_info=True)
            metrics.incr("translation.errors")
            return
        if not translated:
            return
        if _same_text(translated, final.text):
            # The "translation" is the original — an English turn that reached
            # the queue as an ambiguous run-continuation (the prompt returns
            # already-English text unchanged). An echo adds nothing to the
            # listener, so it is dropped rather than rendered twice.
            metrics.incr("translation.echo_drops")
            return
        try:
            await self._send(
                Translation(
                    type="translation",
                    segmentId=final.segmentId,
                    text=translated,
                    sourceLang=final.lang,
                )
            )
        except Exception:
            # Like captions, delivery is best-effort but the record is not:
            # persist below even if the socket is gone.
            log.warning(
                "session %s could not deliver a translation (client gone)", self.session_id
            )
            metrics.incr("translation.send_errors")
        metrics.incr("translation.emitted")
        if self._conversations is not None:
            await asyncio.to_thread(
                self._conversations.set_segment_translation,
                self._household,
                self.session_id,
                final.segmentId,
                translated,
            )

    def _consider_cue(self, result: CaptionFinal) -> None:
        """On each finalized turn, maybe kick off cue generation in the background.

        Cheap gating happens here on the event loop (no model call): skip when cues
        are off, while one is already in flight, or inside the fixed rate-limit
        window. Only past those does it spawn a task that calls the model off-loop.
        """
        if self._cue_generator is None:
            return
        if result.text:
            self._recent_finals.append(result.text)
        if self._translation_active:
            # Live translation run (XERK-160): cues neither trigger nor appear
            # while the translation box owns their slot; they resume once the
            # run ends. The turn's text still joined the context window above.
            return
        if self._music_active:
            # A song is playing (XERK-184): its lyrics own the box, so cues stand
            # aside exactly as they do for a translation run; they resume once the
            # song ends. The turn's text still joined the context window above.
            return
        if self._cue_inflight:
            return
        if self._last_cue_monotonic is not None:
            elapsed_ms = (time.monotonic() - self._last_cue_monotonic) * 1000
            if elapsed_ms < min_interval_ms():
                return
        self._cue_inflight = True
        task = asyncio.create_task(self._generate_cue(result.endMs))
        self._cue_tasks.add(task)
        task.add_done_callback(self._cue_tasks.discard)

    async def _generate_cue(self, at_ms: int) -> None:
        """Run the cue model over the recent transcript and, on a hit, deliver +
        persist the cue. Best-effort throughout: any failure is logged/counted and
        swallowed so the caption stream is never disturbed."""
        try:
            transcript = "\n".join(t for t in self._recent_finals if t)
            if not transcript.strip():
                return
            assert self._cue_generator is not None
            # Steer the generator away from cues already surfaced this conversation
            # (XERK-102) so it finds fresh context instead of re-proposing an old one.
            # Only the most recent cues ride the prompt (older ones are still
            # caught by the full backstop sets below), so the ask stays compact.
            avoid = list(self._surfaced_cues[-_CUE_AVOID_PROMPT_LIMIT:])
            # Gather live-source evidence first (XERK-120). The retriever bounds
            # its own latency (deadline inside), and failure means an ungrounded
            # cue, never a missing one — evidence is an upgrade, not a gate.
            evidence = []
            if self._cue_retriever is not None:
                try:
                    with metrics.timer("cue.retrieval_ms"):
                        evidence = await self._cue_retriever.retrieve(list(self._recent_finals))
                except Exception:
                    log.warning(
                        "session %s cue evidence retrieval failed", self.session_id, exc_info=True
                    )
                    metrics.incr("cue.retrieval.errors")
            generated = await asyncio.to_thread(
                self._cue_generator.generate,
                transcript,
                avoid_cues=avoid,
                evidence=evidence,
            )
            if generated is None:
                return
            title = generated.title.strip()
            body = generated.body.strip()
            if not title or not body:
                return
            if self._translation_active or self._music_active:
                # A translation run (XERK-160) or a song (XERK-184) opened while
                # this cue was generating: it must not appear mid-run. Dropped
                # entirely — not surfaced, not recorded — so the fact stays
                # available for later.
                metrics.incr("cue.translation_drops")
                return
            # Backstop de-dupe: never surface the same cue twice in a conversation,
            # however far apart (XERK-102) — the report was an old cue popping up
            # again later once it had aged out of a short rolling window. Two
            # layers: the normalized title, and the content-word fingerprint that
            # catches the same fact returning under a fresh title and reworded
            # body (three "drone factory" cues in one recorded session). A repeat
            # is dropped WITHOUT resetting the rate-limit clock, so the next turn
            # can immediately try again for a genuinely new cue.
            norm = normalize_cue_title(title)
            if norm in self._surfaced_cue_norms:
                metrics.incr("cue.dedupe_drops")
                return
            substance = cue_substance_tokens(title, body)
            if len(substance) >= CUE_SUBSTANCE_MIN_TOKENS and any(
                len(prior) >= CUE_SUBSTANCE_MIN_TOKENS
                and cue_substance_similarity(substance, prior) >= _CUE_SUBSTANCE_DUP_THRESHOLD
                for prior in self._surfaced_cue_substance
            ):
                metrics.incr("cue.dedupe_drops")
                return
            # Third layer: same SUBJECT at a new angle ("Grafana origin" after
            # "Grafana"; ten SAML cues in one recorded session). The prompt
            # already bans this ("a definition, a mechanism, and a piece of
            # history about one thing are all the SAME cue"); this enforces the
            # ban when the model ignores it. Reworded angle-repeats measure
            # 0.04-0.33 substance Jaccard — inside the genuinely-distinct band,
            # unreachable by any threshold — but their titles share a
            # distinctive word. One subject, one cue.
            subject = cue_subject_tokens(title)
            if subject & self._surfaced_cue_subjects:
                metrics.incr("cue.subject_drops")
                return
            self._surfaced_cue_subjects |= subject
            self._surfaced_cue_norms.add(norm)
            self._surfaced_cues.append(GeneratedCue(title=title, body=body))
            self._surfaced_cue_substance.append(substance)
            self._last_cue_monotonic = time.monotonic()
            cue_id = uuid.uuid4().hex
            source = generated.source
            try:
                await self._send(
                    Cue(
                        type="cue",
                        cueId=cue_id,
                        title=title,
                        body=body,
                        atMs=at_ms,
                        source=source,
                    )
                )
            except Exception:
                # Like captions, delivery is best-effort but the record is not:
                # persist below even if the socket is gone.
                log.warning("session %s could not deliver a cue (client gone)", self.session_id)
                metrics.incr("cue.send_errors")
            metrics.incr("cue.emitted")
            if source:
                metrics.incr("cue.grounded")
            if self._conversations is not None:
                await asyncio.to_thread(
                    self._conversations.add_cue,
                    self._household,
                    self.session_id,
                    CueRecord(cue_id=cue_id, title=title, body=body, at_ms=at_ms, source=source),
                )
        except Exception:
            log.warning("session %s cue generation failed", self.session_id, exc_info=True)
            metrics.incr("cue.errors")
        finally:
            self._cue_inflight = False

    # ---- Music ID (XERK-184) -------------------------------------------------

    def _current_audio_ms(self) -> int:
        """The session-timeline position of the most recent audio, in ms — where a
        recognized song is stamped (same timeline as cues/segments)."""
        return self._start_offset_ms + self._audio_bytes_pushed * 1000 // BYTES_PER_SEC

    def _music_window_wav(self) -> bytes | None:
        """The current rolling window as a WAV, or None until enough audio has
        accumulated. Requires ~5s so a landmark match has something to work with;
        below that a scan would just waste a recognition call."""
        min_bytes = min(self._music_window_bytes, window_bytes(5.0))
        if len(self._music_audio) < min_bytes or min_bytes == 0:
            return None
        return pcm16_to_wav(bytes(self._music_audio))

    async def _music_scan_loop(self) -> None:
        """Periodically fingerprint the audio window and drive the song run.

        Own background task, not the transcript pump: music is audio-driven. The
        cadence adapts — a locked song only needs its anchor nudged
        (`music_lock_interval_ms`), while searching backs off on repeated misses
        so an idle session isn't scanned at full rate. Any failure is swallowed;
        captions are never touched."""
        try:
            while not self._closed:
                if self._music_active and self._music_pending_key is None:
                    interval = max(1, settings.music_lock_interval_ms)
                elif self._music_active:
                    # A takeover candidate is waiting for its confirming scan
                    # (XERK-187): re-check at the search cadence so a real song
                    # change isn't held up by the slower lock interval.
                    interval = max(1, settings.music_scan_interval_ms)
                else:
                    interval = scan_backoff_ms(
                        self._music_misses,
                        base_ms=max(1, settings.music_scan_interval_ms),
                        cap_ms=max(1, settings.music_scan_max_interval_ms),
                    )
                await asyncio.sleep(interval / 1000)
                if self._closed:
                    return
                await self._scan_music_once()
        except asyncio.CancelledError:
            return
        except Exception:
            log.warning("session %s music scan loop failed", self.session_id, exc_info=True)
            metrics.incr("music.errors")

    async def _scan_music_once(self) -> None:
        """One scan step: fingerprint the window and open / re-sync / end a run."""
        if self._music is None:
            return
        if self._translation_active:
            # A translation run owns the box (translation > song > cue); don't
            # open or refresh a song while someone is being translated.
            return
        wav = self._music_window_wav()
        if wav is None:
            return
        try:
            match = await self._music.identify(wav)
        except Exception:
            log.warning("session %s music identify failed", self.session_id, exc_info=True)
            metrics.incr("music.errors")
            match = None
        now = time.monotonic()
        if match is None or match.confidence < settings.music_min_confidence:
            self._music_misses += 1
            if self._music_active and self._music_last_match_monotonic is not None:
                quiet_ms = (now - self._music_last_match_monotonic) * 1000
                if quiet_ms >= max(0, settings.music_hold_ms):
                    await self._end_music_run()
            return
        self._music_misses = 0
        key = match.track_key or track_key(match.artist, match.title)
        at_ms = self._current_audio_ms()
        if self._music_active and key == self._music_track_key:
            # The locked song again: refresh its clock and drop any takeover
            # candidate — the previous differing scan was a blip, not a change.
            self._music_last_match_monotonic = now
            self._music_pending_key = None
            await self._send_song_sync(match, at_ms)
        elif self._music_active and key != self._music_pending_key:
            # A different track while a song is locked (XERK-187): note it and
            # wait for a confirming scan. Crossfaded windows flap between the
            # outgoing and incoming track; replacing on one sighting resets the
            # box (done → new → done) several times per blend. The locked song's
            # hold clock keeps running — if the newcomer never confirms and the
            # locked song never returns, the hold ends the run here just as a
            # plain miss would.
            self._music_pending_key = key
            if self._music_last_match_monotonic is not None:
                quiet_ms = (now - self._music_last_match_monotonic) * 1000
                if quiet_ms >= max(0, settings.music_hold_ms):
                    await self._end_music_run()
        else:
            # Not active (first sighting opens immediately — nothing to protect),
            # or the pending track matched twice in a row: (re)open the run.
            self._music_last_match_monotonic = now
            self._music_pending_key = None
            await self._open_music_run(match, key, at_ms)

    async def _open_music_run(self, match: MusicMatch, key: str, at_ms: int) -> None:
        """A new song took over: close any prior run, fetch its lyrics, deliver the
        `song` frame, and persist the identity. Best-effort throughout."""
        if self._music_active:
            # A different song replaced the last one: close the old run cleanly so
            # the client's box (and the glasses countdown) resets before the new one.
            await self._end_music_run()
        try:
            synced = await self._music.lyrics(match)
        except Exception:
            log.warning("session %s lyric lookup failed", self.session_id, exc_info=True)
            synced = []
        song_id = uuid.uuid4().hex
        self._music_run_id = song_id
        self._music_track_key = key
        self._music_active = True
        lines = [LyricLine(atMs=max(0, ln.at_ms), text=ln.text) for ln in synced]
        try:
            await self._send(
                Song(
                    type="song",
                    songId=song_id,
                    title=match.title,
                    artist=match.artist,
                    atMs=at_ms,
                    offsetMs=max(0, match.offset_ms),
                    durationMs=match.duration_ms,
                    lines=lines,
                )
            )
        except Exception:
            # Like captions, delivery is best-effort but the record is not.
            log.warning("session %s could not deliver a song (client gone)", self.session_id)
            metrics.incr("music.send_errors")
        metrics.incr("music.emitted")
        if self._conversations is not None:
            await asyncio.to_thread(
                self._conversations.add_song,
                self._household,
                self.session_id,
                SongRecord(
                    song_id=song_id,
                    title=match.title,
                    artist=match.artist,
                    at_ms=at_ms,
                    duration_ms=match.duration_ms,
                ),
            )

    async def _send_song_sync(self, match: MusicMatch, at_ms: int) -> None:
        """Re-anchor the locked song so the client corrects scroll drift."""
        if self._music_run_id is None:
            return
        try:
            await self._send(
                SongSync(
                    type="song.sync",
                    songId=self._music_run_id,
                    atMs=at_ms,
                    offsetMs=max(0, match.offset_ms),
                )
            )
        except Exception:
            log.warning("session %s could not deliver song.sync (client gone)", self.session_id)
            metrics.incr("music.send_errors")
        metrics.incr("music.sync")

    async def _end_music_run(self) -> None:
        """The song is over (stopped matching past the hold, or replaced): close
        the run and tell the client, so its box dismisses and cues resume."""
        if not self._music_active:
            return
        song_id = self._music_run_id
        self._music_active = False
        self._music_run_id = None
        self._music_track_key = None
        self._music_last_match_monotonic = None
        self._music_pending_key = None
        if song_id is not None:
            try:
                await self._send(SongDone(type="song.done", songId=song_id))
            except Exception:
                log.warning(
                    "session %s could not deliver song.done (client gone)", self.session_id
                )
                metrics.incr("music.send_errors")
        metrics.incr("music.done")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
        # A still-running warmup would race the flush/close below (both drive the same
        # stream session): cancel it so teardown owns the transcriber cleanly.
        if self._warmup is not None:
            self._warmup.cancel()
            self._warmup = None
        # A failing STT seam can raise from flush()/close() too; guard it so
        # teardown still persists the conversation and never leaks an exception
        # out of close().
        if self._transcriber is not None:
            try:
                await self._transcriber.flush()
                await self._transcriber.close()
            except Exception:
                log.exception("session %s transcriber flush/close failed", self.session_id)
                metrics.incr("stage.stt.errors")
        if self._pump is not None:
            await self._pump
        if self._translation_worker is not None:
            # The pump is done, so every translate job is queued. A run still open
            # at teardown is over by definition (queues the `done` marker); then a
            # stop sentinel lets the worker drain pending translations first, so
            # the tail turns still persist translated. Bounded: a wedged model
            # call must not hold teardown hostage.
            self._end_translation_run()
            assert self._translation_queue is not None
            self._translation_queue.put_nowait(("stop", None))
            try:
                await asyncio.wait_for(self._translation_worker, timeout=15)
            except asyncio.TimeoutError:
                log.warning("session %s translation drain timed out", self.session_id)
            self._translation_worker = None
        if self._translation_hold is not None:
            self._translation_hold.cancel()
            self._translation_hold = None
        # Stop the music scan loop and release its service (XERK-184). A song run
        # left open just ends with the session — the client is tearing down too.
        if self._music_scan is not None:
            self._music_scan.cancel()
            try:
                await self._music_scan
            except asyncio.CancelledError:
                pass
            self._music_scan = None
        if self._music is not None:
            try:
                await self._music.close()
            except Exception:
                log.warning("session %s music service close failed", self.session_id)
        if self._cue_retriever is not None:
            try:
                await self._cue_retriever.close()
            except Exception:
                log.warning("session %s cue retriever close failed", self.session_id)
        await self._persist()
        log.info("session %s closed", self.session_id)

    async def _persist(self) -> None:
        """Retain the full audio and finalize the conversation.

        The store/audio calls are offloaded to threads: under the real Postgres +
        disk backends they block, and this runs on the event loop during teardown —
        blocking it would freeze every other live session.
        """
        if self._conversations is None:
            return
        # Persist retained audio, then point the conversation at it.
        if self._audio_store is not None and self._full_audio:
            key = audio_key(self._household, self.session_id)
            pcm = bytes(self._full_audio)
            # Extend, don't overwrite. A session that resumes after the grace window
            # has lapsed reaches the api as a *new* Session on the same conversation
            # id, so its buffer holds only the post-resume audio — the glasses do
            # exactly this, persisting their session id across drops and relaunches.
            # Prepend whatever is already retained for this conversation so the stored
            # clip spans the whole session and stays replayable end to end, instead of
            # being clobbered with the latest fragment (XERK-86).
            existing = await asyncio.to_thread(self._audio_store.get, key)
            if existing:
                pcm = wav_to_pcm16(existing) + pcm
            wav = pcm16_to_wav(pcm)
            await asyncio.to_thread(self._audio_store.put, key, wav)
            await asyncio.to_thread(
                self._conversations.set_audio_key, self._household, self.session_id, key
            )
            self._full_audio.clear()
        await asyncio.to_thread(
            self._conversations.finish, self._household, self.session_id, status="ready"
        )
