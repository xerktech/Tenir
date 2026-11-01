"""Conversation history & search REST API.

The management surface over the retained conversation store: browse history,
search transcripts, read a conversation back with its segments, download the
retained audio, and export or delete a conversation (and its audio) per record.
Everything is scoped to the caller's household.
"""

from __future__ import annotations

import dataclasses
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from api.auth import Principal, current_principal
from api.auth.deps import principal_from_request
from api.session import is_valid_session_id
from api.persistence import (
    Conversation,
    ConversationStatus,
    ConversationSummary,
    Cue,
    Segment,
    Song,
    alias_key,
    audio_key,
    coerce_status,
    get_audio_store,
    get_conversation_store,
)

router = APIRouter(tags=["history"])
log = logging.getLogger("api.history")


class SegmentOut(BaseModel):
    segmentId: str
    text: str
    startMs: int
    endMs: int
    lang: str | None = None
    # English translation of a non-English turn (XERK-160); clients render it
    # alongside the original.
    translation: str | None = None
    # Who said it, on a screen-captured meeting (XERK-2505); None otherwise.
    speaker: str | None = None

    @classmethod
    def of(cls, seg: Segment) -> "SegmentOut":
        return cls(
            segmentId=seg.segment_id,
            text=seg.text,
            startMs=seg.start_ms,
            endMs=seg.end_ms,
            lang=seg.lang,
            translation=seg.translation,
            speaker=seg.speaker,
        )


class CueOut(BaseModel):
    """A private context cue, rendered inline in history at atMs (XERK-81)."""

    cueId: str
    title: str
    body: str
    atMs: int
    # Live-source attribution (XERK-120); None for a cue from model knowledge.
    source: str | None = None

    @classmethod
    def of(cls, cue: Cue) -> "CueOut":
        return cls(
            cueId=cue.cue_id, title=cue.title, body=cue.body, atMs=cue.at_ms, source=cue.source
        )


class SongOut(BaseModel):
    """A song recognized playing, rendered inline in history at atMs (XERK-184)."""

    songId: str
    title: str
    artist: str
    atMs: int
    durationMs: int | None = None

    @classmethod
    def of(cls, song: Song) -> "SongOut":
        return cls(
            songId=song.song_id,
            title=song.title,
            artist=song.artist,
            atMs=song.at_ms,
            durationMs=song.duration_ms,
        )


class ConversationSummaryOut(BaseModel):
    """List-view projection: metadata without the full segment list."""

    id: str
    status: ConversationStatus
    micSource: str | None = None
    sourceLang: str | None = None
    startedAt: str
    endedAt: str | None = None
    durationMs: int
    segmentCount: int
    hasAudio: bool
    # "mic" for a live audio session, "screen" for a meeting read off captured captions
    # (XERK-2505); the org/platform/title fields are set on screen meetings only.
    source: str = "mic"
    orgSiteKey: str | None = None
    orgName: str | None = None
    platform: str | None = None
    meetingTitle: str | None = None

    @classmethod
    def of(cls, conv: ConversationSummary) -> "ConversationSummaryOut":
        return cls(
            id=conv.id,
            # A status from an older schema would otherwise fail response validation
            # and 500 the whole listing — one legacy row must not hide every
            # conversation (XERK-58).
            status=coerce_status(conv.status, ended=conv.ended_at is not None),
            micSource=conv.mic_source,
            sourceLang=conv.source_lang,
            startedAt=conv.started_at.isoformat(),
            endedAt=conv.ended_at.isoformat() if conv.ended_at else None,
            durationMs=conv.duration_ms,
            segmentCount=conv.segment_count,
            hasAudio=conv.audio_key is not None,
            source=conv.source,
            orgSiteKey=conv.org_site_key,
            orgName=conv.org_name,
            platform=conv.platform,
            meetingTitle=conv.meeting_title,
        )


class ConversationOut(ConversationSummaryOut):
    """Detail view: the summary projection plus the transcript, its cues, and the
    songs recognized during it."""

    segments: list[SegmentOut] = Field(default_factory=list)
    cues: list[CueOut] = Field(default_factory=list)
    songs: list[SongOut] = Field(default_factory=list)
    # Participant tile names of a screen meeting, first seen first (XERK-2505).
    participants: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, conv: Conversation, aliases: dict[str, str] | None = None) -> "ConversationOut":
        """``aliases`` (``alias_key -> name``, the org's speaker aliases) relabels speakers
        renamed since the meeting was stored (XERK-2507)."""
        base = ConversationSummaryOut.of(conv.summary()).model_dump()

        def shown(name: str) -> str:
            return aliases.get(alias_key(name), name) if aliases else name

        segments = [SegmentOut.of(s) for s in conv.segments]
        for seg in segments:
            if seg.speaker is not None:
                seg.speaker = shown(seg.speaker)
        return cls(
            **base,
            segments=segments,
            cues=[CueOut.of(c) for c in conv.cues],
            songs=[SongOut.of(s) for s in conv.songs],
            # Two tiles renamed to one name show once.
            participants=list(dict.fromkeys(shown(p) for p in conv.participants)),
        )


class OrgOut(BaseModel):
    """An org the caller has screen-captured meetings from: the history org filter."""

    siteKey: str
    name: str


# A display name, not free text: long enough for "Firstname Lastname (Team)".
_MAX_SPEAKER_NAME = 80


class SpeakerRenameIn(BaseModel):
    """Show ``speaker`` (as the meeting currently displays it) as ``name`` in every meeting
    from the same org, past and future (XERK-2507)."""

    speaker: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=_MAX_SPEAKER_NAME)


def _store():
    store = get_conversation_store()
    if store is None:
        raise HTTPException(status_code=409, detail="persistence is disabled")
    return store


def _owner_scope(principal: Principal) -> str | None:
    """The owner filter a read runs under (XERK-651).

    An admin sees every recording in their household, so they read with no owner
    filter (``None``). A member is scoped to their own recordings by their stable
    local ``user_id``. A NULL-owner (legacy) row never equals a member id, so it is
    admin-only until backfilled (docs/auth-oidc.md §9).
    """
    return None if principal.is_admin else principal.user_id


def _require(household: str, conversation_id: str, *, owner: str | None = None) -> Conversation:
    # A conversation id is always a server-issued uuid4. Anything else cannot
    # match a row, and handing it to the store is how a NUL byte in the path
    # (`/conversations/<uuid>%00`) reached psycopg and came back as a 500 with a
    # traceback instead of a 404 (XERK-236). Reject the shape, don't query it.
    if not is_valid_session_id(conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    # Owner scope is applied in the store read, so a recording the caller doesn't own
    # comes back as None and 404s here — the same "not found" a member gets for an id
    # that doesn't exist, so ownership never leaks another user's ids (XERK-651).
    conv = _store().get(household, conversation_id, owner=owner)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


def _with_stored_audio(conv: Conversation) -> Conversation:
    """Link a finished recording's WAV that its row never recorded.

    Rows the stale sweep closed before it linked audio (PR #227), or whose link
    failed during an outage, are 'ready' with no audio_key while the WAV sits at
    its deterministic key: History hid the player and the audio route 404'd
    (XERK-1591). Heal on read; a failed write still serves the audio."""
    audio = get_audio_store()
    if conv.audio_key is not None or conv.status == "live" or audio is None:
        return conv
    try:
        key = audio_key(conv.household, conv.id)
    except ValueError:
        return conv  # not a key-safe id, so no WAV can have been stored under it
    try:
        stored = audio.exists(key)
    except OSError as exc:
        # A broken audio mount must not 500 the transcript: read it as "no audio".
        log.warning("could not check stored audio for conversation %s: %s", conv.id, exc)
        return conv
    if not stored:
        return conv
    try:
        _store().set_audio_key(conv.household, conv.id, key)
    except Exception as exc:  # noqa: BLE001 - the next read retries the link
        log.warning("could not link stored audio for conversation %s: %s", conv.id, exc)
    return dataclasses.replace(conv, audio_key=key)


def _detail(conv: Conversation) -> ConversationOut:
    """The detail view, with the org's current speaker names on a screen meeting."""
    aliases = (
        _store().speaker_aliases(conv.household, conv.org_site_key)
        if conv.org_site_key is not None
        else None
    )
    return ConversationOut.of(conv, aliases)


@router.get("/conversations", response_model=list[ConversationSummaryOut])
def list_conversations(
    q: str | None = Query(None, description="Keyword search over conversation transcripts."),
    org: str | None = Query(
        None,
        max_length=200,
        description="Only screen-captured meetings from this org site key (XERK-2507).",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(current_principal),
) -> list[ConversationSummaryOut]:
    store = _store()
    hh = principal.household
    owner = _owner_scope(principal)
    convs = (
        store.search(hh, q, owner=owner, org=org, limit=limit, offset=offset)
        if q
        else store.list(hh, owner=owner, org=org, limit=limit, offset=offset)
    )
    return [ConversationSummaryOut.of(c) for c in convs]


# Declared before /conversations/{conversation_id} so "orgs" is not read as an id.
@router.get("/conversations/orgs", response_model=list[OrgOut])
def list_conversation_orgs(principal: Principal = Depends(current_principal)) -> list[OrgOut]:
    # Owner-scoped like the listing: a member's filter offers only orgs they recorded.
    return [
        OrgOut(siteKey=key, name=name)
        for key, name in _store().orgs(principal.household, owner=_owner_scope(principal))
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> ConversationOut:
    return _detail(
        _with_stored_audio(
            _require(principal.household, conversation_id, owner=_owner_scope(principal))
        )
    )


@router.get("/conversations/{conversation_id}/export", response_model=ConversationOut)
def export_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> ConversationOut:
    # Per-record export for the privacy controls; same shape as the detail view.
    return _detail(
        _with_stored_audio(
            _require(principal.household, conversation_id, owner=_owner_scope(principal))
        )
    )


@router.put("/conversations/{conversation_id}/speakers", response_model=ConversationOut)
def rename_speaker(
    conversation_id: str,
    body: SpeakerRenameIn,
    principal: Principal = Depends(current_principal),
) -> ConversationOut:
    """Rename a speaker of a screen meeting (e.g. ``sam@example.com`` -> ``Sam``).

    Aliases are per org, so the new name shows in every meeting from that org, and the
    capture pipeline applies it to meetings recorded from now on. The caller must be able
    to read this meeting, and the speaker must appear in it: that keeps a member from
    writing aliases for an org they never recorded."""
    owner = _owner_scope(principal)
    conv = _require(principal.household, conversation_id, owner=owner)
    if conv.org_site_key is None:
        raise HTTPException(status_code=409, detail="only meeting speakers can be renamed")
    shown = _detail(conv)
    speakers = set(shown.participants) | {s.speaker for s in shown.segments if s.speaker}
    if body.speaker not in speakers:
        raise HTTPException(status_code=404, detail="no such speaker in this meeting")
    name = " ".join(body.name.split())
    if not name:
        raise HTTPException(status_code=422, detail="name must not be blank")
    _store().rename_speaker(conv.household, conv.org_site_key, body.speaker, name)
    return _detail(_with_stored_audio(_require(principal.household, conversation_id, owner=owner)))


def _parse_byte_range(range_header: str, size: int) -> tuple[int, int] | None:
    """Parse a single ``Range: bytes=start-end`` against a body of ``size`` bytes.

    Returns an inclusive ``(start, end)`` span, or ``None`` when the header isn't a
    single satisfiable byte range — a malformed, multi-range or out-of-bounds ask,
    for which we fall back to a full ``200`` (RFC 7233 lets a server ignore Range).
    Handles the open-ended (``bytes=500-``) and suffix (``bytes=-500``) forms.
    """
    if not range_header.startswith("bytes="):
        return None
    spec = range_header[len("bytes=") :]
    if "," in spec:  # multiple ranges — not worth it for one small clip
        return None
    start_s, sep, end_s = spec.partition("-")
    if not sep:
        return None
    try:
        if start_s == "":
            # Suffix range: the final N bytes of the body.
            suffix = int(end_s)
            if suffix <= 0:
                return None
            start, end = max(0, size - suffix), size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


def _audio_response(data: bytes, filename: str, range_header: str | None) -> Response:
    """Serve retained audio, honouring a single HTTP Range request.

    A native media seek bar (the web ``<audio>`` element, Android ``MediaPlayer``)
    scrubs by asking for byte ranges; without a ``206`` the player can't seek within
    the clip. We always advertise ``Accept-Ranges: bytes`` and switch to ``206
    Partial Content`` when the client asks for a satisfiable range. ``inline`` (not
    ``attachment``) so navigating the URL plays it rather than forcing a download.
    """
    size = len(data)
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
    }
    span = _parse_byte_range(range_header, size) if range_header else None
    if span is None:
        return Response(content=data, media_type="audio/wav", headers=headers)
    start, end = span
    return Response(
        content=data[start : end + 1],
        status_code=206,
        media_type="audio/wav",
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"},
    )


@router.get("/conversations/{conversation_id}/audio")
def get_conversation_audio(
    conversation_id: str,
    principal: Principal = Depends(principal_from_request),
    range_header: str | None = Header(default=None, alias="Range"),
) -> Response:
    # Owner-scoped: a member cannot fetch another user's audio even with a direct
    # link (the token can ride in ?token=, but ownership still gates the row).
    conv = _with_stored_audio(
        _require(principal.household, conversation_id, owner=_owner_scope(principal))
    )
    store = get_audio_store()
    data = store.get(conv.audio_key) if (store is not None and conv.audio_key) else None
    if data is None:
        raise HTTPException(status_code=404, detail="no audio retained for this conversation")
    return _audio_response(data, f"{conversation_id}.wav", range_header)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> None:
    # Per-record delete: drop the transcript *and* the retained audio. Owner-scoped,
    # so a member can only delete their own recording (another user's 404s); an admin
    # can delete any in the household.
    hh = principal.household
    conv = _require(hh, conversation_id, owner=_owner_scope(principal))
    audio = get_audio_store()
    if audio is not None:
        # Also the deterministic key: a session can store its WAV yet never record
        # the key (it ended during a database outage), and that audio must not
        # outlive the "deleted" recording.
        keys = {conv.audio_key}
        try:
            keys.add(audio_key(hh, conversation_id))
        except ValueError:
            pass  # not a key-safe id, so no WAV can have been stored under it
        for key in keys - {None}:
            audio.delete(key)
    _store().delete(hh, conversation_id)
    if audio is not None:
        # Again now the row is gone: a live session may have stored its WAV and
        # linked it in between. One storing after this finds no row to link and
        # drops its own WAV (XERK-1608). Audio first above, so a failed delete
        # leaves the row for the user to retry.
        for key in keys - {None}:
            audio.delete(key)
