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
from concurrent.futures import ThreadPoolExecutor
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from api.contract import ScreenCadence, ScreenNotice, ScreenNoticeKind
from api.metrics import metrics
from api.persistence import ConversationStore, ScreenMeta, Segment, get_audio_store, stale
from api.screen.merge import CaptionMerger
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

# Caption parsing and merging are CPU work on attacker-shaped input; they get their own two
# threads so a flood of crafted frames queues here, not ahead of every store write in the
# default pool that the rest of the api shares.
CPU_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="screen-cpu")


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
        self.title: str | None = None
        self.conversation_id: str | None = None
        self.merger: CaptionMerger | None = None
        self.last_caption_ms = 0
        self.last_ts = 0
        # Captions-off warning: when the toolbar was last seen, and when to warn next.
        self._meeting_seen_ms: int | None = None
        self._captions_off_at: int | None = None
        self._caption_seen_ms: int | None = None
        self._stored_people: set[str] = set()
        self._stored_title: str | None = None
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
        self.active = roster.match(frame.active_speaker) if frame.active_speaker else None
        if frame.title:
            self.title = frame.title
        if self.conversation_id is not None:
            await self._store_people(ts)
            await self._store_title()
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
        if not blocks:
            return
        self.last_caption_ms = ts
        self._caption_seen_ms = ts
        if self.conversation_id is None:
            await self._start(ts)
        assert self.merger is not None
        if self.active:
            for b in blocks:
                if b.has_header and b.speaker and b.speaker != self.active:
                    # Captions lag the border by a few seconds, so a mismatch is a signal
                    # to watch (OCR misreads, a wrong roster match), not an error.
                    metrics.incr("screen.speaker_border_mismatch")
        # Off the event loop: the alignment is CPU work, and a slow one stalled every socket.
        loop = asyncio.get_running_loop()
        await self._store(await loop.run_in_executor(CPU_POOL, self.merger.feed, blocks, ts))

    async def _start(self, ts: int) -> None:
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
        self.merger = CaptionMerger(start_ms=ts, stable_ms=self.stable_ms)
        self._unsaved = []
        self._stored_people = set()
        self._stored_title = None
        metrics.incr("screen.meetings_started")
        log.info(
            "screen meeting started: %s (%s, %s)",
            conversation_id,
            self.meta.org_site_key,
            self.meta.platform,
        )
        await self._store_people(ts)
        await self._store_title()
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

    async def _store_title(self) -> None:
        if self.title and self.title != self._stored_title:
            await self._io(
                self.store.set_meeting_title, self.household, self.conversation_id, self.title
            )
            self._stored_title = self.title

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
        self.conversation_id = None
        self.merger = None
        # The next meeting on this window starts with whoever its tiles show then.
        self.roster = None
        self.active = None
        self.title = None
        await self._send(self.cadence.message(meeting=False, org_site_key=self.meta.org_site_key))
        return conversation_id


def now_ms() -> int:
    return int(time.time() * 1000)


class ScreenMeetings:
    """Process-wide trackers, keyed by (household, user, org site key)."""

    def __init__(self) -> None:
        self._trackers: dict[tuple[str, str, str], MeetingTracker] = {}

    def get(self, key: tuple[str, str, str]) -> MeetingTracker | None:
        return self._trackers.get(key)

    def put(self, key: tuple[str, str, str], tracker: MeetingTracker) -> MeetingTracker:
        return self._trackers.setdefault(key, tracker)

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
                    # Idle and no socket: nothing to keep for this window.
                    self._trackers.pop(key, None)
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


meetings = ScreenMeetings()


async def sweep_loop(interval: float = 5.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await meetings.sweep()
        except Exception:  # pragma: no cover - sweep() already guards each tracker
            log.exception("screen meeting sweep failed")
