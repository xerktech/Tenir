"""Active-session registry (Phase 2).

The enrolment API promotes a provisional ``speaker-N`` from a *live* session to a
named household person ("who was Speaker 2?"), so it needs to reach the running
`Session` by id. This is a tiny process-local registry; a multi-process or
clustered deployment (Phase 6) would back it with Redis behind the same calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from api.session import Session

_active: dict[str, "Session"] = {}


def register(session: "Session") -> None:
    _active[session.session_id] = session


def unregister(session: "Session") -> None:
    _active.pop(session.session_id, None)


def get(session_id: str) -> "Session | None":
    return _active.get(session_id)


def count() -> int:
    """Number of live sessions — surfaced on /health and /metrics (Phase 7)."""
    return len(_active)


def active() -> list["Session"]:
    """Snapshot of the live sessions (used for graceful shutdown)."""
    return list(_active.values())
