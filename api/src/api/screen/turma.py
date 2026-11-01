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

A pass looks at EVERY pending meeting: one org's stuck backlog (its 404s or 429s) must never
hide another org's meetings. After a retryable refusal the rest of that org waits for the
next pass; an unreachable hub ends the pass (every request would wait out the timeout).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import quote, urlsplit

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
SEGMENT_TEXT_MAX = 2000
PARTICIPANTS_MAX = 100
ERROR_MAX = 300
# Two hub limits, both 1 MiB (an hour of captions is ~60 KB): the request body (413) and the
# org's pending transcripts (429, MEETINGS_PENDING_BYTES_ORG_MAX), which the hub measures as
# JSON of its SANITIZED segments, where every line carries `"speaker":""`. A meeting over the
# second on its own would 429 forever, so lines are counted the way the hub counts them; the
# headroom covers its other fields. Past the budget the newest lines are left out and the
# conversation records how many were sent.
BYTES_MAX = 1024 * 1024
BYTES_HEADROOM = 16 * 1024


def _ms(dt) -> int:
    return int(dt.timestamp() * 1000)


def _encode(body: dict[str, object]) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def meeting_body(conv: Conversation) -> tuple[dict[str, object], int] | None:
    """The POST body for one finished meeting and how many caption lines it had in all
    (more than the body carries when the meeting is past the hub's limits); None when it has
    no caption text to send."""
    segments = []
    for seg in sorted(conv.segments, key=lambda s: (s.start_ms, s.segment_id)):
        text = seg.text.strip()
        if not text:
            continue
        start = max(0, seg.start_ms)
        item: dict[str, object] = {
            # Code points, as the hub counts them; it would cut there itself.
            "text": text[:SEGMENT_TEXT_MAX],
            "startMs": start,
            "endMs": max(start, seg.end_ms),
        }
        if seg.speaker:
            item["speaker"] = seg.speaker
        segments.append(item)
    if not segments:
        return None
    total = len(segments)
    started = _ms(conv.started_at)
    ended = max(started, _ms(conv.ended_at or conv.started_at))
    body: dict[str, object] = {
        "meetingId": conv.id,
        "startedAt": started,
        "endedAt": ended,
        "participants": conv.participants[:PARTICIPANTS_MAX],
        "segments": [],
    }
    if conv.meeting_title:
        body["title"] = conv.meeting_title[:TITLE_MAX]
    if conv.platform:
        body["platform"] = conv.platform[:PLATFORM_MAX]
    # Keep the earliest lines that fit both budgets (+1 per comma between them). A line's
    # hub-counted size is never below its wire size, so one count bounds both.
    room = min(BYTES_MAX - BYTES_HEADROOM - len(_encode(body)), BYTES_MAX - BYTES_HEADROOM)
    kept = []
    for item in segments[:SEGMENTS_MAX]:
        size = len(_encode({"speaker": "", **item})) + (1 if kept else 0)
        if size > room:
            break
        room -= size
        kept.append(item)
    body["segments"] = kept
    return body, total


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
        live = {cid for _, cid, _ in pending}
        for cid in list(self._backoff):
            if cid not in live:
                self._backoff.pop(cid, None)
        deferred: set[str] = set()  # orgs the hub just refused for now: next pass
        for household, cid, org in pending:
            _, due = self._backoff.get(cid, (0, 0.0))
            if due > self.clock() or org in deferred:
                continue  # checked before loading: a deferred org's backlog costs no reads
            outcome = await self._deliver(household, cid, deferred)
            if outcome == "sent":
                sent += 1
            elif outcome == "unreachable":
                break
        return sent

    async def _set(self, household: str, cid: str, state, error: str | None) -> None:
        await asyncio.to_thread(
            self.store.set_delivery,
            household,
            cid,
            state,
            error[:ERROR_MAX] if error else None,
        )

    async def _deliver(self, household: str, cid: str, deferred: set[str]) -> str:
        """One attempt: "sent", "failed", "retry" (the hub refused for now; the org is
        deferred), "unreachable" (no answer at all), or "skipped"."""
        conv = await asyncio.to_thread(self.store.get, household, cid)
        if conv is None or conv.source != "screen" or not conv.org_site_key:
            self._backoff.pop(cid, None)
            return "skipped"
        if conv.delivery_state not in (None, "pending"):
            return "skipped"
        built = meeting_body(conv)
        if built is None:
            await self._fail(household, cid, "no caption lines to send")
            return "failed"
        body, total = built
        if not body["segments"]:
            await self._fail(household, cid, "the first caption line is past the hub's limit")
            return "failed"
        endpoint = f"{self.url}/api/orgs/{quote(conv.org_site_key, safe='')}/meetings"
        outcome = "retry"
        try:
            resp = await self.client.post(
                endpoint,
                content=_encode(body),
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError as exc:
            reason = f"hub unreachable ({type(exc).__name__})"
            outcome = "unreachable"
        else:
            if resp.status_code == 200 and _ok(resp):
                sent = len(body["segments"])  # type: ignore[arg-type]
                note = None
                if sent < total:
                    note = (
                        f"sent the first {sent} of {total} lines: the rest is past the hub's limit"
                    )
                await self._set(household, cid, "sent", note)
                self._backoff.pop(cid, None)
                metrics.incr("turma.meetings_sent")
                log.info("meeting %s delivered to turma (%s)", cid, conv.org_site_key)
                return "sent"
            reason = _hub_error(resp)
            if resp.status_code in PERMANENT:
                await self._fail(household, cid, reason)
                return "failed"
            deferred.add(conv.org_site_key)
        ended = (conv.ended_at or conv.started_at).timestamp()
        if self.clock() - ended >= self.give_up_seconds:
            await self._fail(household, cid, f"gave up retrying: {reason}")
            return "failed"
        failures = self._backoff.get(cid, (0, 0.0))[0] + 1
        delay = min(self.retry_max_seconds, BASE_DELAY_SECONDS * 2 ** (failures - 1))
        self._backoff[cid] = (failures, self.clock() + delay)
        await self._set(household, cid, "pending", reason)
        metrics.incr("turma.meetings_retried")
        log.info("meeting %s not delivered yet (%s); retry in %.0fs", cid, reason, delay)
        return outcome

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


def half_configured() -> str | None:
    """The setting missing when only one of URL and token is set, else None."""
    url, token = settings.turma_url.strip(), settings.turma_meetings_token.strip()
    if bool(url) == bool(token):
        return None
    return "API_TURMA_MEETINGS_TOKEN" if url else "API_TURMA_URL"


def configured() -> bool:
    return bool(settings.turma_url.strip() and settings.turma_meetings_token.strip())


def get_delivery() -> TurmaDelivery | None:
    return _delivery


def start(store: ConversationStore | None) -> TurmaDelivery | None:
    """Build the process-wide delivery when configured (lifespan)."""
    global _delivery
    _delivery = None
    url, token = settings.turma_url.strip(), settings.turma_meetings_token.strip()
    if missing := half_configured():
        # Half set is a mistake, not a choice: say so, or meetings pile up pending silently.
        log.warning("meeting delivery to turma is off: %s is not set", missing)
        return None
    if store is None or not configured():
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        log.error("meeting delivery to turma is off: API_TURMA_URL must be an http(s) URL")
        return None
    _delivery = TurmaDelivery(
        store,
        url=url,
        token=token,
        retry_max_seconds=settings.turma_retry_max_seconds,
        give_up_seconds=settings.turma_give_up_seconds,
    )
    return _delivery


def wake() -> None:
    if _delivery is not None:
        _delivery.wake()
