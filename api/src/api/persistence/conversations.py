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
    utcnow,
)


class ConversationStore(Protocol):
    def create(
        self,
        household: str,
        conversation_id: str,
        *,
        mic_source: str | None = None,
        source_lang: str | None = None,
    ) -> Conversation: ...
    def add_segment(self, household: str, conversation_id: str, segment: Segment) -> None: ...
    def add_cue(self, household: str, conversation_id: str, cue: Cue) -> None: ...
    def finish(
        self,
        household: str,
        conversation_id: str,
        *,
        status: ConversationStatus = "ready",
    ) -> Conversation | None: ...
    def set_audio_key(self, household: str, conversation_id: str, audio_key: str) -> None: ...
    def clear_audio_key(self, household: str, conversation_id: str) -> None: ...
    def get(self, household: str, conversation_id: str) -> Conversation | None: ...
    def list(self, household: str, *, limit: int = 50, offset: int = 0) -> list[Conversation]: ...
    def search(
        self, household: str, query: str, *, limit: int = 50, offset: int = 0
    ) -> list[Conversation]: ...
    def delete(self, household: str, conversation_id: str) -> bool: ...
    def households(self) -> list[str]: ...


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
        mic_source: str | None = None,
        source_lang: str | None = None,
    ) -> Conversation:
        with self._lock:
            convs = self._conversations(household)
            # Idempotent so a resumed session (same id) keeps its existing record.
            existing = convs.get(conversation_id)
            if existing is not None:
                return existing
            conv = Conversation(
                id=conversation_id,
                household=household,
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

    def get(self, household: str, conversation_id: str) -> Conversation | None:
        with self._lock:
            return self._conversations(household).get(conversation_id)

    def list(self, household: str, *, limit: int = 50, offset: int = 0) -> list[Conversation]:
        with self._lock:
            convs = sorted(
                self._conversations(household).values(),
                key=lambda c: c.started_at,
                reverse=True,
            )
            return convs[offset : offset + limit]

    def search(
        self, household: str, query: str, *, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return self.list(household, limit=limit, offset=offset)
        with self._lock:
            scored: list[tuple[int, Conversation]] = []
            for conv in self._conversations(household).values():
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
