"""``/ws/screen``: the meeting-caption ingest socket (XERK-2505, contract ScreenFrameHeader).

Auth is a screen device token (POST /auth/device-token) bound to the operator's user; login
and OIDC tokens are refused. Each binary frame is ``>I`` header length + JSON header + JPEG.
Per window and region only the newest frame waits for OCR: a frame that arrives while the
previous one is still being read replaces any frame queued behind it (latest-frame-wins, the
sizing in docs/meeting-captions.md). Frames are only ever held in memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, WebSocket
from pydantic import ValidationError

from api.auth import AuthError, Principal, get_user_store, principal_from_bearer
from api.auth.deps import SCREEN_SCOPE
from api.config import settings
from api.contract import ErrorMessage, Pong, ScreenCadence, ScreenFrameHeader
from api.metrics import metrics
from api.persistence import ScreenMeta, get_conversation_store
from api.screen.meetings import (
    CPU_POOL,
    AccountGone,
    Cadence,
    MeetingTracker,
    meetings,
    now_ms,
)
from api.screen.ocr import HttpOcrClient, OcrClient, OcrError
from api.screen.zoom import parse_caption_crop, parse_full_frame

router = APIRouter()
log = logging.getLogger("api.screen")

# A capture timestamp further than this from the server clock is replaced by the server's.
_MAX_CLOCK_SKEW_MS = 60_000
_HEADER_LEN = struct.Struct(">I")
_MAX_HEADER = 4096

_ocr: OcrClient | None = None
# Windows (orgSiteKeys) one socket may feed: the capture PC watches four. Bounds the lanes,
# and the decoded frames they hold, that one device token can open.
MAX_WINDOWS = 8
# Open screen sockets per user: each entry stops its socket (closes 1008). Deleting a user
# calls revoke_user(), since auth is only checked at the handshake (XERK-236 parity).
_revokers: dict[str, set[Callable[[], None]]] = {}


def revoke_user(user_id: str) -> int:
    """Close every open screen socket of a deleted user; returns how many."""
    stops = list(_revokers.get(user_id, ()))
    for stop in stops:
        stop()
    metrics.incr("screen.revoked", len(stops))
    return len(stops)


def get_ocr_client() -> OcrClient | None:
    """The process-wide tenir-ocr client, or None when API_OCR_URL is unset."""
    global _ocr
    if _ocr is None and settings.ocr_url.strip():
        _ocr = HttpOcrClient(settings.ocr_url.strip(), timeout=settings.ocr_timeout_seconds)
    return _ocr


def cadence() -> Cadence:
    return Cadence(
        idle_fps=settings.screen_idle_fps,
        meeting_fps=settings.screen_meeting_fps,
        full_frame_seconds=settings.screen_full_frame_seconds,
    )


def _token(ws: WebSocket) -> str | None:
    auth = ws.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    return token or ws.query_params.get("token")


def _screen_principal(ws: WebSocket) -> tuple[Principal | None, str]:
    token = _token(ws)
    if not token:
        return None, "missing token"
    try:
        return principal_from_bearer(token, scope=SCREEN_SCOPE), ""
    except AuthError as exc:
        return None, str(exc)


def _account_exists(user_id: str) -> bool:
    return get_user_store().get_by_id(user_id) is not None


class FrameError(ValueError):
    pass


def split_frame(data: bytes) -> tuple[ScreenFrameHeader, bytes]:
    """Split a binary frame into its validated header and the JPEG; raises FrameError."""
    if len(data) < _HEADER_LEN.size:
        raise FrameError("frame shorter than its header length")
    (n,) = _HEADER_LEN.unpack_from(data)
    if n == 0 or n > _MAX_HEADER or _HEADER_LEN.size + n >= len(data):
        raise FrameError("bad header length")
    try:
        header = ScreenFrameHeader.model_validate_json(
            data[_HEADER_LEN.size : _HEADER_LEN.size + n]
        )
    except ValidationError as exc:
        raise FrameError(f"bad frame header: {exc.errors()[0]['msg']}") from exc
    jpeg = data[_HEADER_LEN.size + n :]
    if not jpeg.startswith(b"\xff\xd8"):
        raise FrameError("frame image is not a JPEG")
    return header, jpeg


class _Lane:
    """Processes one window region's frames one at a time, newest first."""

    def __init__(self, handle: Callable[[ScreenFrameHeader, bytes], Awaitable[None]]) -> None:
        self._handle = handle
        self._pending: tuple[ScreenFrameHeader, bytes] | None = None
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._run())

    def put(self, header: ScreenFrameHeader, jpeg: bytes) -> None:
        if self._pending is not None:
            metrics.incr("screen.frames_dropped")
        self._pending = (header, jpeg)
        self._wake.set()

    async def _run(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            item, self._pending = self._pending, None
            if item is not None:
                await self._handle(*item)


@router.websocket("/ws/screen")
async def screen_ws(ws: WebSocket) -> None:
    principal, reason = await asyncio.to_thread(_screen_principal, ws)
    await ws.accept()
    if principal is None:
        log.warning("screen ws rejected: %s", reason)
        metrics.incr("screen.unauthorized")
        await ws.close(code=1008, reason="invalid or expired device token")
        return
    store = get_conversation_store()
    ocr = get_ocr_client()
    if store is None or ocr is None:
        missing = "persistence" if store is None else "ocr"
        log.warning("screen ws refused: %s not configured", missing)
        await ws.close(code=1013, reason=f"{missing} not configured")
        return

    send_lock = asyncio.Lock()
    closing = asyncio.Event()

    async def send(msg: ScreenCadence | Pong | ErrorMessage) -> None:
        async with send_lock:
            await ws.send_text(msg.model_dump_json(exclude_none=True))

    async def error(message: str) -> None:
        await send(ErrorMessage(type="error", code="bad_request", message=message))

    def tracker_for(header: ScreenFrameHeader) -> MeetingTracker:
        key = (principal.household, principal.user_id, header.orgSiteKey)
        tracker = meetings.get(key)
        if tracker is None:
            tracker = meetings.put(
                key,
                MeetingTracker(
                    principal.household,
                    principal.user_id,
                    ScreenMeta(header.orgSiteKey, header.orgName, header.platform.value),
                    store=store,
                    account_exists=_account_exists,
                    cadence=cadence(),
                    stable_ms=int(settings.screen_line_stable_seconds * 1000),
                    end_ms=int(settings.screen_meeting_end_seconds * 1000),
                ),
            )
        return tracker

    async def handle(header: ScreenFrameHeader, jpeg: bytes) -> None:
        try:
            await read_frame(header, jpeg)
        finally:
            metrics.incr("screen.frames_handled")

    async def read_frame(header: ScreenFrameHeader, jpeg: bytes) -> None:
        if closing.is_set():
            return
        loop = asyncio.get_running_loop()
        server_now = now_ms()
        ts = header.ts if abs(header.ts - server_now) <= _MAX_CLOCK_SKEW_MS else server_now
        try:
            result = await ocr.read(jpeg)
        except OcrError as exc:
            log.warning("screen OCR failed: %s", exc)
            metrics.incr("screen.ocr_errors")
            return
        if closing.is_set():
            return  # the account was deleted while OCR ran
        reattached = (tracker := tracker_for(header)).notify is not send
        tracker.notify = send
        try:
            if reattached and tracker.conversation_id is not None:
                # A reconnect mid-meeting: this socket got the idle default on connect,
                # so tell it the window is in a meeting.
                await send(tracker.cadence.message(meeting=True, org_site_key=header.orgSiteKey))
            async with tracker.lock:
                ts = tracker.clock(ts)
                if header.region.value == "full":
                    frame = await loop.run_in_executor(CPU_POOL, parse_full_frame, result, jpeg)
                    await tracker.on_full(frame, ts)
                else:
                    roster = await tracker.ensure_roster()
                    blocks = await loop.run_in_executor(
                        CPU_POOL, lambda: parse_caption_crop(result, roster, active=tracker.active)
                    )
                    await tracker.on_caption(blocks, ts)
        except AccountGone:
            log.warning("screen ws closed: account no longer exists")
            metrics.incr("screen.account_removed")
            closing.set()
        except Exception:
            # A store blip must not drop the socket: the window keeps streaming and the
            # next frame retries (a failed meeting start is retried by the next caption).
            log.exception("screen frame failed (%s)", header.orgSiteKey)
            metrics.incr("screen.frame_errors")

    lanes: dict[tuple[str, str], _Lane] = {}
    _revokers.setdefault(principal.user_id, set()).add(closing.set)
    # A delete between the handshake's auth and this registration never saw this socket:
    # re-check now that a delete would (the mic socket does the same, XERK-1504).
    if not await asyncio.to_thread(_account_exists, principal.user_id):
        closing.set()
    await send(cadence().message(meeting=False))
    receive: asyncio.Task | None = None
    closed_by_us = asyncio.create_task(closing.wait())
    try:
        while True:
            receive = asyncio.create_task(ws.receive())
            done, _ = await asyncio.wait(
                {receive, closed_by_us}, return_when=asyncio.FIRST_COMPLETED
            )
            if closed_by_us in done:
                await ws.close(code=1008, reason="account removed")
                break
            frame = receive.result()
            if frame["type"] == "websocket.disconnect":
                break
            if (data := frame.get("bytes")) is not None:
                if len(data) > settings.screen_max_frame_bytes:
                    await error("frame too large")
                    continue
                try:
                    header, jpeg = split_frame(data)
                except FrameError as exc:
                    await error(str(exc))
                    continue
                metrics.incr("screen.frames")
                lane_key = (header.orgSiteKey, header.region.value)
                if (
                    lane_key not in lanes
                    and len({k[0] for k in lanes} | {header.orgSiteKey}) > MAX_WINDOWS
                ):
                    await error(f"more than {MAX_WINDOWS} windows on one socket")
                    continue
                if lane_key not in lanes:
                    lanes[lane_key] = _Lane(handle)
                lanes[lane_key].put(header, jpeg)
                continue
            try:
                msg = json.loads(frame.get("text") or "")
            except ValueError:
                msg = None
            if isinstance(msg, dict) and msg.get("type") == "ping":
                t = msg.get("t")
                await send(Pong(type="pong", t=t if isinstance(t, int) else None))
            else:
                await error("expected a binary frame or ping")
    finally:
        stops = _revokers.get(principal.user_id)
        if stops is not None:
            stops.discard(closing.set)
            if not stops:
                _revokers.pop(principal.user_id, None)
        for task in (receive, closed_by_us, *(lane.task for lane in lanes.values())):
            if task is not None and not task.done():
                task.cancel()
        meetings.detach(send)
