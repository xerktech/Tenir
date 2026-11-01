"""Deliver finished screen meetings to the Turma hub (XERK-2513).

A screen meeting is created with ``delivery_state='pending'``. Once it is ``ready`` (the
tracker ended it, a shutdown finished it, or the boot stale sweep closed a crashed one) this
loop POSTs it to ``{API_TURMA_URL}/api/orgs/<orgSiteKey>/meetings`` with the hub's
``TURMA_MEETINGS_TOKEN`` bearer. The wire contract is Turma's
``.claude/rules/contract-meetings.md``.

Exactly once at the hub = at-least-once here + the hub's idempotency on ``meetingId`` (the
conversation id): a repeat answers 200 ``duplicate: true`` and changes nothing. So a request
lost mid-flight (a hub restart) is simply sent again; the state lives in the database, so a
Tenir restart resumes too.

- 200 with ``ok`` → ``sent``.
- A refusal that can't change by waiting (400 malformed, 401/403 token, 413 too big) →
  ``failed`` with the hub's words.
- Anything else (unreachable, 5xx, 404 "no host is in that org" while the hub's hosts
  re-register after a restart, 409 store full, 429 queue full) → retried with exponential
  backoff; still ``pending`` with the last reason, until ``API_TURMA_GIVE_UP_SECONDS`` after
  the meeting ended, then ``failed``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import httpx

from api.config import settings
from api.metrics import metrics
from api.persistence import Conversation, ConversationStore

log = logging.getLogger("api.screen.turma")

# Refusals no retry can fix; everything else is retried.
PERMANENT = frozenset({400, 401, 403, 413})
BASE_DELAY_SECONDS = 5.0
POLL_SECONDS = 10.0
REQUEST_TIMEOUT_SECONDS = 30.0
# The hub's caps (contract-meetings.md): over these it refuses (title/platform) or 413s
# (segments). Segment and participant text over its caps is cut by the hub itself.
TITLE_MAX = 300
PLATFORM_MAX = 40
SEGMENTS_MAX = 10_000
PARTICIPANTS_MAX = 100
ERROR_MAX = 300


def _ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def meeting_body(conv: Conversation) -> dict[str, object] | None:
    """The POST body for one finished meeting; None when it has no caption text to send."""
    segments = []
    for seg in sorted(conv.segments, key=lambda s: (s.start_ms, s.segment_id)):
        if not seg.text.strip():
            continue
        start = max(0, seg.start_ms)
        item: dict[str, object] = {
            "text": seg.text,
            "startMs": start,
            "endMs": max(start, seg.end_ms),
        }
        if seg.speaker:
            item["speaker"] = seg.speaker
        segments.append(item)
    if not segments:
        return None
    started = _ms(conv.started_at)
    ended = max(started, _ms(conv.ended_at or conv.started_at))
    body: dict[str, object] = {
        "meetingId": conv.id,
        "startedAt": started,
        "endedAt": ended,
        "participants": conv.participants[:PARTICIPANTS_MAX],
        "segments": segments[:SEGMENTS_MAX],
    }
    if conv.meeting_title:
        body["title"] = conv.meeting_title[:TITLE_MAX]
    if conv.platform:
        body["platform"] = conv.platform[:PLATFORM_MAX]
    return body


def _hub_error(resp: httpx.Response) -> str:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    words = payload.get("error") if isinstance(payload, dict) else None
    reason = f"hub answered {resp.status_code}"
    if isinstance(words, str) and words:
        reason += f": {words}"
    return reason


class TurmaDelivery:
    def __init__(
        self,
        store: ConversationStore,
        *,
        url: str,
        token: str,
        client: httpx.AsyncClient | None = None,
        retry_max_seconds: float = 300.0,
        give_up_seconds: float = 86_400.0,
        clock=time.time,
    ) -> None:
        self.store = store
        self.url = url.rstrip("/")
        self.token = token
        self.client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS)
        self.retry_max_seconds = retry_max_seconds
        self.give_up_seconds = give_up_seconds
        self.clock = clock
        # conversation id -> (failed attempts, earliest next attempt). In memory only: a
        # restarted process retries everything pending at once, which the hub's
        # idempotency makes safe.
        self._backoff: dict[str, tuple[int, float]] = {}
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """A meeting just turned ready: try it now rather than on the next poll."""
        self._wake.set()

    async def run_once(self) -> int:
        """Try every pending meeting that is due; returns how many were sent."""
        pending = await asyncio.to_thread(self.store.pending_deliveries)
        sent = 0
        live = {cid for _, cid in pending}
        for cid in list(self._backoff):
            if cid not in live:
                self._backoff.pop(cid, None)
        for household, cid in pending:
            _, due = self._backoff.get(cid, (0, 0.0))
            if due > self.clock():
                continue
            if await self._deliver(household, cid):
                sent += 1
        return sent

    async def _set(self, household: str, cid: str, state, error: str | None) -> None:
        await asyncio.to_thread(
            self.store.set_delivery,
            household,
            cid,
            state,
            error[:ERROR_MAX] if error else None,
        )

    async def _deliver(self, household: str, cid: str) -> bool:
        conv = await asyncio.to_thread(self.store.get, household, cid)
        if conv is None or conv.delivery_state != "pending" or not conv.org_site_key:
            self._backoff.pop(cid, None)
            return False
        body = meeting_body(conv)
        if body is None:
            await self._set(household, cid, "failed", "no caption lines to send")
            metrics.incr("turma.meetings_failed")
            return False
        endpoint = f"{self.url}/api/orgs/{quote(conv.org_site_key, safe='')}/meetings"
        try:
            resp = await self.client.post(
                endpoint, json=body, headers={"Authorization": f"Bearer {self.token}"}
            )
        except httpx.HTTPError as exc:
            reason = f"hub unreachable ({type(exc).__name__})"
        else:
            if resp.status_code == 200 and _ok(resp):
                await self._set(household, cid, "sent", None)
                self._backoff.pop(cid, None)
                metrics.incr("turma.meetings_sent")
                log.info("meeting %s delivered to turma (%s)", cid, conv.org_site_key)
                return True
            reason = _hub_error(resp)
            if resp.status_code in PERMANENT:
                await self._fail(household, cid, reason)
                return False
        ended = (conv.ended_at or conv.started_at).timestamp()
        if self.clock() - ended >= self.give_up_seconds:
            await self._fail(household, cid, f"gave up retrying: {reason}")
            return False
        failures = self._backoff.get(cid, (0, 0.0))[0] + 1
        delay = min(self.retry_max_seconds, BASE_DELAY_SECONDS * 2 ** (failures - 1))
        self._backoff[cid] = (failures, self.clock() + delay)
        await self._set(household, cid, "pending", reason)
        metrics.incr("turma.meetings_retried")
        log.info("meeting %s not delivered yet (%s); retry in %.0fs", cid, reason, delay)
        return False

    async def _fail(self, household: str, cid: str, reason: str) -> None:
        self._backoff.pop(cid, None)
        await self._set(household, cid, "failed", reason)
        metrics.incr("turma.meetings_failed")
        log.warning("meeting %s not delivered to turma: %s", cid, reason)

    async def loop(self, interval: float = POLL_SECONDS) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.run_once()
            except Exception:  # a database outage: the next pass retries
                log.exception("turma meeting delivery pass failed")

    async def aclose(self) -> None:
        await self.client.aclose()


def _ok(resp: httpx.Response) -> bool:
    """A 200 counts only as the hub's own ``{ok: true}``: a proxy's or login page's 200
    would otherwise mark a meeting sent that never arrived."""
    try:
        payload = resp.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


_delivery: TurmaDelivery | None = None


def configured() -> bool:
    return bool(settings.turma_url.strip() and settings.turma_meetings_token.strip())


def get_delivery() -> TurmaDelivery | None:
    return _delivery


def start(store: ConversationStore | None) -> TurmaDelivery | None:
    """Build the process-wide delivery when configured (lifespan)."""
    global _delivery
    if store is None or not configured():
        _delivery = None
        return None
    _delivery = TurmaDelivery(
        store,
        url=settings.turma_url.strip(),
        token=settings.turma_meetings_token.strip(),
        retry_max_seconds=settings.turma_retry_max_seconds,
        give_up_seconds=settings.turma_give_up_seconds,
    )
    return _delivery


def wake() -> None:
    if _delivery is not None:
        _delivery.wake()
