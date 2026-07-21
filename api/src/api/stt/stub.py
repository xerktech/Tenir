"""Phase 0 stub transcriber.

Emits placeholder captions driven by how much audio has arrived, so the full
phone -> WSS -> api -> lens caption loop can be exercised in the simulator
before a real model is wired in (Phase 1). It does NOT transcribe anything.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from api.contract import CaptionFinal, CaptionPartial

# 16 kHz * 2 bytes/sample = 32000 bytes/sec. Finalize roughly every ~2s of audio.
_BYTES_PER_SEC = 16000 * 2
_FINAL_EVERY = _BYTES_PER_SEC * 2


class StubTranscriber:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[CaptionPartial | CaptionFinal] = asyncio.Queue()
        self._bytes_since_final = 0
        self._total_bytes = 0
        self._closed = False

    async def push(self, pcm: bytes) -> None:
        self._total_bytes += len(pcm)
        self._bytes_since_final += len(pcm)
        secs = self._total_bytes / _BYTES_PER_SEC
        await self._queue.put(
            CaptionPartial(type="caption.partial", text=f"[listening… {secs:0.1f}s]", lang="en")
        )
        if self._bytes_since_final >= _FINAL_EVERY:
            await self._emit_final()

    async def _emit_final(self) -> None:
        start = (self._total_bytes - self._bytes_since_final) * 1000 // _BYTES_PER_SEC
        end = self._total_bytes * 1000 // _BYTES_PER_SEC
        self._bytes_since_final = 0
        await self._queue.put(
            CaptionFinal(
                type="caption.final",
                segmentId=str(uuid.uuid4()),
                text="[stub segment — real STT lands in Phase 1]",
                lang="en",
                startMs=start,
                endMs=end,
            )
        )

    async def results(self) -> AsyncIterator[CaptionPartial | CaptionFinal]:
        while not self._closed:
            yield await self._queue.get()

    async def flush(self) -> None:
        if self._bytes_since_final > 0:
            await self._emit_final()

    async def close(self) -> None:
        self._closed = True
        # Unblock a pending results() get.
        await self._queue.put(CaptionPartial(type="caption.partial", text="", lang="en"))
