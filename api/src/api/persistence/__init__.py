"""Persistence seam: conversation transcripts + full-audio retention.

Two process-wide singletons — the conversation store and the audio store — are
selected by ``API_PERSISTENCE_BACKEND`` / ``API_AUDIO_BACKEND`` and shared by the
live sessions and the history REST API so a recorded session is immediately
browsable in the UI. ``off`` disables retention; ``memory`` (default) is the
simulator/CI backend; ``postgres``/``disk`` are the production swaps behind the
same Protocols.
"""

from __future__ import annotations

from api.config import settings
from api.persistence.audio import (
    AudioStore,
    InMemoryAudioStore,
    LocalDiskAudioStore,
    audio_key,
)
from api.persistence.conversations import ConversationStore, InMemoryConversationStore
from api.persistence.models import (
    Conversation,
    ConversationStatus,
    Cue,
    Segment,
    coerce_status,
)
from api.persistence.wav import pcm16_to_wav, wav_to_pcm16

__all__ = [
    "AudioStore",
    "Conversation",
    "ConversationStatus",
    "ConversationStore",
    "Cue",
    "LocalDiskAudioStore",
    "Segment",
    "audio_key",
    "coerce_status",
    "get_audio_store",
    "get_conversation_store",
    "pcm16_to_wav",
    "wav_to_pcm16",
]


def _build_conversation_store() -> ConversationStore | None:
    backend = settings.persistence_backend
    if backend in ("off", "none", ""):
        return None
    if backend == "memory":
        return InMemoryConversationStore()
    if backend == "postgres":
        from api.persistence.postgres import SqlConversationStore

        return SqlConversationStore(settings.database_url)
    raise ValueError(
        f"unknown persistence backend: {backend!r} (expected 'off', 'memory' or 'postgres')"
    )


def _build_audio_store() -> AudioStore | None:
    backend = settings.audio_backend
    if backend in ("off", "none", ""):
        return None
    if backend == "memory":
        return InMemoryAudioStore()
    if backend == "disk":
        return LocalDiskAudioStore(settings.audio_dir)
    raise ValueError(f"unknown audio backend: {backend!r} (expected 'off', 'memory' or 'disk')")


_conversation_store = _build_conversation_store()
_audio_store = _build_audio_store()


def get_conversation_store() -> ConversationStore | None:
    """The shared conversation store, or None when persistence is disabled."""
    return _conversation_store


def get_audio_store() -> AudioStore | None:
    """The shared audio object store, or None when audio retention is disabled."""
    return _audio_store
