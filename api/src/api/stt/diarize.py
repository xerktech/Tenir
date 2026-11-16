"""Client for the tenir-diarizer service (XERK-2958, ``diarizer/README.md``).

The streaming transcriber posts each finalized turn's PCM here before decoding it and
gets back the turn cut into per-speaker pieces. A piece two people spoke over each
other carries that speaker's separated audio; every other piece is a span of the turn's
own audio. Any failure returns ``None`` and the turn is captioned whole and unlabelled.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass

import httpx

from api.metrics import metrics

log = logging.getLogger("api.stt.diarize")

# After a failure the diarizer is skipped for this long (turns caption unlabelled at once).
# Every turn waits on the diarizer before it decodes, so a hung or blackholed service that
# is retried per turn adds its whole timeout to each caption; QA measured finals 27 s late
# and most of them dropped as stale within 20 turns.
_COOLDOWN_S = 30.0


@dataclass(frozen=True)
class Piece:
    speaker: int  # 1-based, stable for the conversation
    start_ms: int  # relative to the turn start
    end_ms: int
    overlap: bool
    audio: bytes | None  # separated s16le PCM for an overlapped piece, else None

    @property
    def label(self) -> str:
        return f"Speaker {self.speaker}"


class Diarizer:
    """One conversation's view of the diarizer. ``conversation_id`` keys the service's
    speaker cache, so a resumed conversation keeps its numbering while the service is up."""

    def __init__(
        self, url: str, conversation_id: str, *, timeout: float = 3.0, api_key: str = ""
    ) -> None:
        self._url = url.rstrip("/") + "/v1/diarize"
        self._conversation = conversation_id
        self._timeout = timeout
        api_key = api_key.strip()  # a trailing newline from `echo` into a secret
        if not api_key.isascii():
            # Not sendable in a header: refuse it here rather than crash the session.
            log.error("API_DIARIZER_KEY is not ASCII; the diarizer will refuse every turn")
            api_key = ""
        # httpx's per-phase read timeout also hangs up on a slow reply, so the service
        # sees the disconnect and drops the turn rather than finishing it for nobody.
        self._client = httpx.Client(
            timeout=timeout,
            headers={"authorization": f"Bearer {api_key}"} if api_key else None,
        )
        # One token per sitting (this transcriber's life): the service tells a resumed
        # sitting whose timeline restarted behind its own from a late resend.
        self._epoch = uuid.uuid4().hex
        self._skip_until = 0.0

    async def diarize(self, pcm: bytes, offset_ms: int) -> list[Piece] | None:
        """Pieces for one turn starting at ``offset_ms`` on the conversation timeline, or
        None when the diarizer is unavailable (the caller then captions the turn whole)."""
        if time.monotonic() < self._skip_until:
            metrics.incr("stage.diarize.skipped")
            return None
        try:
            # Whole-request deadline (httpx's timeout is per phase), as ParakeetEngine._post.
            return await asyncio.wait_for(
                asyncio.to_thread(self._post, pcm, offset_ms), self._timeout
            )
        except Exception as exc:
            self._skip_until = time.monotonic() + _COOLDOWN_S
            why = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                why = "401: API_DIARIZER_KEY does not match the diarizer's DIAR_API_KEY"
            log.warning(
                "diarizer unavailable (%s); captioning unlabelled for %.0f s", why, _COOLDOWN_S
            )
            log.debug("diarizer failure", exc_info=True)
            metrics.incr("stage.diarize.errors")
            return None

    def _post(self, pcm: bytes, offset_ms: int) -> list[Piece]:
        r = self._client.post(
            self._url,
            params={"session": self._conversation, "offset_ms": offset_ms, "epoch": self._epoch},
            content=pcm,
            headers={"content-type": "application/octet-stream"},
        )
        r.raise_for_status()
        return parse_pieces(r.json(), turn_ms=len(pcm) // 32)

    def close(self) -> None:
        self._client.close()


def parse_pieces(body: dict, *, turn_ms: int) -> list[Piece]:
    """Validate the service's answer; anything malformed raises (→ unlabelled turn)."""
    pieces = []
    for p in body["pieces"]:
        speaker = int(p["speaker"])
        start, end = int(p["startMs"]), int(p["endMs"])
        if speaker < 1 or not 0 <= start < end or start > turn_ms:
            raise ValueError(f"bad diarizer piece: {p!r}")
        audio = base64.b64decode(p["audio"], validate=True) if p.get("audio") else None
        if audio is not None and len(audio) % 2:
            audio = audio[:-1]
        pieces.append(Piece(speaker, start, min(end, turn_ms), bool(p.get("overlap")), audio))
    return pieces
