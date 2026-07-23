"""Per-connection session.

Holds the live session identity and the STT seam, fans audio in, and pumps
caption results back out. Every finalized turn is persisted to the conversation
store as it lands, the full audio is retained in memory for the session and
flushed to the audio store on end — a recorded, stored STT session.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from api.config import settings
from api.contract import (
    CaptionFinal,
    CaptionPartial,
    Lang,
    MicSource,
    ServerMessage,
    SessionReady,
)
from api.metrics import metrics
from api.persistence import (
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
        await self._send(
            SessionReady(type="session.ready", sessionId=self.session_id, resumed=self.resumed)
        )
        log.info("session %s ready (mic=%s)", self.session_id, mic_source)

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
                log.warning(
                    "session %s could not deliver a caption (client gone)", self.session_id
                )
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

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._grace_task is not None:
            self._grace_task.cancel()
            self._grace_task = None
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
