"""Conversation history & search REST API.

The management surface over the retained conversation store: browse history,
search transcripts, read a conversation back with its segments, download the
retained audio, and export or delete a conversation (and its audio) per record.
Everything is scoped to the caller's household.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from api.auth import Principal, current_principal
from api.auth.deps import principal_from_request
from api.persistence import (
    Conversation,
    ConversationStatus,
    Segment,
    coerce_status,
    get_audio_store,
    get_conversation_store,
)

router = APIRouter(tags=["history"])


class SegmentOut(BaseModel):
    segmentId: str
    text: str
    startMs: int
    endMs: int
    lang: str | None = None

    @classmethod
    def of(cls, seg: Segment) -> "SegmentOut":
        return cls(
            segmentId=seg.segment_id,
            text=seg.text,
            startMs=seg.start_ms,
            endMs=seg.end_ms,
            lang=seg.lang,
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

    @classmethod
    def of(cls, conv: Conversation) -> "ConversationSummaryOut":
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
            segmentCount=len(conv.segments),
            hasAudio=conv.audio_key is not None,
        )


class ConversationOut(ConversationSummaryOut):
    """Detail view: the summary projection plus the transcript."""

    segments: list[SegmentOut] = Field(default_factory=list)

    @classmethod
    def of(cls, conv: Conversation) -> "ConversationOut":
        base = ConversationSummaryOut.of(conv).model_dump()
        return cls(**base, segments=[SegmentOut.of(s) for s in conv.segments])


def _store():
    store = get_conversation_store()
    if store is None:
        raise HTTPException(status_code=409, detail="persistence is disabled")
    return store


def _require(household: str, conversation_id: str) -> Conversation:
    conv = _store().get(household, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@router.get("/conversations", response_model=list[ConversationSummaryOut])
def list_conversations(
    q: str | None = Query(None, description="Keyword search over conversation transcripts."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    principal: Principal = Depends(current_principal),
) -> list[ConversationSummaryOut]:
    store = _store()
    hh = principal.household
    convs = (
        store.search(hh, q, limit=limit, offset=offset)
        if q
        else store.list(hh, limit=limit, offset=offset)
    )
    return [ConversationSummaryOut.of(c) for c in convs]


@router.get("/conversations/{conversation_id}", response_model=ConversationOut)
def get_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> ConversationOut:
    return ConversationOut.of(_require(principal.household, conversation_id))


@router.get("/conversations/{conversation_id}/export", response_model=ConversationOut)
def export_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> ConversationOut:
    # Per-record export for the privacy controls; same shape as the detail view.
    return ConversationOut.of(_require(principal.household, conversation_id))


@router.get("/conversations/{conversation_id}/audio")
def get_conversation_audio(
    conversation_id: str, principal: Principal = Depends(principal_from_request)
) -> Response:
    conv = _require(principal.household, conversation_id)
    store = get_audio_store()
    data = store.get(conv.audio_key) if (store is not None and conv.audio_key) else None
    if data is None:
        raise HTTPException(status_code=404, detail="no audio retained for this conversation")
    return Response(
        content=data,
        media_type="audio/wav",
        headers={"Content-Disposition": f'attachment; filename="{conversation_id}.wav"'},
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str, principal: Principal = Depends(current_principal)
) -> None:
    # Per-record delete: drop the transcript *and* the retained audio.
    hh = principal.household
    conv = _require(hh, conversation_id)
    audio = get_audio_store()
    if audio is not None and conv.audio_key:
        audio.delete(conv.audio_key)
    _store().delete(hh, conversation_id)
