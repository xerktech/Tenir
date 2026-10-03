"""Active-session registry (Phase 2).

The enrolment API promotes a provisional ``speaker-N`` from a *live* session to a
named household person ("who was Speaker 2?"), so it needs to reach the running
`Session` by id. This is a tiny process-local registry; a multi-process or
clustered deployment (Phase 6) would back it with Redis behind the same calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.session import Session

_active: dict[str, "Session"] = {}

# Per-id locks serializing session starts, with a holder count so an entry is
# dropped once nobody holds or waits on it (ids are unbounded over a process life).
_start_locks: dict[str, tuple[asyncio.Lock, int]] = {}


def register(session: "Session") -> None:
    _active[session.session_id] = session


def unregister(session: "Session") -> None:
    # Only if it is still this session: two Sessions can share an id (racing cold
    # resumes), and a stale one's grace close must not evict the live one (XERK-1507).
    if _active.get(session.session_id) is session:
        del _active[session.session_id]


def get(session_id: str) -> "Session | None":
    return _active.get(session_id)


def count() -> int:
    """Number of live sessions — surfaced on /health and /metrics (Phase 7)."""
    return len(_active)


def active() -> list["Session"]:
    """Snapshot of the live sessions (used for graceful shutdown)."""
    return list(_active.values())


@asynccontextmanager
async def start_lock(session_id: str | None) -> AsyncIterator[None]:
    """Hold the start lock for `session_id` across a get-then-start-then-register.

    Without it two cold resumes of one id both find nothing registered and each
    start a Session on the same conversation (XERK-1514). A None id (the server
    will mint a fresh one) has nothing to race on, so it takes no lock.
    """
    if session_id is None:
        yield
        return
    lock, holders = _start_locks.get(session_id, (asyncio.Lock(), 0))
    _start_locks[session_id] = (lock, holders + 1)
    try:
        async with lock:
            yield
    finally:
        lock, holders = _start_locks[session_id]
        if holders == 1:
            del _start_locks[session_id]
        else:
            _start_locks[session_id] = (lock, holders - 1)
