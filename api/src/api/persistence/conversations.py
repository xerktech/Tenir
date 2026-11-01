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

import copy
import threading
from datetime import datetime
from typing import Protocol

from api.persistence.models import (
    Conversation,
    ConversationStatus,
    ConversationSummary,
    Cue,
    ScreenMeta,
    Segment,
    Song,
    alias_key,
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
        screen: ScreenMeta | None = None,
    ) -> Conversation:
        """Create (or reopen) the row. ``screen`` makes it a screen-captured meeting
        (``source='screen'`` plus its org and platform, XERK-2505)."""
        ...
    def reopen(self, household: str, conversation_id: str) -> Conversation | None:
        """Mark an existing recording live again for a cold resume; None when it is gone.

        Never inserts: a recording deleted after the resume's existence check must stay
        deleted, which the create() upsert would undo."""
        ...
    def add_segment(self, household: str, conversation_id: str, segment: Segment) -> None: ...
    def set_segment_translation(
        self, household: str, conversation_id: str, segment_id: str, translation: str
    ) -> None: ...
    def add_cue(self, household: str, conversation_id: str, cue: Cue) -> None: ...
    def set_meeting_title(self, household: str, conversation_id: str, title: str) -> None: ...
    def add_participants(
        self, household: str, conversation_id: str, names: list[tuple[str, int]]
    ) -> None:
        """Record ``(name, first_seen_ms)`` tile names; a name already recorded is kept."""
        ...
    def speaker_aliases(self, household: str, org_site_key: str) -> dict[str, str]:
        """``alias_key(alias) -> display name`` for one org's speakers."""
        ...
    def set_speaker_alias(self, household: str, org_site_key: str, alias: str, name: str) -> None: ...
    def rename_speaker(self, household: str, org_site_key: str, current: str, name: str) -> None:
        """Show ``current`` (a displayed speaker name) as ``name`` from now on (XERK-2507).

        Every alias already displayed as ``current`` (compared by ``alias_key``, as lookups
        are) is re-pointed too, so a raw tile name renamed twice still resolves in one hop."""
        ...
    def orgs(self, household: str, *, owner: str | None = None) -> list[tuple[str, str]]:
        """``(org_site_key, org_name)`` of every org the caller has a screen meeting from,
        by name; the name is the most recent meeting's."""
        ...
    def add_song(self, household: str, conversation_id: str, song: Song) -> None: ...
    def finish(
        self,
        household: str,
        conversation_id: str,
        *,
        status: ConversationStatus = "ready",
        opened_at: datetime | None = None,
    ) -> Conversation | None:
        """Close the row. With ``opened_at`` (the caller's create()/reopen().opened_at), only
        while no later sitting has reopened it since."""
        ...
    def set_audio_key(self, household: str, conversation_id: str, audio_key: str) -> bool:
        """Point the row at its WAV; returns False when there is no such row."""
        ...
    def clear_audio_key(self, household: str, conversation_id: str) -> None: ...
    def get(
        self, household: str, conversation_id: str, *, owner: str | None = None
    ) -> Conversation | None: ...
    def list(
        self,
        household: str,
        *,
        owner: str | None = None,
        org: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Conversation]:
        """Newest first; ``org`` keeps only screen meetings from that org site key."""
        ...
    def search(
        self,
        household: str,
        query: str,
        *,
        owner: str | None = None,
        org: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationSummary]: ...
    def delete(self, household: str, conversation_id: str) -> bool: ...
    def households(self) -> list[str]: ...
    def ready(self) -> None: ...
    def finish_stale(self) -> list[tuple[str, str]]: ...


class InMemoryConversationStore:
    """Thread-safe in-memory ``ConversationStore`` (default backend).

    Thread-safe because the session writes from the event loop while the REST
    history API reads from worker threads, both sharing one process-wide instance.
    """

    def __init__(self) -> None:
        self._by_household: dict[str, dict[str, Conversation]] = {}
        self._aliases: dict[tuple[str, str], dict[str, str]] = {}
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
        screen: ScreenMeta | None = None,
    ) -> Conversation:
        with self._lock:
            convs = self._conversations(household)
            # Idempotent so a resumed session (same id) keeps its existing record —
            # including its original owner; a resume never re-owns a recording.
            # A cold resume of a finished one reopens it: it is being recorded
            # again, so it reads live until this sitting finishes it (XERK-1502).
            existing = convs.get(conversation_id)
            if existing is not None:
                existing.status = "live"
                existing.ended_at = None
                existing.opened_at = utcnow()
                # A copy, as the SQL store returns: the caller's opened_at is its
                # token and must not move when a later sitting reopens the row.
                return copy.deepcopy(existing)
            conv = Conversation(
                id=conversation_id,
                household=household,
                owner=owner,
                mic_source=mic_source,
                source_lang=source_lang,
            )
            if screen is not None:
                conv.source = "screen"
                conv.org_site_key = screen.org_site_key
                conv.org_name = screen.org_name
                conv.platform = screen.platform
            conv.opened_at = conv.started_at
            convs[conversation_id] = conv
            return copy.deepcopy(conv)

    def reopen(self, household: str, conversation_id: str) -> Conversation | None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return None
            conv.status = "live"
            conv.ended_at = None
            # A new sitting, as in create(): its token for finish(), returned as a copy.
            conv.opened_at = utcnow()
            return copy.deepcopy(conv)

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

    def set_meeting_title(self, household: str, conversation_id: str, title: str) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is not None:
                conv.meeting_title = title

    def add_participants(
        self, household: str, conversation_id: str, names: list[tuple[str, int]]
    ) -> None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return
            for name, _first_seen_ms in names:
                if name not in conv.participants:
                    conv.participants.append(name)

    def speaker_aliases(self, household: str, org_site_key: str) -> dict[str, str]:
        with self._lock:
            return dict(self._aliases.get((household, org_site_key), {}))

    def set_speaker_alias(self, household: str, org_site_key: str, alias: str, name: str) -> None:
        with self._lock:
            self._aliases.setdefault((household, org_site_key), {})[alias_key(alias)] = name

    def rename_speaker(self, household: str, org_site_key: str, current: str, name: str) -> None:
        with self._lock:
            aliases = self._aliases.setdefault((household, org_site_key), {})
            for key, shown in aliases.items():
                if alias_key(shown) == alias_key(current):
                    aliases[key] = name
            aliases[alias_key(current)] = name

    def orgs(self, household: str, *, owner: str | None = None) -> list[tuple[str, str]]:
        with self._lock:
            latest: dict[str, Conversation] = {}
            for conv in self._conversations(household).values():
                if conv.org_site_key is None or not self._owned(conv, owner):
                    continue
                seen = latest.get(conv.org_site_key)
                if seen is None or conv.started_at > seen.started_at:
                    latest[conv.org_site_key] = conv
        pairs = [(key, conv.org_name or key) for key, conv in latest.items()]
        return sorted(pairs, key=lambda p: (p[1].casefold(), p[0]))

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
        opened_at: datetime | None = None,
    ) -> Conversation | None:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return None
            if opened_at is not None and conv.opened_at != opened_at:
                return copy.deepcopy(conv)  # a later sitting reopened it; that one finishes it
            conv.ended_at = utcnow()
            conv.status = status
            return copy.deepcopy(conv)

    def set_audio_key(self, household: str, conversation_id: str, audio_key: str) -> bool:
        with self._lock:
            conv = self._conversations(household).get(conversation_id)
            if conv is None:
                return False
            conv.audio_key = audio_key
            return True

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
            # A copy, as the SQL store returns: a caller changing what it read must
            # not change the stored row behind the store's back.
            return copy.deepcopy(conv)

    @staticmethod
    def _in_org(conv: Conversation, org: str | None) -> bool:
        return org is None or conv.org_site_key == org

    def list(
        self,
        household: str,
        *,
        owner: str | None = None,
        org: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationSummary]:
        with self._lock:
            convs = sorted(
                (
                    c
                    for c in self._conversations(household).values()
                    if self._owned(c, owner) and self._in_org(c, org)
                ),
                key=lambda c: c.started_at,
                reverse=True,
            )
            return [c.summary() for c in convs[offset : offset + limit]]

    def search(
        self,
        household: str,
        query: str,
        *,
        owner: str | None = None,
        org: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ConversationSummary]:
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return self.list(household, owner=owner, org=org, limit=limit, offset=offset)
        with self._lock:
            scored: list[tuple[int, Conversation]] = []
            for conv in self._conversations(household).values():
                if not self._owned(conv, owner) or not self._in_org(conv, org):
                    continue
                hay = conv.transcript.lower()
                score = sum(hay.count(term) for term in terms)
                if score:
                    scored.append((score, conv))
        scored.sort(key=lambda sc: (sc[0], sc[1].started_at), reverse=True)
        return [conv.summary() for _, conv in scored[offset : offset + limit]]

    def delete(self, household: str, conversation_id: str) -> bool:
        with self._lock:
            return self._conversations(household).pop(conversation_id, None) is not None

    def ready(self) -> None:
        """Readiness probe: an in-memory store is always reachable."""

    def households(self) -> list[str]:
        """Every household with at least one conversation."""
        with self._lock:
            return list(self._by_household.keys())

    def finish_stale(self) -> list[tuple[str, str]]:
        """Close out conversations left ``live`` by a previous process; returns the
        ``(household, id)`` of each row it closed.

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
                (household, conv)
                for household, convs in self._by_household.items()
                for conv in convs.values()
                if conv.status == "live"
            ]
            for _, conv in stale:
                conv.status = "ready"
                conv.ended_at = conv.ended_at or utcnow()
            return [(household, conv.id) for household, conv in stale]
