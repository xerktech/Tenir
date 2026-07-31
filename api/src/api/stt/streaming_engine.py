"""Streaming STT engine seam (XERK-115 hybrid, master plan §5.2 follow-up).

The `WhisperEngine` seam (`engine.py`) is request/response: one window in, one
transcript out. A *cache-aware streaming* model inverts that — it holds an open
connection, is fed audio as it arrives, and emits a transcript that only ever grows.
This narrow async seam models that: `StreamingTranscriber` drives partials through a
`StreamSession` (the low-latency Nemotron half of the hybrid) while still finalising
each turn on the offline `WhisperEngine` (the accurate Parakeet half).

Kept separate from `WhisperEngine` on purpose: partials and finals now come from
different models, and the streaming side is stateful (per-turn encoder cache) and
async (a live socket), neither of which the synchronous per-window engine models.

The concrete `NemotronWsEngine` is a WebSocket client (network I/O, excluded from
coverage like `ParakeetEngine`); the protocol it speaks is covered by the
`nemotron-stt` server tests, and `StreamingTranscriber`'s use of this seam is covered
against a fake session with no socket.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

log = logging.getLogger("api.stt.streaming_engine")

# Tenir contract Lang -> a Nemotron locale key. The model prompts on a locale (or
# "auto" to detect); anything we don't have a mapping for falls back to auto-detect,
# which the GPU benchmark confirmed works across the languages we care about.
_LOCALE = {"en": "en-US", "es": "es-ES"}


def to_locale(language: str | None) -> str:
    return _LOCALE.get((language or "").lower(), "auto")


class StreamSession(Protocol):
    async def feed(self, pcm: bytes) -> str | None:
        """Feed 16 kHz s16le mono PCM; return the running turn transcript if it
        advanced, else None. Never raises for an ordinary empty/duplicate update."""
        ...

    async def reset(self) -> None:
        """Clear the turn's encoder cache + running text (call at a turn boundary)."""
        ...

    async def aclose(self) -> None:
        """Close the underlying connection."""
        ...


class StreamingSttEngine(Protocol):
    async def open(self, *, language: str | None) -> StreamSession:
        """Open one streaming session, pinning the language for its lifetime."""
        ...


class NemotronWsEngine:  # pragma: no cover - requires websockets + a live server
    """Opens a WebSocket to the nemotron-stt server (`/v1/audio/stream`)."""

    def __init__(self, *, endpoint: str, timeout: float = 15.0) -> None:
        # endpoint is the server root, e.g. "ws://nemotron:8000"; normalise a stray
        # http(s):// to ws(s):// so the same compose var shape as the HTTP routes works.
        base = endpoint.rstrip("/")
        if base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        self._url = base + "/v1/audio/stream"
        self._timeout = timeout

    async def open(self, *, language: str | None) -> StreamSession:
        import websockets  # noqa: PLC0415

        ws = await websockets.connect(self._url, open_timeout=self._timeout)
        await ws.send(json.dumps({"target_lang": to_locale(language)}))
        return _NemotronWsSession(ws, timeout=self._timeout)


class _NemotronWsSession:  # pragma: no cover - requires a live server
    """One WebSocket, one turn's cache. Strict request/response: each PCM frame gets
    exactly one partial back, each reset exactly one ack."""

    def __init__(self, ws: object, *, timeout: float) -> None:
        self._ws = ws
        self._timeout = timeout

    async def feed(self, pcm: bytes) -> str | None:
        import asyncio  # noqa: PLC0415

        await self._ws.send(pcm)  # type: ignore[attr-defined]
        raw = await asyncio.wait_for(self._ws.recv(), self._timeout)  # type: ignore[attr-defined]
        msg = json.loads(raw)
        if msg.get("type") == "partial":
            return msg.get("text") or ""
        return None

    async def reset(self) -> None:
        import asyncio  # noqa: PLC0415

        await self._ws.send(json.dumps({"type": "reset"}))  # type: ignore[attr-defined]
        await asyncio.wait_for(self._ws.recv(), self._timeout)  # type: ignore[attr-defined]

    async def aclose(self) -> None:
        await self._ws.close()  # type: ignore[attr-defined]
