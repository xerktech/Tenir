"""tenir api: FastAPI + WebSocket.

One container serves everything: the WS capture endpoint (live STT via the
LiteLLM gateway), the auth + history REST API, and the built web UI as static
files. Sessions are recorded and stored — transcript segments in the
conversation store, full audio in the audio store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from api import registry
from api.auth import (
    AuthError,
    Principal,
    assert_secure_auth_config,
    principal_from_token,
    require_admin,
)
from api.auth.router import router as auth_router
from api.config import settings
from api.contract import (
    ErrorMessage,
    Ping,
    Pong,
    ServerMessage,
    SessionEnd,
    SessionReady,
    SessionStart,
)
from api.history import router as history_router
from api.metrics import metrics
from api.protocol import ValidationError, parse_client_message, serialize
from api.readiness import probe_backends
from api.session import Session
from api.status import probe_loop, refresh
from api.status import snapshot as status_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on the insecure default signing secret. Done at startup (not
    # import) so merely importing the app — codegen, tests, --help — never trips it,
    # and so it runs once per process rather than per import.
    assert_secure_auth_config()
    # Surface backend reachability at boot so a misconfigured/unreachable Postgres
    # or audio dir is visible immediately, not mid-session (it stays non-fatal:
    # connections are lazy and may still be warming up).
    checks = await asyncio.to_thread(probe_backends)
    for name, status in checks.items():
        if status != "ok":
            log.warning("backend %s not ready at startup: %s", name, status)
    # Seed the component-status cache once at boot (so GET /status answers
    # immediately) and keep it fresh on a background loop.
    status_task: asyncio.Task[None] | None = None
    if settings.status_probe_interval_seconds > 0:
        try:
            await refresh()
        except Exception:
            log.exception("initial status probe failed")
        status_task = asyncio.create_task(probe_loop())
    yield
    if status_task is not None:
        status_task.cancel()
    # Finalize any still-live (incl. detached, grace-pending) sessions on shutdown so
    # their audio/transcript is persisted and resources are released cleanly.
    for session in registry.active():
        registry.unregister(session)
        await session.close()


app = FastAPI(title="tenir api", version="0.1.1", lifespan=lifespan)

_cors_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # A wildcard origin combined with credentials is rejected by browsers. The
    # clients authenticate with bearer tokens (no cookies), so only enable
    # credentials when the origins are explicit — keeping the wildcard dev default
    # usable rather than silently breaking every cross-origin request.
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(history_router)


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "active_sessions": registry.count(),
        "stt_backend": settings.stt_backend,
    }


@app.get("/ready")
async def ready() -> Response:
    """Backend reachability for an orchestrator's readiness probe.

    Unlike ``/health`` (liveness — the process is up), this actually probes the
    selected real backends (Postgres, the audio dir) with one cheap call each;
    memory/stub backends are trivially ready. Returns 200 when all are reachable,
    503 otherwise, so a load balancer doesn't route to an api whose stores are down.
    """
    checks = await asyncio.to_thread(probe_backends)
    ok = all(status == "ok" for status in checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


@app.get("/status")
async def status() -> dict[str, object]:
    """Per-component health for the status view (public, like ``/health``).

    Returns the cached snapshot from the background probe loop — each configured
    backend with a red/yellow/green ``state`` (down / connecting / ready) — so the
    clients can show whether every component is healthy without each request
    triggering a live probe.
    """
    return status_snapshot()


@app.get("/metrics")
async def get_metrics(_: Principal = Depends(require_admin)) -> dict[str, object]:
    """Latency & resilience counters as a plain-JSON snapshot. Admin-gated (it
    exposes operational, tenant-agnostic data)."""
    snap = metrics.snapshot()
    snap["active_sessions"] = registry.count()
    return snap


def _ws_principal(ws: WebSocket) -> Principal | None:
    """Authenticate a WebSocket from its bearer token.

    The token rides in the ``Authorization`` header or a ``?token=`` query param
    (the Even Hub WS client can set either). Returns the principal, or ``None`` when
    the token is missing/invalid (the caller then closes the socket).
    """
    auth_header = ws.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    token = token or ws.query_params.get("token")
    if not token:
        return None
    try:
        return principal_from_token(token)
    except AuthError:
        return None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    principal = _ws_principal(ws)
    if principal is None:
        # 1008 = policy violation; the client must present a valid token first.
        await ws.close(code=1008)
        return
    await ws.accept()
    session: Session | None = None

    async def send(msg: ServerMessage) -> None:
        await ws.send_text(serialize(msg))

    try:
        while True:
            frame = await ws.receive()

            if frame["type"] == "websocket.disconnect":
                break

            # Binary frames are raw PCM audio (see the contract transport notes).
            if (data := frame.get("bytes")) is not None:
                if session is None:
                    await send(_err("bad_request", "audio before session.start"))
                    continue
                try:
                    await session.on_audio(data)
                except Exception:
                    # A bad frame or a transient STT-seam hiccup must not drop the
                    # whole socket. Log, count, keep the session open so capture
                    # continues.
                    log.exception("audio frame failed on session %s", session.session_id)
                    metrics.incr("audio.errors")
                continue

            text = frame.get("text")
            if text is None:
                continue  # keepalive / empty frame

            try:
                msg = parse_client_message(text)
            except ValidationError as e:
                log.warning("invalid client message: %s", e)
                await send(_err("bad_request", "could not parse message"))
                continue

            if isinstance(msg, SessionStart):
                # Resume a still-live session if the client presents its id and the
                # household matches: rebind to it, preserving the transcriber state,
                # instead of starting fresh.
                resumable = registry.get(msg.sessionId) if msg.sessionId else None
                if resumable is not None and resumable.household == principal.household:
                    if session is not None and session is not resumable:
                        registry.unregister(session)
                        await session.close()
                    session = resumable
                    await session.rebind(send)
                    await send(
                        SessionReady(
                            type="session.ready", sessionId=session.session_id, resumed=True
                        )
                    )
                    metrics.incr("sessions.resumed")
                    continue
                # A session id that is live under *another* household must never be
                # honored: the registry is keyed by id alone, so registering under it
                # would evict that household's running session (cross-household data
                # loss + isolation hole). Start fresh under a server-generated id.
                requested_id = msg.sessionId
                if requested_id is not None and registry.get(requested_id) is not None:
                    requested_id = None
                if session is not None:
                    registry.unregister(session)
                    await session.close()
                # A real backend (model/DB) can raise from Session()/start(); surface
                # it as an error frame instead of aborting the socket so a transient
                # backend outage doesn't 500 the connection.
                try:
                    new_session = Session(
                        send, session_id=requested_id, household=principal.household
                    )
                    await new_session.start(
                        mic_source=msg.micSource, source_lang=msg.sourceLang
                    )
                except Exception:
                    log.exception("session.start failed for household %s", principal.household)
                    metrics.incr("sessions.start_errors")
                    await send(_err("internal", "could not start session"))
                    continue
                session = new_session
                registry.register(session)
                metrics.incr("sessions.started")
            elif isinstance(msg, Ping):
                await send(Pong(type="pong", t=msg.t))
            elif session is None:
                await send(_err("session_not_found", "send session.start first"))
            elif isinstance(msg, SessionEnd):
                registry.unregister(session)
                await session.close()
                session = None
            elif msg.type == "mic.switch":
                session.set_mic_source(msg.micSource)

    except WebSocketDisconnect:
        log.info("client disconnected")
    finally:
        # Socket dropped without an explicit session.end: keep the session alive for
        # a grace window so a reconnect can resume it. Only detach if this handler
        # still owns the session — a concurrent resume may have rebound it to a new
        # connection, which must not be torn down here.
        if session is not None and not session.is_closed and session.current_send is send:
            session.detach(grace_seconds=settings.session_resume_grace_seconds)


def _err(code: str, message: str, *, fatal: bool = False) -> ErrorMessage:
    return ErrorMessage(type="error", code=code, message=message, fatal=fatal)


# ---- static web UI (single-container deployment) ----------------------------
# The built SPA is baked into the image at /srv/web (see api/Dockerfile) and
# mounted last so every API route above takes precedence. html=True serves
# index.html at "/", making the container a complete app on one origin — the
# SPA calls the same-origin API, so no CORS and no second container. When the
# directory is absent (local dev, tests) nothing is mounted; `vite dev` serves
# the UI instead.
_web_dir = Path(settings.web_dir)
if _web_dir.is_dir():  # pragma: no cover - exercised in the built image
    app.mount("/", StaticFiles(directory=_web_dir, html=True), name="web")
