"""Screen-meeting lifecycle (XERK-2505): one tracker per (household, user, org window).

A meeting starts when captions first appear on a window and ends after
``API_SCREEN_MEETING_END_SECONDS`` with none; it then becomes a ``ready`` conversation
(``source='screen'``) owned by the device token's user. Trackers live in the process, not on
the socket, so a capture client that reconnects mid-meeting carries on the same conversation.
A process that dies mid-meeting leaves the row ``live``; the boot stale sweep finishes it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Hashable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from dataclasses import dataclass

from api.contract import ScreenCadence, ScreenNotice, ScreenNoticeKind
from api.metrics import metrics
from api.persistence import ConversationStore, ScreenMeta, Segment, get_audio_store, stale
from api.screen import turma
from api.screen.merge import CaptionMerger, MergeBudget, OverBudget, _Line
from api.screen.zoom import CaptionBlock, FullFrame, Roster

log = logging.getLogger("api.screen")

Notify = Callable[[ScreenCadence | ScreenNotice], Awaitable[None]]

# A meeting toolbar seen this recently counts as a meeting on screen. A caption read this
# recently means captions are on and the meeting is just quiet: no warning. So a captioned
# meeting that goes silent stays unwarned, while captions turned off mid-meeting, or a next
# meeting in the same window without them, is warned this long + CAPTIONS_OFF_MS later.
MEETING_SEEN_MS = 600_000
# Meeting on screen this long with no caption: captions are off, so warn the operator...
CAPTIONS_OFF_MS = 120_000
# ...and again this often while it lasts (a desktop notification each time).
CAPTIONS_OFF_REPEAT_MS = 900_000

T = TypeVar("T")


class FairPool:
    """A thread pool whose waiting jobs take turns by user (XERK-2737).

    Each user (a device token's owner) has a FIFO queue; a free thread takes the next job from
    the user at the front of the line, who then goes to the back. So a user's frame waits
    behind at most one job per other busy user, not behind every frame they have queued: one
    user's crafted frames, or several users' together, slow live meetings in proportion to the
    number of users, never to the number of frames. A job whose caller was cancelled (socket
    closed) before it started is skipped. Thread-safe and loop-agnostic: every caller's own
    event loop gets its result."""

    def __init__(self, workers: int, name: str) -> None:
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._workers = workers
        self._busy = 0
        # Users with waiting jobs, the next to be served first (dicts keep insertion order).
        self._queues: dict[Hashable, deque[tuple[Callable[[], Any], asyncio.Future[Any]]]] = {}
        self._lock = threading.Lock()

    async def run(self, user: Hashable, fn: Callable[..., T], *args: Any) -> T:
        """Run ``fn(*args)`` on the pool in ``user``'s turn; its result or exception."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[T] = loop.create_future()
        with self._lock:
            self._queues.setdefault(user, deque()).append((lambda: fn(*args), fut))
        queued = time.perf_counter()
        self._next()
        try:
            return await fut
        finally:
            # Time spent waiting for a thread plus the work: how hard users contend.
            metrics.observe("screen.cpu_wait", (time.perf_counter() - queued) * 1000)

    def _next(self) -> None:
        while True:
            with self._lock:
                if self._busy >= self._workers or not self._queues:
                    return
                user = next(iter(self._queues))
                queue = self._queues.pop(user)
                call, fut = queue.popleft()
                if queue:
                    self._queues[user] = queue  # back of the line
                if fut.cancelled():
                    continue
                self._busy += 1
            try:
                self._pool.submit(self._call, call, fut)
            except RuntimeError as exc:  # the executor is shut down (process exit)
                with self._lock:
                    self._busy -= 1
                _settle(fut, None, exc)

    def _call(self, call: Callable[[], Any], fut: asyncio.Future[Any]) -> None:
        try:
            try:
                result, error = call(), None
            except BaseException as exc:  # all of them: an unsettled future hangs its caller
                result, error = None, exc
            _settle(fut, result, error)
        finally:
            with self._lock:
                self._busy -= 1
            self._next()


def _settle(fut: asyncio.Future[Any], result: Any, error: BaseException | None) -> None:
    """Hand a job's outcome to the loop its caller waits on (futures aren't thread-safe)."""

    def done() -> None:
        if fut.cancelled():
            return
        if isinstance(error, StopIteration):
            # A future refuses StopIteration, and an unset one hangs its caller.
            fut.set_exception(RuntimeError("pool job raised StopIteration"))
        elif error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(result)

    try:
        fut.get_loop().call_soon_threadsafe(done)
    except RuntimeError:
        pass  # the caller's loop is closed: nobody is waiting for this result


# Caption parsing and merging are CPU work on attacker-shaped input; they get their own two
# threads so a flood of crafted frames queues here, not ahead of every store write in the
# default pool that the rest of the api shares. Jobs take turns by user (XERK-2737).
CPU_POOL = FairPool(workers=2, name="screen-cpu")


class AccountGone(Exception):
    """The device token's user was deleted: stop recording for it."""


@dataclass(frozen=True)
class Cadence:
    idle_fps: float
    meeting_fps: float
    full_frame_seconds: float

    def message(self, *, meeting: bool, org_site_key: str | None = None) -> ScreenCadence:
        return ScreenCadence(
            type="screen.cadence",
            orgSiteKey=org_site_key,
            fps=self.meeting_fps if meeting else self.idle_fps,
            fullFrameSeconds=self.full_frame_seconds,
        )


class MeetingTracker:
    def __init__(
        self,
        household: str,
        owner: str,
        meta: ScreenMeta,
        *,
        store: ConversationStore,
        account_exists: Callable[[str], bool],
        cadence: Cadence,
        stable_ms: int,
        end_ms: int,
    ) -> None:
        self.household = household
        self.owner = owner
        # Whose turn this window's CPU work waits for on the shared pool (XERK-2737).
        self.user = (household, owner)
        self.meta = meta
        self.store = store
        self.account_exists = account_exists
        self.cadence = cadence
        self.stable_ms = stable_ms
        self.end_ms = end_ms
        self.lock = asyncio.Lock()
        # The socket currently feeding this window, for cadence changes; None when detached.
        self.notify: Notify | None = None
        self.roster: Roster | None = None
        self.active: str | None = None
        self.conversation_id: str | None = None
        self.merger: CaptionMerger | None = None
        # The ts last handed to the open meeting's merger, for the frame-gap metric (XERK-2722).
        self._fed_ts: int | None = None
        # The stored lines of the meeting that last ended here (XERK-2615), see on_caption.
        self._ended_lines: list[_Line] = []
        # Alignment work this window may spend (XERK-2730), shared by all its mergers: after a
        # meeting ends each frame gets a fresh one, and a bucket each refilled per frame (QA).
        # ``ScreenMeetings.put`` swaps in its user's, shared by all their windows.
        self.budget = MergeBudget()
        self.last_caption_ms = 0
        self._last_blocks: list[CaptionBlock] = []
        self.last_ts = 0
        # Captions-off warning: when the toolbar was last seen, and when to warn next.
        self._meeting_seen_ms: int | None = None
        self._captions_off_at: int | None = None
        self._caption_seen_ms: int | None = None
        self._stored_people: set[str] = set()
        # Final lines not yet written. The merger counts a line final once, so a failed or
        # cancelled write must be retried from here, never re-derived (it would dedup away).
        self._unsaved: list[Segment] = []

    async def _io(self, fn: Callable[..., object], *args: object) -> object:
        return await asyncio.to_thread(fn, *args)

    async def _send(self, msg: ScreenCadence | ScreenNotice) -> None:
        if self.notify is None:
            return
        try:
            await self.notify(msg)
        except Exception as exc:  # the socket went away; the next one gets the default
            log.info("screen cadence not delivered: %r", exc)

    def clock(self, ts: int) -> int:
        """The frame's capture time, kept monotonic per window."""
        self.last_ts = max(ts, self.last_ts)
        return self.last_ts

    async def ensure_roster(self) -> Roster:
        if self.roster is None:
            aliases = await self._io(
                self.store.speaker_aliases, self.household, self.owner, self.meta.org_site_key
            )
            self.roster = Roster(aliases)  # type: ignore[arg-type]
        return self.roster

    async def on_full(self, frame: FullFrame, ts: int) -> None:
        roster = await self.ensure_roster()
        for name in frame.participants:
            roster.add(name)
        # A cut-off name replaced by its full reading: relabel the lines held under it.
        if self.merger is not None:
            for old, new in roster.renamed.items():
                self.merger.rename(old, new)
        roster.renamed.clear()
        self.active = roster.match(frame.active_speaker) if frame.active_speaker else None
        # frame.title is not stored: in a remote-desktop window the top band is the client's
        # toolbar (Horizon's Ctrl+Alt+Del row became a meeting's title). The meeting is
        # titled by its topic when it is delivered (topic.py).
        if self.conversation_id is not None:
            await self._store_people(ts)
        if frame.in_meeting:
            self._meeting_seen_ms = ts
        await self._check_captions_off(ts)

    async def _check_captions_off(self, ts: int) -> None:
        """Warn when a meeting has been on screen CAPTIONS_OFF_MS with no caption read.

        Full frames keep coming while no caption shows, so this runs on each one. A meeting
        with captions on starts a conversation at its first caption, which clears it.
        """
        seen, caption = self._meeting_seen_ms, self._caption_seen_ms
        if (
            seen is None
            or ts - seen > MEETING_SEEN_MS
            or self.conversation_id is not None
            or (caption is not None and ts - caption <= MEETING_SEEN_MS)
        ):
            self._captions_off_at = None
        elif self._captions_off_at is None:
            self._captions_off_at = ts + CAPTIONS_OFF_MS
        elif ts >= self._captions_off_at:
            self._captions_off_at = ts + CAPTIONS_OFF_REPEAT_MS
            metrics.incr("screen.captions_off")
            await self._send(
                ScreenNotice(
                    type="screen.notice",
                    orgSiteKey=self.meta.org_site_key,
                    kind=ScreenNoticeKind.captions_off,
                )
            )

    async def on_caption(self, blocks: list[CaptionBlock], ts: int) -> None:
        try:
            await self._on_caption(blocks, ts)
        except OverBudget:
            # The merger's CPU bucket ran dry (XERK-2730): a crafted frame costs difflib
            # seconds on the shared CPU_POOL. Dropped like a frame latest-frame-wins skipped;
            # the merger is unchanged and the next snapshot re-reads the box.
            metrics.incr(f"screen.caption_over_budget.{self.meta.platform}")

    async def _on_caption(self, blocks: list[CaptionBlock], ts: int) -> None:
        if not blocks:
            return
        if (
            self.meta.platform == "teams"
            and self.merger is not None
            and not any(b.has_header and (b.speaker or b.avatar) for b in blocks)
            and not await CPU_POOL.run(self.user, self.merger.knows_any, blocks, ts)
        ):
            # No header the roster or an avatar vouches for, and re-reading no recent line:
            # not the panel but other text in the band, a slide mid-meeting whose headings
            # read as unknown headers (XERK-2614). Stored, it became speech; it doesn't hold
            # the meeting open either. A speaker off the roster has an avatar, so stays (QA).
            metrics.incr("screen.unaligned_crop_dropped")
            return
        # Teams passes header-less crops during a meeting (an entry whose header scrolled
        # off): one re-sent unchanged (a slide, tile text) must not hold the meeting open for
        # good (XERK-2609). Zoom keeps its old rule: any caption keeps it open.
        stale = (
            self.meta.platform == "teams"
            and not any(b.has_header for b in blocks)
            and blocks == self._last_blocks
        )
        if self.active:
            for b in blocks:
                if b.has_header and b.speaker and b.speaker != self.active:
                    # Captions lag the border by a few seconds, so a mismatch is a signal
                    # to watch (OCR misreads, a wrong roster match), not an error.
                    metrics.incr("screen.speaker_border_mismatch")
        merger = self.merger
        if merger is None:
            # A panel left on screen past the end timer (a break) still shows the ended
            # meeting's lines (XERK-2615): they align as known, and a crop holding nothing
            # else starts no meeting. Otherwise they were re-stored as a second conversation.
            merger = CaptionMerger(
                start_ms=ts, stable_ms=self.stable_ms, carried=self._ended_lines, budget=self.budget
            )
        # Off the event loop: the alignment is CPU work, and a slow one stalled every socket.
        try:
            segments = await CPU_POOL.run(self.user, merger.feed, blocks, ts)
        finally:
            # Merge CPU per snapshot, to retune merge.py's budget from real meetings.
            metrics.observe(f"screen.merge_cpu.{self.meta.platform}", merger.last_cpu_ms)
        # Only a frame the merger took holds the meeting open: one refused for its cost
        # (``OverBudget``) is dropped like one latest-frame-wins skipped (XERK-2730).
        self._last_blocks = blocks
        if not stale:
            self.last_caption_ms = ts
        self._caption_seen_ms = ts
        # A zero gap is no gap: ``clock`` holds ts flat while a client's clock runs ahead of
        # the last frame, and those zeros filled the shared percentiles (XERK-2701).
        if self.merger is not None and self._fed_ts is not None and ts > self._fed_ts:
            # The time between snapshots the merger sees decides its dropped-frame rules (a
            # Teams entry is revised in place only within 3 s, XERK-2717): measure it, per
            # platform, from real meetings rather than modelling it (XERK-2722). Only after
            # a feed that returned: a frame the merger never took is not a snapshot it saw.
            metrics.observe(f"screen.caption_gap.{self.meta.platform}", ts - self._fed_ts)
        self._fed_ts = ts
        if self.conversation_id is None:
            if merger.new_lines == 0:
                return
            await self._start(ts, merger)
        await self._store(segments)

    async def _start(self, ts: int, merger: CaptionMerger) -> None:
        if not await self._io(self.account_exists, self.owner):
            raise AccountGone(self.owner)
        if stale.is_pending(self.store):
            # The boot sweep of a previous process's live rows hasn't succeeded yet: run it
            # before this meeting's row exists, so it can't finalize it (XERK-1428).
            await self._io(stale.sweep_if_pending, self.store, get_audio_store())
        # A uuid4 string, the shape history accepts for a conversation id.
        conversation_id = str(uuid.uuid4())
        await self._io(
            lambda: self.store.create(
                self.household, conversation_id, owner=self.owner, screen=self.meta
            )
        )
        self.conversation_id = conversation_id
        self.merger = merger
        self._unsaved = []
        self._stored_people = set()
        metrics.incr("screen.meetings_started")
        log.info(
            "screen meeting started: %s (%s, %s)",
            conversation_id,
            self.meta.org_site_key,
            self.meta.platform,
        )
        await self._store_people(ts)
        await self._send(self.cadence.message(meeting=True, org_site_key=self.meta.org_site_key))

    async def _store_people(self, ts: int) -> None:
        if self.roster is None or self.merger is None:
            return
        new = [n for n in self.roster.names if n not in self._stored_people]
        if not new:
            return
        at = max(0, ts - self.merger.start_ms)
        await self._io(
            self.store.add_participants,
            self.household,
            self.conversation_id,
            [(n, at) for n in new],
        )
        self._stored_people.update(new)

    async def _store(self, segments: list[Segment]) -> None:
        """Write new final lines, after any a failed write left behind (upsert by id)."""
        self._unsaved.extend(segments)
        while self._unsaved:
            seg = self._unsaved[0]
            await self._io(self.store.add_segment, self.household, self.conversation_id, seg)
            self._unsaved.pop(0)
            metrics.incr("screen.lines")

    def expired(self, now_ms: int) -> bool:
        return self.conversation_id is not None and now_ms - self.last_caption_ms >= self.end_ms

    async def end(self, now_ms: int) -> str | None:
        """Flush the open meeting and mark it ready; returns its conversation id."""
        if self.conversation_id is None or self.merger is None:
            return None
        conversation_id = self.conversation_id
        await self._store(self.merger.flush(max(now_ms, self.last_ts)))
        await self._io(lambda: self.store.finish(self.household, conversation_id, status="ready"))
        log.info("screen meeting ended: %s", conversation_id)
        metrics.incr("screen.meetings_ended")
        # Post it to the Turma hub now rather than on the delivery loop's next poll.
        turma.wake()
        self.conversation_id = None
        self._ended_lines = self.merger.recent()
        self.merger = None
        # The next meeting on this window starts with whoever its tiles show then.
        self.roster = None
        self.active = None
        await self._send(self.cadence.message(meeting=False, org_site_key=self.meta.org_site_key))
        return conversation_id


def now_ms() -> int:
    return int(time.time() * 1000)


# Ended meetings' lines kept for windows whose tracker was dropped (see ScreenMeetings), and
# the users whose alignment-work buckets are kept.
_MAX_CARRIES = 1000
# A user's bucket holds this many windows' worth (merge._WORK_PER_S): two dense meetings at once.
_USER_WORK = 2


class ScreenMeetings:
    """Process-wide trackers, keyed by (household, user, org site key)."""

    def __init__(self) -> None:
        self._trackers: dict[tuple[str, str, str], MeetingTracker] = {}
        # An idle tracker with no socket is dropped, but its ended meeting's lines outlive it
        # (XERK-2615): a capture client that reconnects over a break re-sends the panel still
        # on screen, and a fresh tracker without them re-stored it as a second conversation.
        # Capped, oldest out: keys come from device-token clients.
        self._carries: dict[tuple[str, str, str], list[_Line]] = {}
        # One alignment-work bucket per user (XERK-2730), across their windows and sockets: a
        # device token feeds up to MAX_WINDOWS windows per socket into the shared 2-thread
        # CPU_POOL, and a bucket per window let one token take most of it. Capped, oldest out.
        self._budgets: dict[tuple[str, str], MergeBudget] = {}

    def get(self, key: tuple[str, str, str]) -> MeetingTracker | None:
        return self._trackers.get(key)

    def put(self, key: tuple[str, str, str], tracker: MeetingTracker) -> MeetingTracker:
        kept = self._trackers.setdefault(key, tracker)
        if kept is tracker:
            if (lines := self._carries.pop(key, None)) is not None:
                tracker._ended_lines = lines
            user = (key[0], key[1])
            tracker.budget = self._budgets.pop(user, None) or MergeBudget(_USER_WORK)
            self._budgets[user] = tracker.budget  # most recently used last
            while len(self._budgets) > _MAX_CARRIES:
                del self._budgets[next(iter(self._budgets))]
        return kept

    def _drop(self, key: tuple[str, str, str], tracker: MeetingTracker) -> None:
        self._trackers.pop(key, None)
        if tracker._ended_lines:
            self._carries.pop(key, None)
            self._carries[key] = tracker._ended_lines
            while len(self._carries) > _MAX_CARRIES:
                del self._carries[next(iter(self._carries))]

    def open_count(self) -> int:
        return sum(1 for t in self._trackers.values() if t.conversation_id is not None)

    def detach(self, notify: Notify) -> None:
        for t in self._trackers.values():
            if t.notify is notify:
                t.notify = None

    async def sweep(self, now: int | None = None) -> list[str]:
        """End every meeting whose captions have been gone long enough; returns their ids."""
        at = now_ms() if now is None else now
        ended = []
        for key, t in list(self._trackers.items()):
            async with t.lock:
                if t.expired(at):
                    try:
                        if (cid := await t.end(at)) is not None:
                            ended.append(cid)
                    except Exception:
                        log.exception("could not end screen meeting %s", t.conversation_id)
                        continue
                if t.conversation_id is None and t.notify is None:
                    # Idle and no socket: only its ended meeting's lines are kept.
                    self._drop(key, t)
        return ended

    async def close_all(self) -> None:
        """Shutdown: finish every open meeting so it reads ready, not live."""
        at = now_ms()
        for t in list(self._trackers.values()):
            async with t.lock:
                try:
                    await t.end(at)
                except Exception:
                    log.exception("could not end screen meeting %s on shutdown", t.conversation_id)
        self._trackers.clear()

    def clear(self) -> None:
        self._trackers.clear()
        self._carries.clear()
        self._budgets.clear()


meetings = ScreenMeetings()


async def sweep_loop(interval: float = 5.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await meetings.sweep()
        except Exception:  # pragma: no cover - sweep() already guards each tracker
            log.exception("screen meeting sweep failed")
