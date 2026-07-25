"""Transcriber interface.

Every STT backend is swappable behind this seam (master plan §5.2). The api
feeds it raw 16 kHz s16le mono PCM and receives partial/final caption messages to
push to the client. Keeping this narrow lets Phase 1 drop in faster-whisper without
touching the session or WS layers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from api.contract import CaptionFinal, CaptionPartial


class Transcriber(Protocol):
    async def warmup(self) -> None:
        """Pay any per-session startup cost ahead of the first audio (XERK-128).

        Called once at session start, off the caption hot path, so a backend that
        has a cold-start cost (e.g. the hybrid path opening its streaming socket and
        the server building a per-connection decoder) pays it before the first spoken
        words rather than stalling the first caption behind it. Best-effort and
        optional: a backend with no startup cost makes this a no-op, and a failed
        warmup must degrade to the lazy path, never raise.
        """
        ...

    async def push(self, pcm: bytes) -> None:
        """Feed a chunk of 16 kHz s16le mono PCM."""
        ...

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        """Yield caption messages as they become available."""
        ...

    async def flush(self) -> None:
        """Finalize any in-flight audio (called on session end)."""
        ...

    async def close(self) -> None:
        """Release resources."""
        ...
