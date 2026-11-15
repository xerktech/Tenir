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

A ``failed`` meeting whose cause an operator can fix in config (a 401/403 from a wrong token,
or giving up during a long hub outage) goes back to ``pending`` when the loop starts
(XERK-2605): fixing the token needs a restart anyway, and the hub's idempotency makes the
resend safe. Every meeting pending at boot (re-queued or not) gets a full
``API_TURMA_GIVE_UP_SECONDS`` from that boot, so a hub still re-registering its hosts after a
cluster restart is retried, not given up on at the first 404 — also when the pod bounces
twice. A still-wrong token just fails them again, once per boot. Refusals no fix can change
(400, 413, nothing to send) stay failed.

Transcript back-fill (XERK-2965): a meeting the hub read before it kept full transcripts
(Turma XERK-2941) has none there. At the loop's start and then every
``BACKFILL_SECONDS``, each org Tenir delivered to is asked which of its settled meetings
have no transcript (``GET .../meetings/missing-transcripts``), and each one still held here
is POSTed again with ``backfill: true``: the hub saves it as the transcript, changes nothing
else, and never creates a meeting from it. An older hub answers that GET with a refusal, and
the org is skipped until the next pass.

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
from api.screen import topic
from api.screen.utterances import sentences

log = logging.getLogger("api.screen.turma")

# Refusals no retry can fix; everything else is retried.
PERMANENT = frozenset({400, 401, 403, 413})
# The start of a ``failed`` reason a config fix can change: re-queued on boot (XERK-2605).
# They must match what ``_hub_error`` and ``_deliver`` write.
GIVE_UP_PREFIX = "gave up retrying: "
REQUEUE_ON_BOOT = (
    "hub answered 401",
    "hub answered 403",
    GIVE_UP_PREFIX,
)
BASE_DELAY_SECONDS = 5.0
POLL_SECONDS = 10.0
# How often the transcript back-fill asks the hub again. Daily: a transcript the hub's byte
# budget evicted is listed again, so this bounds re-sending it to once a day.
BACKFILL_SECONDS = 86_400.0
# A pass the hub could not finish (unreachable, a refusal, an older hub) is asked again this
# soon instead: a Tenir deployed before the hub would otherwise wait a whole day.
BACKFILL_RETRY_SECONDS = 900.0
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
    # Sentences, not wrapped caption rows (XERK-2696); already sorted and non-blank.
    for seg in sentences(conv.segments, SEGMENT_TEXT_MAX):
        text = seg.text
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


# Whole-call cap on titling a meeting by topic (topic.py) before its first delivery.
TITLE_SECONDS = 20.0


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
        titler=None,
    ) -> None:
        self.store = store
        # Titles a meeting by its topic before its first delivery attempt (topic.py).
        self.titler = titler or topic.title_for
        self._titled: set[str] = set()  # asked once per process: a failure doesn't block
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
        # When this process's loop started (set by ``requeue_failed``): no meeting gives up
        # sooner than a full window after it. Not per meeting: a second restart must not lose
        # the window a re-queued meeting got on the first (XERK-2605 QA).
        self._booted_at: float | None = None
        # When the next transcript back-fill pass is due (0: at the loop's first pass).
        self._backfill_at = 0.0  # when the next pass (daily or retry) is due
        self._daily_at = 0.0  # when the next FULL pass is due
        # Orgs the last pass could not finish for a reason that may clear by waiting; a retry
        # pass asks only these, so one stuck org never re-runs every other org's pass.
        self._retry_orgs: set[str] = set()

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
        if conv.meeting_title is None and cid not in self._titled:
            self._titled.add(cid)
            try:
                # Bounded as a whole: httpx's timeout is per read, and this runs inside the
                # delivery loop, so a slow model would hold every other meeting back (QA).
                title = await asyncio.wait_for(self.titler(conv), TITLE_SECONDS)
            except Exception as exc:  # noqa: BLE001 — a title must never block delivery
                log.warning("meeting %s: no topic title (%r)", cid, exc)
                title = None
            if title:
                await asyncio.to_thread(self.store.set_meeting_title, household, cid, title)
            # Re-read: the meeting may have been deleted (or delivered) while it was titled,
            # and a deleted meeting must not reach Turma (QA).
            conv = await asyncio.to_thread(self.store.get, household, cid)
            if conv is None or conv.delivery_state not in (None, "pending"):
                self._titled.discard(cid)
                self._backoff.pop(cid, None)
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
                headers=self._auth(),
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
                log.info("meeting %s delivered to turma (%r)", cid, conv.org_site_key)
                self._titled.discard(cid)
                return "sent"
            reason = _hub_error(resp)
            if resp.status_code in PERMANENT:
                await self._fail(household, cid, reason)
                return "failed"
            deferred.add(conv.org_site_key)
        ended = (conv.ended_at or conv.started_at).timestamp()
        window_start = max(ended, self._booted_at or ended)
        if self.clock() - window_start >= self.give_up_seconds:
            await self._fail(household, cid, f"{GIVE_UP_PREFIX}{reason}")
            return "failed"
        failures = self._backoff.get(cid, (0, 0.0))[0] + 1
        delay = min(self.retry_max_seconds, BASE_DELAY_SECONDS * 2 ** (failures - 1))
        self._backoff[cid] = (failures, self.clock() + delay)
        await self._set(household, cid, "pending", reason)
        metrics.incr("turma.meetings_retried")
        log.info("meeting %s not delivered yet (%s); retry in %.0fs", cid, reason, delay)
        return outcome

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    async def backfill_transcripts(self, only: set[str] | None = None) -> int:
        """Send again every delivered meeting whose transcript the hub says it has not kept
        (of the orgs in ``only``, when given); returns how many the hub saved. Orgs left
        unfinished for a reason that may clear by waiting land in ``_retry_orgs``. An
        unreachable hub ends the pass, leaving every org not yet done to retry."""
        rows = await asyncio.to_thread(self.store.delivered_meetings)
        by_org: dict[str, dict[str, str]] = {}  # org -> {conversation id: household}
        for household, cid, org in rows:
            if only is None or org in only:
                by_org.setdefault(org, {})[cid] = household
        saved = 0
        retry: set[str] = set()
        self._retry_orgs = retry
        pending = list(by_org)
        for org, held in by_org.items():
            pending.remove(org)
            base = f"{self.url}/api/orgs/{quote(org, safe='')}/meetings"
            try:
                resp = await self.client.get(f"{base}/missing-transcripts", headers=self._auth())
            except httpx.HTTPError as exc:
                log.info("transcript back-fill: hub unreachable (%s)", type(exc).__name__)
                retry.update([org, *pending])
                return saved
            ids = _missing_ids(resp)
            if ids is None:
                log.info("transcript back-fill skipped an org: %s", _hub_error(resp))
                # A 401/403 here is an older hub (no such route) while the hub and Tenir roll
                # out in either order: worth asking again soon. A 404 (an old label, an org no
                # host is in) or a 400 holds until the daily pass.
                if _transient(resp) or resp.status_code in (401, 403):
                    retry.add(org)
                continue
            for cid in ids:
                household = held.get(cid)
                if household is None:
                    continue  # not ours, or deleted here: nothing to send
                conv = await asyncio.to_thread(self.store.get, household, cid)
                built = meeting_body(conv) if conv is not None else None
                if built is None or not built[0]["segments"]:
                    continue
                body = {**built[0], "backfill": True}
                try:
                    resp = await self.client.post(base, content=_encode(body), headers=self._auth())
                except httpx.HTTPError as exc:
                    log.info("transcript back-fill: hub unreachable (%s)", type(exc).__name__)
                    retry.update([org, *pending])
                    return saved
                if resp.status_code == 200 and _ok(resp):
                    answer = resp.json()
                    if answer.get("transcriptSaved") is True:
                        saved += 1
                        metrics.incr("turma.transcripts_backfilled")
                    elif answer.get("transcriptUnknown") is True:
                        retry.add(org)  # the hub's store could not tell: ask again soon
                else:
                    log.info("transcript of meeting %s not back-filled: %s", cid, _hub_error(resp))
                    if _transient(resp):
                        retry.add(org)
        if saved:
            log.info("back-filled %d meeting transcript(s) on the turma hub", saved)
        return saved

    async def _maybe_backfill(self) -> None:
        now = self.clock()
        if now < self._backfill_at:
            return
        full = now >= self._daily_at
        if full:
            self._daily_at = now + BACKFILL_SECONDS
        only = None if full else set(self._retry_orgs)
        try:
            await self.backfill_transcripts(only)
        except Exception:  # a database outage: the whole pass is retried soon
            log.exception("turma transcript back-fill failed")
            self._daily_at = min(self._daily_at, self.clock() + BACKFILL_RETRY_SECONDS)
        retry_at = self.clock() + BACKFILL_RETRY_SECONDS if self._retry_orgs else float("inf")
        self._backfill_at = min(self._daily_at, retry_at)

    async def _fail(self, household: str, cid: str, reason: str) -> None:
        self._titled.discard(cid)
        self._backoff.pop(cid, None)
        await self._set(household, cid, "failed", reason)
        metrics.incr("turma.meetings_failed")
        log.warning("meeting %s not delivered to turma: %s", cid, reason)

    async def requeue_failed(self) -> int:
        """Put meetings failed for a reason a restart with fixed config could change back to
        pending; returns how many."""
        self._booted_at = self.clock()
        rows = await asyncio.to_thread(self.store.requeue_failed_deliveries, REQUEUE_ON_BOOT)
        n = len(rows)
        if n:
            log.info("re-queued %d meeting(s) whose delivery to turma had failed", n)
        return n

    async def loop(self, interval: float = POLL_SECONDS) -> None:
        try:
            await self.requeue_failed()
        except Exception:  # a database outage: those meetings wait for the next boot
            log.exception("could not re-queue failed turma meeting deliveries")
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
            await self._maybe_backfill()

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


def _transient(resp: httpx.Response) -> bool:
    """An answer that may change by waiting: a 5xx, a 429, or a 200 that is not the hub's
    own JSON (a proxy or login page)."""
    return resp.status_code >= 500 or resp.status_code in (408, 429) or resp.status_code == 200


def _missing_ids(resp: httpx.Response) -> list[str] | None:
    """The hub's ``{meetingIds: [...]}`` answer, else None (a refusal, an older hub, a proxy)."""
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except ValueError:
        return None
    ids = payload.get("meetingIds") if isinstance(payload, dict) else None
    if not isinstance(ids, list):
        return None
    return [i for i in ids if isinstance(i, str)]


_delivery: TurmaDelivery | None = None


def config_problem() -> str | None:
    """Why the delivery settings can't work, else None: one of URL and token set without the
    other, or a URL that isn't http(s) with a host. Both empty is "off", not a problem."""
    url, token = settings.turma_url.strip(), settings.turma_meetings_token.strip()
    if bool(url) != bool(token):
        return f"{'API_TURMA_MEETINGS_TOKEN' if url else 'API_TURMA_URL'} is not set"
    parts = urlsplit(url)
    if url and (parts.scheme not in ("http", "https") or not parts.hostname):
        return "API_TURMA_URL must be an http(s) URL"
    return None


def configured() -> bool:
    """Delivery is on: both settings set and usable."""
    return bool(settings.turma_url.strip()) and config_problem() is None


def get_delivery() -> TurmaDelivery | None:
    return _delivery


def start(store: ConversationStore | None) -> TurmaDelivery | None:
    """Build the process-wide delivery when configured (lifespan)."""
    global _delivery
    _delivery = None
    if problem := config_problem():
        # A mistake, not a choice: say so, or meetings pile up pending silently.
        log.error("meeting delivery to turma is off: %s", problem)
        return None
    if store is None or not configured():
        return None
    url, token = settings.turma_url.strip(), settings.turma_meetings_token.strip()
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
