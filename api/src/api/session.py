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
    MicSource,
    ServerMessage,
    SessionReady,
)
from api.cue import CueGenerator, make_cue_generator, min_interval_ms, normalize_cue_title
from api.cue.base import (
    CUE_SUBSTANCE_MIN_TOKENS,
    GeneratedCue,
    cue_substance_similarity,
    cue_substance_tokens,
)
from api.cue.retrieval import EvidenceRetriever, make_evidence_retriever
from api.metrics import metrics
from api.persistence import (
    Cue as CueRecord,
    Segment,
    audio_key,
    get_audio_store,
    get_conversation_store,
    pcm16_to_wav,
    wav_to_pcm16,
)
from api.stt import Transcriber, make_transcriber

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
        # fresh title and reworded body.
        self._surfaced_cue_norms: set[str] = set()
        self._surfaced_cues: list[GeneratedCue] = []
        self._surfaced_cue_substance: list[frozenset[str]] = []
        self._last_cue_monotonic: float | None = None
        self._cue_inflight = False
        self._cue_tasks: set[asyncio.Task[None]] = set()
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
        # Build the transcriber now that we know the source language.
        self._transcriber = make_transcriber(source_lang=source_lang)
        # Build the cue generator (None when API_CUE_BACKEND=off — the default —
        # so the stripped core does no cue work at all).
        self._cue_generator = make_cue_generator()
        # And its evidence retriever (XERK-120); only meaningful with cues on.
        if self._cue_generator is not None:
            self._cue_retriever = make_evidence_retriever()
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
            if isinstance(result, CaptionFinal):
                # A finalized turn may be cue-worthy; consider it out of band.
                self._consider_cue(result)

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
