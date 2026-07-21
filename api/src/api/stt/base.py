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
