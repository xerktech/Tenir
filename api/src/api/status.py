"""Component status — per-backend health for ``GET /status``.

The stack used to fail silently when a model server was unreachable or still
loading: a live recording produced no captions and no signal anything was wrong.
This probes each *real* configured backend's health endpoint on a background loop
and caches the result so the api can surface a red/yellow/green light per
component (the infra stores, the STT server, and the LiteLLM gateway).

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
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from api.config import settings
from api.persistence import get_audio_store, get_conversation_store

log = logging.getLogger("api.status")

# Raw probe outcomes.
READY = "ready"
NOT_READY = "not_ready"  # reachable, but reports/looks not-yet-ready (loading)
UNREACHABLE = "unreachable"

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


def _now() -> float:
    """Wall clock (epoch seconds). Indirected so tests can pin the grace window."""
    return time.time()


def _short(exc: object) -> str:
    text = str(exc) or exc.__class__.__name__
    return text if len(text) <= 200 else text[:197] + "..."


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---- probe primitives (monkeypatched in tests) ------------------------------


async def _http_probe(url: str) -> tuple[str, str]:
    """Probe a model server's health endpoint: 200 → ready, other status →
    reachable-but-not-ready, transport error → unreachable."""
    try:
        async with httpx.AsyncClient(timeout=settings.status_probe_timeout_seconds) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001 - any transport failure means unreachable
        return UNREACHABLE, _short(exc)
    if resp.status_code == 200:
        return READY, "ready"
    if resp.status_code == 503:
        return NOT_READY, "loading (HTTP 503)"
    return NOT_READY, f"HTTP {resp.status_code}"


async def _infra_probe(fn: Callable[[], object] | None) -> tuple[str, str]:
    """Probe an infra store with one cheap call off the event loop."""
    if fn is None:
        return UNREACHABLE, "not configured"
    try:
        await asyncio.to_thread(fn)
    except Exception as exc:  # noqa: BLE001
        return UNREACHABLE, _short(exc)
    return READY, "reachable"


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
        raw, detail = await _infra_probe(conv.households if conv else None)
        results.append(("postgres", "Database (Postgres)", "infra", raw, detail))
    if s.audio_backend == "disk":
        audio = get_audio_store()
        raw, detail = await _infra_probe(audio.ready if audio else None)
        results.append(("audio", "Audio store (disk)", "infra", raw, detail))

    # --- LiteLLM gateway + the STT model behind it ---
    if s.stt_backend == "voxtral":
        gw = await _http_probe(f"{s.litellm_probe_url}/health/liveliness")
        results.append(("litellm", "LiteLLM gateway", "gateway", *gw))

        # Health of the LiteLLM-fronted STT model. The gateway is the api's real
        # path to it, so unless the deployment declares a direct URL it can
        # actually reach, the light mirrors the one gateway probe — probing a host
        # the api has no route to reports a false red for a server that is serving
        # traffic fine. A genuine outage still surfaces: the gateway's own /health
        # fails with it.
        if s.status_stt_url:
            raw, detail = await _http_probe(f"{s.status_stt_url}/health")
        else:
            raw, detail = gw
            detail = "reachable via LiteLLM gateway" if raw == READY else detail
        results.append(("stt", "Live STT (Voxtral)", "model", raw, detail))

    return results


def _apply(cid: str, raw: str, detail: str) -> tuple[str, str]:
    """Map a raw probe outcome to a display state, applying the grace window."""
    if raw == READY:
        _down_since.pop(cid, None)
        return STATE_READY, detail
    if raw == NOT_READY:
        # Reachable but warming up — that's yellow, and not "down" yet.
        _down_since.pop(cid, None)
        return STATE_CONNECTING, detail
    # UNREACHABLE: yellow within the grace window, red once it's been down too long.
    since = _down_since.setdefault(cid, _now())
    if _now() - since < settings.status_grace_seconds:
        return STATE_CONNECTING, f"connecting… ({detail})"
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


async def probe_loop() -> None:
    """Background loop: refresh the cached snapshot every probe interval."""
    while True:
        try:
            await refresh()
        except Exception:  # noqa: BLE001 - never let a probe failure kill the loop
            log.exception("status probe pass failed")
        await asyncio.sleep(settings.status_probe_interval_seconds)
