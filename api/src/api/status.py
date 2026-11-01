"""Component status — per-backend health for ``GET /status``.

The stack used to fail silently when a model server was unreachable or still
loading: a live recording produced no captions and no signal anything was wrong.
This probes each *real* configured backend's health endpoint on a background loop
and caches the result so the api can surface a red/yellow/green light per
component (the infra stores, the STT + cue-LLM model servers, and the LiteLLM
gateway that fronts them).

Detection is pure HTTP so it works across physical hosts (no Docker access). Each
probe yields a raw outcome — ``READY`` / ``NOT_READY`` (reachable but loading) /
``UNREACHABLE`` — and a per-component tracker maps that to a display state,
holding an unreachable component at ``connecting`` for a grace window before it
settles to ``down``. That grace window is what gives a vLLM server (which refuses
connections until its model has loaded) a truthful yellow at startup: a loading
model goes yellow→green, a genuinely dead one goes yellow→red.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from api.config import settings
from api.persistence import get_audio_store, get_conversation_store
from api.persistence.postgres import is_database_unavailable
from api.screen import turma

log = logging.getLogger("api.status")

# httpx logs every request at INFO; a probe pass every few seconds made that 12
# lines/min per target (XERK-1818). The flag is a contextvar, so it is set only in
# the probe's own task and LiteLLM/translation requests keep their httpx logs.
_in_probe: contextvars.ContextVar[bool] = contextvars.ContextVar("status_probe", default=False)


class _QuietProbeRequests(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or not _in_probe.get()


logging.getLogger("httpx").addFilter(_QuietProbeRequests())

# Raw probe outcomes.
READY = "ready"
NOT_READY = "not_ready"  # reachable, but reports/looks not-yet-ready (loading)
UNREACHABLE = "unreachable"
MISCONFIGURED = "misconfigured"  # a setting that can't work: red at once, no grace window

# Display states (the dashboard's red/yellow/green).
STATE_READY = "ready"  # green
STATE_CONNECTING = "connecting"  # yellow
STATE_DOWN = "down"  # red


@dataclass(frozen=True)
class Component:
    id: str
    label: str
    category: str  # "infra" | "model" | "gateway"
    state: str
    detail: str
    checked_at: float  # epoch seconds


# Cached snapshot (last probe pass) + per-component "first seen unreachable" timer.
_cache: list[Component] = []
_down_since: dict[str, float] = {}
# Failure classes already logged per probe target in its current outage, so a
# persistent failure logs once rather than on every pass (XERK-1438). A set, not
# the last class: a down database alternates PoolTimeout / DatabaseUnavailable.
_logged_failures: dict[str, set[str]] = {}


def _now() -> float:
    """Wall clock (epoch seconds). Indirected so tests can pin the grace window."""
    return time.time()


def _public_detail(target: str, exc: BaseException) -> str:
    """``/status`` is public, and a raw probe error carries internal hostnames,
    absolute paths and SQL text — so the full message is logged server-side and
    the body gets only the exception class (XERK-1427).

    Logged once per failure class per outage, and on one line: psycopg
    messages carry ``LINE 1:`` and caret lines that split in line-based aggregators.
    """
    # One outage, one class: during a boot-time outage PoolOpener alternates a real
    # attempt (PoolTimeout) with its shared verdict (DatabaseUnavailable), which
    # flipped the public detail every few seconds (XERK-1818).
    name = "DatabaseUnavailable" if is_database_unavailable(exc) else type(exc).__name__
    seen = _logged_failures.setdefault(target, set())
    if name not in seen:
        seen.add(name)
        log.warning(
            "status probe of %s failed: %s: %s",
            target,
            type(exc).__name__,
            " ".join(str(exc).split()),
        )
    return f"unreachable: {name}"


def _probe_ok(target: str) -> None:
    """Note a reachable target (any answer, even a 5xx), ending its outage so its
    next failure is logged again."""
    if _logged_failures.pop(target, None) is not None:
        log.info("status probe of %s reachable again", target)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---- probe primitives (monkeypatched in tests) ------------------------------


async def _http_probe(url: str) -> tuple[str, str]:
    """Probe a model server's health endpoint: 200 → ready, other status →
    reachable-but-not-ready, transport error → unreachable."""
    token = _in_probe.set(True)
    try:
        async with httpx.AsyncClient(timeout=settings.status_probe_timeout_seconds) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - any transport failure means unreachable
        return UNREACHABLE, _public_detail(url, exc)
    finally:
        _in_probe.reset(token)
    _probe_ok(url)
    if resp.status_code == 200:
        return READY, "ready"
    # The quieted httpx line was the only server-side record of why a model is yellow.
    log.info("status probe of %s: HTTP %d", url, resp.status_code)
    if resp.status_code == 503:
        return NOT_READY, "loading (HTTP 503)"
    return NOT_READY, f"HTTP {resp.status_code}"


async def _infra_probe(fn: Callable[[], object] | None) -> tuple[str, str]:
    """Probe an infra store with one cheap call off the event loop."""
    if fn is None:
        return UNREACHABLE, "not configured"
    target = getattr(fn, "__qualname__", "store")
    try:
        await asyncio.to_thread(fn)
    except Exception as exc:  # noqa: BLE001
        return UNREACHABLE, _public_detail(target, exc)
    _probe_ok(target)
    return READY, "reachable"


# Failed deliveries count on /status for meetings that ended within this window; older
# failures stay recorded on their conversation but no longer hold the light red.
DELIVERY_FAILURE_WINDOW = timedelta(days=7)


async def _delivery_probe(store) -> tuple[str, str]:
    """Meetings waiting for or refused by the Turma hub: any recent failure is red (after the
    grace window), meetings still being retried yellow, all delivered green. Counts and the
    hub's words only, never a meeting's title: /status is public."""
    since = datetime.now(timezone.utc) - DELIVERY_FAILURE_WINDOW
    try:
        summary = await asyncio.to_thread(store.delivery_summary, since)
    except Exception as exc:  # noqa: BLE001
        return UNREACHABLE, _public_detail("delivery_summary", exc)
    failed, pending = summary["failed"], summary["pending"]
    if failed:
        detail = f"{failed} meeting(s) not delivered"
        if summary.get("error"):
            detail += f": {summary['error']}"
        return UNREACHABLE, detail
    if pending:
        return NOT_READY, f"{pending} meeting(s) waiting to be delivered"
    return READY, "all meetings delivered"


# ---- registry + state machine ------------------------------------------------

# A raw probe result for one component, before the grace-window mapping.
_RawResult = tuple[str, str, str, str, str]  # (id, label, category, raw, detail)


async def _gather() -> list[_RawResult]:
    """Probe every *active* (real-backend) component. Stub/off/memory backends have
    no server to reach, so they are omitted — the dashboard adapts to the deploy."""
    s = settings
    results: list[_RawResult] = []

    # --- infra stores (no "loading" phase: ready or down) ---
    if s.persistence_backend == "postgres":
        conv = get_conversation_store()
        raw, detail = await _infra_probe(conv.ready if conv else None)
        results.append(("postgres", "Database (Postgres)", "infra", raw, detail))
    if s.audio_backend == "disk":
        audio = get_audio_store()
        raw, detail = await _infra_probe(audio.ready if audio else None)
        results.append(("audio", "Audio store (disk)", "infra", raw, detail))

    # --- Turma hub delivery of finished meetings (XERK-2513) ---
    conv_store = get_conversation_store()
    if problem := turma.config_problem():
        # Meetings pile up pending while misconfigured: show it, not just a boot log line.
        results.append(("turma", "Meetings to Turma", "infra", MISCONFIGURED, problem))
    elif turma.configured() and conv_store is not None:
        raw, detail = await _delivery_probe(conv_store)
        results.append(("turma", "Meetings to Turma", "infra", raw, detail))

    # --- LiteLLM gateway + the models behind it ---
    # Both the live STT engine and the cue LLM reach their model through the same
    # LiteLLM gateway, so the gateway is probed once whenever *either* is backed by a
    # real server (stub/off backends have nothing to reach). Each model light then
    # resolves against that single gateway probe (see _gateway_fronted_model).
    # The offline Parakeet model backs both partials and finals under the parakeet
    # backend.
    stt_via_gateway = s.stt_backend == "parakeet"
    cue_via_gateway = s.cue_backend == "openai"
    if stt_via_gateway or cue_via_gateway:
        gw = await _http_probe(f"{s.litellm_probe_url}/health/liveliness")
        results.append(("litellm", "LiteLLM gateway", "gateway", *gw))

        if stt_via_gateway:
            raw, detail = await _gateway_fronted_model(gw, s.status_stt_url)
            results.append(("stt", "Live STT (Parakeet)", "model", raw, detail))

        if cue_via_gateway:
            raw, detail = await _gateway_fronted_model(gw, s.status_llm_url)
            results.append(("llm", "Cue LLM", "model", raw, detail))

    return results


async def _gateway_fronted_model(gw: tuple[str, str], direct_url: str) -> tuple[str, str]:
    """Resolve the health of a LiteLLM-fronted model server.

    The gateway is the api's real path to the model, so unless the deployment
    declares a direct URL it can actually reach, the model light mirrors the one
    gateway probe — probing a host the api has no route to reports a false red for a
    server that is serving traffic fine. A genuine outage still surfaces: the
    gateway's own /health fails with it. When a direct URL *is* configured (the
    single-host stack), probe it for true per-model resolution.
    """
    if direct_url:
        return await _http_probe(f"{direct_url}/health")
    raw, detail = gw
    return raw, ("reachable via LiteLLM gateway" if raw == READY else detail)


def _apply(cid: str, raw: str, detail: str) -> tuple[str, str]:
    """Map a raw probe outcome to a display state, applying the grace window."""
    if raw == READY:
        _down_since.pop(cid, None)
        return STATE_READY, detail
    if raw == MISCONFIGURED:
        # Waiting can't fix a config error, so it skips the "could still be loading" grace.
        _down_since.pop(cid, None)
        return STATE_DOWN, detail
    if raw == NOT_READY:
        # Reachable but warming up — that's yellow, and not "down" yet.
        _down_since.pop(cid, None)
        return STATE_CONNECTING, detail
    # UNREACHABLE: yellow within the grace window, red once it's been down too long.
    # The state already says "connecting", so the detail is passed through unwrapped.
    since = _down_since.setdefault(cid, _now())
    if _now() - since < settings.status_grace_seconds:
        return STATE_CONNECTING, detail
    return STATE_DOWN, detail


async def refresh() -> list[Component]:
    """Run one probe pass over all active components and update the cache."""
    raw_results = await _gather()
    now = _now()
    components = [
        Component(cid, label, category, *_apply(cid, raw, detail), now)
        for cid, label, category, raw, detail in raw_results
    ]
    seen = {c.id for c in components}
    for cid in list(_down_since):
        if cid not in seen:
            _down_since.pop(cid, None)
    global _cache
    _cache = components
    return components


def _overall(components: list[Component]) -> str:
    if not components:
        return STATE_READY
    states = {c.state for c in components}
    if states == {STATE_READY}:
        return "ready"
    if states == {STATE_DOWN}:
        return "down"
    return "degraded"


def _reasons(components: list[Component]) -> list[str]:
    """Concise, machine-readable reasons the overall status isn't fully ready — one
    line per component not in the green state, so a client can show *why* it's
    degraded without re-deriving it from the component list itself."""
    return [
        f"{c.id}: {c.state} ({c.detail})" if c.detail else f"{c.id}: {c.state}"
        for c in components
        if c.state != STATE_READY
    ]


def snapshot() -> dict:
    """The cached status as the ``GET /status`` JSON body."""
    components = _cache
    generated = max((c.checked_at for c in components), default=_now())
    return {
        "overall": _overall(components),
        "generatedAt": _iso(generated),
        "reasons": _reasons(components),
        # Client compatibility: the Windows capture client checks this on start (XERK-2506).
        "clients": {"desktop": {"minVersion": settings.desktop_min_version or None}},
        "components": [
            {
                "id": c.id,
                "label": c.label,
                "category": c.category,
                "state": c.state,
                "detail": c.detail,
                "checkedAt": _iso(c.checked_at),
            }
            for c in components
        ],
    }


def reset() -> None:
    """Clear cache + trackers (tests)."""
    global _cache
    _cache = []
    _down_since.clear()
    _logged_failures.clear()


async def probe_loop() -> None:
    """Background loop: refresh the cached snapshot every probe interval."""
    while True:
        try:
            await refresh()
        except Exception:  # noqa: BLE001 - never let a probe failure kill the loop
            log.exception("status probe pass failed")
        await asyncio.sleep(settings.status_probe_interval_seconds)
