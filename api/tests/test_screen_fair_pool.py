"""The shared caption CPU pool takes turns by user (XERK-2737)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from api.persistence import InMemoryConversationStore, ScreenMeta
from api.screen import meetings as screen_meetings
from api.screen.meetings import Cadence, FairPool, MeetingTracker
from api.screen.zoom import CaptionBlock


async def _hold(pool: FairPool, user: str) -> tuple[asyncio.Task[None], threading.Event]:
    """Occupy the pool's one thread until the returned event is set."""
    gate, started = threading.Event(), threading.Event()

    def blocker() -> None:
        started.set()
        gate.wait(5)

    task = asyncio.create_task(pool.run(user, blocker))
    await asyncio.to_thread(started.wait, 5)
    return task, gate


def test_users_take_turns_however_many_frames_one_has_queued() -> None:
    async def run() -> list[str]:
        pool = FairPool(workers=1, name="test-fair")
        order: list[str] = []
        held, gate = await _hold(pool, "busy")
        jobs = [
            asyncio.create_task(pool.run(user, order.append, f"{user}{i}"))
            for user, n in (("a", 4), ("b", 4), ("live", 1))
            for i in range(n)
        ]
        await asyncio.sleep(0)  # every job queued while the thread is held
        gate.set()
        await asyncio.gather(held, *jobs)
        return order

    order = asyncio.run(run())
    # The live user's one frame waits behind one job per other user, not behind all eight.
    assert order == ["a0", "b0", "live0", "a1", "b1", "a2", "b2", "a3", "b3"]


def test_a_failing_job_raises_to_its_caller_and_the_pool_keeps_serving() -> None:
    async def run() -> int:
        pool = FairPool(workers=1, name="test-fair")
        with pytest.raises(ZeroDivisionError):
            await pool.run("a", lambda: 1 // 0)
        return await pool.run("a", lambda: 7)

    assert asyncio.run(run()) == 7


def test_a_job_cancelled_while_waiting_never_runs() -> None:
    async def run() -> list[str]:
        pool = FairPool(workers=1, name="test-fair")
        ran: list[str] = []
        held, gate = await _hold(pool, "a")
        gone = asyncio.create_task(pool.run("b", ran.append, "gone"))
        kept = asyncio.create_task(pool.run("c", ran.append, "kept"))
        await asyncio.sleep(0)
        gone.cancel()  # the socket closed before its frame got a thread
        gate.set()
        await asyncio.gather(held, kept)
        with pytest.raises(asyncio.CancelledError):
            await gone
        return ran

    assert asyncio.run(run()) == ["kept"]


def test_jobs_run_on_both_threads_at_once() -> None:
    async def run() -> None:
        pool = FairPool(workers=2, name="test-fair")
        both = threading.Barrier(2, timeout=5)
        # Each job waits for the other: only two threads running together get past it.
        await asyncio.gather(pool.run("a", both.wait), pool.run("a", both.wait))

    asyncio.run(run())


def test_a_windows_caption_work_waits_in_its_users_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    users: list[object] = []
    real = screen_meetings.CPU_POOL.run

    async def recording(user: object, fn, *args):  # type: ignore[no-untyped-def]
        users.append(user)
        return await real(user, fn, *args)

    monkeypatch.setattr(screen_meetings.CPU_POOL, "run", recording)
    tracker = MeetingTracker(
        "hh",
        "owner",
        ScreenMeta("site", "Org", "zoom"),
        store=InMemoryConversationStore(),
        account_exists=lambda _: True,
        cadence=Cadence(idle_fps=0.2, meeting_fps=1.0, full_frame_seconds=30.0),
        stable_ms=0,
        end_ms=60_000,
    )
    block = CaptionBlock(speaker="Ann", has_header=True, lines=["hello there"])
    asyncio.run(tracker.on_caption([block], 1_000))
    assert users and set(users) == {("hh", "owner")}
