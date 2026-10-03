"""Best-effort backend reachability probe (deployment readiness).

CI exercises only the in-memory/stub backends, so a real deployment's first sign
of an unreachable Postgres/audio dir was a mid-session failure — ``/health``
reports "ok" regardless. This probes the *selected* real backends with one cheap
call each and reports per-backend status, surfaced at startup (logged) and via
``GET /ready``.

Memory/stub backends are trivially ready. The probe is non-fatal: an unreachable
backend doesn't block boot (connections are lazy and may still be warming up) — it
is just made visible. The real-backend calls do blocking I/O, so callers run this
off the event loop (``asyncio.to_thread``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from api.persistence import get_audio_store, get_conversation_store

log = logging.getLogger("api.readiness")


def _probe(name: str, fn: Callable[[], object]) -> str:
    try:
        fn()
        return "ok"
    except Exception as exc:  # noqa: BLE001 - report status, never raise
        # ``/ready`` is public, and a raw driver message carries backend PIDs,
        # lock/relation detail and the schema.sql path — so the full text goes to
        # the server log only and callers get a bare "error" (XERK-1427).
        log.warning("backend %s not ready: %s: %s", name, type(exc).__name__, exc)
        return "error"


def probe_backends() -> dict[str, str]:
    """Reachability of each configured backend: ``"ok"`` or ``"error"``.

    The failure detail is logged, never returned: the result is served on the
    unauthenticated ``/ready``.

    Only the enabled stores are probed (a disabled/``off`` store is omitted), with a
    cheap operation each — listing households, an audio-store ``ready()`` — so the
    same call works for both the in-memory and the real backends.
    """
    checks: dict[str, str] = {}
    conv = get_conversation_store()
    if conv is not None:
        checks["conversations"] = _probe("conversations", conv.households)
    audio = get_audio_store()
    if audio is not None:
        checks["audio"] = _probe("audio", audio.ready)
    return checks
