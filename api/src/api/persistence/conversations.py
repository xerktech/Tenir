"""Conversation transcript store.

Persists conversations + their transcript segments and answers the history /
search queries the web/mobile clients are built on. The in-memory implementation
behind the ``ConversationStore`` Protocol is the CI/simulator default; the
Postgres backend (``postgres.py``) swaps in behind the same seam, keyed
identically by household then conversation id so live sessions and the history
API share state.

Search is a case-insensitive keyword scan over each conversation's transcript,
ranked by match count then recency — the in-memory stand-in for the Postgres FTS
the real backend uses. Keeps the API contract identical across backends.
"""

from __future__ import annotations

import threading
from typing import Protocol

from api.persistence.models import (
    Conversation,
    ConversationStatus,
    Cue,
    Segment,
    Song,
    utcnow,
)


class ConversationStore(Protocol):
    def create(
        self,
        household: str,
        conversation_id: str,
        *,
        owner: str | None = None,
        mic_source: str | None = None,
        source_lang: str | None = None,
    ) -> Conversation: ...
    def add_segment(self, household: str, conversation_id: str, segment: Segment) -> None: ...
    def set_segment_translation(
        self, household: str, conversation_id: str, segment_id: str, translation: str
    ) -> None: ...
    def add_cue(self, household: str, conversation_id: str, cue: Cue) -> None: ...
    def add_song(self, household: str, conversation_id: str, song: Song) -> None: ...
    def finish(
        self,
        household: str,
        conversation_id: str,
        *,
        status: ConversationStatus = "ready",
    ) -> Conversation | None: ...
    def set_audio_key(self, household: str, conversation_id: str, audio_key: str) -> None: ...
    def clear_audio_key(self, household: str, conversation_id: str) -> None: ...
    def get(
        self, household: str, conversation_id: str, *, owner: str | None = None
    ) -> Conversation | None: ...
    def list(
        self, household: str, *, owner: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Conversation]: ...
    def search(
        self,
        household: str,
        query: str,
        *,
        owner: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]: ...
    def delete(self, household: str, conversation_id: str) -> bool: ...
    def households(self) -> list[str]: ...
    def finish_stale(self) -> int: ...


class InMemoryConversationStore:
    """Thread-safe in-memory ``ConversationStore`` (default backend).

    Thread-safe because the session writes from the event loop while the REST
    history API reads from worker threads, both sharing one process-wide instance.
    """

    def __init__(self) -> None:
        self._by_household: dict[str, dict[str, Conversation]] = {}
        self._lock = threading.Lock()

    def _conversations(self, household: str) -> dict[str, Conversation]:
        return self._by_household.setdefault(household, {})

    def create(
        self,
        household: str,
        conversation_id: str,
        *,
        owner: str | None = None,
        mic_source: str | None = None,
        source_lang: str | None = None,
    ) -> Conversation:
        with self._lock:
            convs = self._conversations(household)
            # Idempotent so a resumed session (same id) keeps its existing record —
            # including its original owner; a resume never re-owns a recording.
            existing = convs.get(conversation_id)
            if existing is not None:
                return existing
            conv = Conversation(
                id=conversation_id,
                household=household,
                owner=owner,
                mic_source=mic_source,
                source_lang=source_lang,
            )
            convs[conversation_id] = conv
            return conv

    def add_segment(self, household: str, conversation_id: str, segment: Segment) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return
            # Upsert by segment id so a re-emitted final replaces rather than dupes.
            for i, existing in enumerate(conv.segments):
                if existing.segment_id == segment.segment_id:
                    conv.segments[i] = segment
                    return
            conv.segments.append(segment)

    def set_segment_translation(
        self, household: str, conversation_id: str, segment_id: str, translation: str
    ) -> None:
        """Attach a (later-arriving) translation to an already-persisted turn
        (XERK-160). The translation lands after the final was stored — the model
        call runs off the caption path — so it is an update, not part of add."""
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return
            for seg in conv.segments:
                if seg.segment_id == segment_id:
                    seg.translation = translation
                    return

    def add_cue(self, household: str, conversation_id: str, cue: Cue) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return
            # Upsert by cue id so a re-delivered cue replaces rather than dupes.
            for i, existing in enumerate(conv.cues):
                if existing.cue_id == cue.cue_id:
                    conv.cues[i] = cue
                    return
            conv.cues.append(cue)

    def add_song(self, household: str, conversation_id: str, song: Song) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return
            # Upsert by song id so a re-delivered song replaces rather than dupes.
            for i, existing in enumerate(conv.songs):
                if existing.song_id == song.song_id:
                    conv.songs[i] = song
                    return
            conv.songs.append(song)

    def finish(
        self,
        household: str,
        conversation_id: str,
        *,
        status: ConversationStatus = "ready",
    ) -> Conversation | None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return None
            conv.ended_at = utcnow()
            conv.status = status
            return conv

    def set_audio_key(self, household: str, conversation_id: str, audio_key: str) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is not None:
                conv.audio_key = audio_key

    def clear_audio_key(self, household: str, conversation_id: str) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is not None:
                conv.audio_key = None

    @staticmethod
    def _owned(conv: Conversation, owner: str | None) -> bool:
        """Whether ``owner`` may read ``conv`` (XERK-651).

        ``owner is None`` is the admin/internal scope — no filter, every row visible.
        Otherwise only the caller's own rows match; a NULL-owner (legacy/auth-off) row
        never equals a member's id, so it stays admin-only until backfilled (§9).
        """
        return owner is None or conv.owner == owner

    def get(
        self, household: str, conversation_id: str, *, owner: str | None = None
    ) -> Conversation | None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None or not self._owned(conv, owner):
                return None
            return conv

    def list(
        self, household: str, *, owner: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        with self._lock:
            convs = sorted(
                (c for c in self._conversations(household).values() if self._owned(c, owner)),
                key=lambda c: c.started_at,
                reverse=True,
            )
            return convs[offset : offset + limit]

    def search(
        self,
        household: str,
        query: str,
        *,
        owner: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return self.list(household, owner=owner, limit=limit, offset=offset)
        with self._lock:
            scored: list[tuple[int, Conversation]] = []
            for conv in self._conversations(household).values():
                if not self._owned(conv, owner):
                    continue
                hay = conv.transcript.lower()
                score = sum(hay.count(term) for term in terms)
                if score:
                    scored.append((score, conv))
        scored.sort(key=lambda sc: (sc[0], sc[1].started_at), reverse=True)
        return [conv for _, conv in scored[offset : offset + limit]]

    def delete(self, household: str, conversation_id: str) -> bool:
        with self._lock:
            return self._conversations(household).pop(conversation_id, None) is not None

    def households(self) -> list[str]:
        """Every household with at least one conversation (readiness probe)."""
        with self._lock:
            return list(self._by_household.keys())

    def finish_stale(self) -> int:
        """Close out conversations left ``live`` by a previous process.

        A graceful shutdown finalizes every registered session, but an OOM kill,
        a host reboot or a stop that overruns the grace period does not — and
        nothing ever revisited those rows, so each one stayed ``live`` forever:
        permanently "recording" in every client's history, never exportable, its
        audio gone with the process (XERK-236). Called once at startup, before
        any new session can register. In-memory rows never survive a restart, so
        this is a no-op here and real work only in the SQL store.
        """
        with self._lock:
            stale = [
                conv
                for convs in self._by_household.values()
                for conv in convs.values()
                if conv.status == "live"
            ]
            for conv in stale:
                conv.status = "ready"
                conv.ended_at = conv.ended_at or utcnow()
            return len(stale)
