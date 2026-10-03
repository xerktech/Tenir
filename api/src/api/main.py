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

from fastapi import Depends, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.websockets import WebSocketState

from api import registry
from api.auth import (
    AuthError,
    Principal,
    assert_secure_auth_config,
    assert_valid_oidc_config,
    get_user_store,
    principal_from_bearer,
    principal_from_token,
    require_admin,
)
from api.auth.tokens import renew_token_if_due
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
from api.logging_filters import install as install_log_redaction
from api.metrics import metrics
from api.persistence import get_conversation_store, stale
from api.persistence.postgres import SqlConversationStore
from api.protocol import ValidationError, parse_client_message, serialize
from api.readiness import probe_backends
from api.session import Session, is_valid_session_id, teardowns_in_flight
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
    # And, when the optional OIDC backend is on, refuse to boot half-configured
    # (XERK-649). No-op when it's off, so a non-OIDC deployment is unaffected.
    assert_valid_oidc_config()
    # Redact query-string bearer tokens from the access log before anything can
    # be logged (XERK-236): the WS handshake and the audio download both carry
    # the token in the URL, and uvicorn logs the full request line.
    install_log_redaction()
    # Apply schema.sql eagerly and refuse to boot if the database rejects it
    # (XERK-1409). Lazily on first use, a migration that fails only on production
    # data rolled out a pod that reported healthy while every session.start failed
    # (the XERK-1406 outage). Raising here makes the pod crashloop visibly instead.
    # An unreachable database stays non-fatal (open() only logs it).
    conversations = get_conversation_store()
    if isinstance(conversations, SqlConversationStore):
        await asyncio.to_thread(conversations.open)
    # Surface backend reachability at boot so a misconfigured/unreachable Postgres
    # or audio dir is visible immediately, not mid-session (it stays non-fatal:
    # connections are lazy and may still be warming up). The probe logs each
    # failure with its full detail itself.
    await asyncio.to_thread(probe_backends)
    # Only a graceful shutdown finalizes live sessions. An OOM kill, a host
    # reboot or a stop that overruns the grace period leaves rows stuck "live",
    # and nothing ever came back for them — they showed as permanently recording
    # in every client's history (XERK-236). Sweep them here, before any new
    # session can register, so a restart heals the previous process's mess. If
    # the database is down now, keep retrying in the background (and from every
    # session.start) until it succeeds (XERK-1428).
    stale_task: asyncio.Task[None] | None = None
    if conversations is not None:
        stale.arm(conversations)
        try:
            await asyncio.to_thread(stale.sweep_if_pending, conversations)
        except Exception:
            log.exception("could not finalize stale conversations at startup; will retry")
            stale_task = asyncio.create_task(stale.retry_loop(conversations))
    # Seed the component-status cache once at boot (so GET /status answers
    # immediately) and keep it fresh on a background loop.
    status_task: asyncio.Task[None] | None = None
    if settings.status_probe_interval_seconds > 0:
        try:
            await refresh()
        except Exception:
            log.exception("initial status probe failed")
        status_task = asyncio.create_task(probe_loop())
    # RSS ingest for the cue news corpus (XERK-120): only with live retrieval on —
    # the stripped core, and the stub path CI runs, never touch the network.
    rss_task: asyncio.Task[None] | None = None
    if settings.cue_backend != "off" and settings.cue_retrieval_backend == "live":
        from api.cue.rss import ingest_loop

        rss_task = asyncio.create_task(ingest_loop())
    yield
    if stale_task is not None:
        stale_task.cancel()
    if rss_task is not None:
        rss_task.cancel()
    if status_task is not None:
        status_task.cancel()
    # Finalize any still-live (incl. detached, grace-pending) sessions on shutdown so
    # their audio/transcript is persisted and resources are released cleanly.
    await close_all_sessions()


# Pod shutdown is SIGKILLed at terminationGracePeriodSeconds (30 s in prod), and
# uvicorn spends some of that draining connections before the lifespan exits. Stay
# well under it so every session is finalized rather than killed mid-close.
_SHUTDOWN_DEADLINE_S = 20.0
_SHUTDOWN_FINALIZE_S = 5.0


async def close_all_sessions(deadline: float = _SHUTDOWN_DEADLINE_S) -> None:
    """Finalize every still-live (incl. detached, grace-pending) session on shutdown.

    Closes run concurrently under one deadline (XERK-1458). One at a time, a single
    session against a hung model (STT flush + translation drain, ~30 s) used up the
    whole grace period and every later one was killed before persisting its audio.
    A teardown still running at the deadline is cancelled; it retains its audio
    first and finalizes the conversation even when cancelled.
    """
    sessions = registry.active()
    for session in sessions:
        registry.unregister(session)
    closes = [asyncio.create_task(session.close()) for session in sessions]
    # Teardowns already under way elsewhere (grace lapse, session.end, revoke, a
    # cancelled close() caller): their sessions left the registry first, and the
    # process exits as soon as this returns, so they must be waited on too.
    others = teardowns_in_flight()
    if not closes and not others:
        return
    _, pending = await asyncio.wait([*closes, *others], timeout=deadline)
    if pending:
        # close() only awaits its shielded teardown, so cancel the teardowns
        # themselves — the closes started above included.
        stuck = {*pending, *teardowns_in_flight()}
        log.warning("cancelling %d session teardown(s) past the shutdown deadline", len(stuck))
        for task in stuck:
            task.cancel()
        # A cancelled teardown still finalizes in its finally; bound that too, so
        # a hung store can't hold shutdown until the SIGKILL.
        await asyncio.wait(stuck, timeout=_SHUTDOWN_FINALIZE_S)
    for session, task in zip(sessions, closes, strict=True):
        if task.done() and not task.cancelled() and (exc := task.exception()) is not None:
            log.error("session %s close failed", session.session_id, exc_info=exc)


app = FastAPI(title="tenir api", version="0.1.1", lifespan=lifespan)

# Sliding token renewal (XERK-168): the header a renewed bearer token rides back
# on. Clients adopt it in their shared request path, so an actively-used device
# keeps refreshing its token and is never logged out by plain expiry.
RENEWED_TOKEN_HEADER = "X-Renewed-Token"

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
    # Browsers hide non-safelisted response headers from cross-origin JS unless
    # they are exposed — without this the web client would never see renewals.
    expose_headers=[RENEWED_TOKEN_HEADER],
)


@app.middleware("http")
async def sliding_token_renewal(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Re-issue the bearer token on any authenticated request past half its life.

    This is what keeps a device logged in until it manually logs out (XERK-168):
    the client swaps in the fresh token from ``X-Renewed-Token``, so only going
    unused for a whole token lifetime forces a re-login. Invalid/expired tokens
    add no header — the route's own dependency 401s them as before. The user-store
    lookup gates renewal on the account still existing (deleted users keep their
    hard expiry) and only runs when a renewal is actually due.
    """
    response = await call_next(request)
    authorization = request.headers.get("authorization", "")
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    if not token:
        return response
    fresh = renew_token_if_due(
        token, secret=settings.auth_secret, ttl_seconds=settings.auth_token_ttl_seconds
    )
    if fresh is None:
        return response
    user = await asyncio.to_thread(get_user_store().get_by_id, principal_from_token(fresh).user_id)
    if user is None:
        return response
    response.headers[RENEWED_TOKEN_HEADER] = fresh
    return response

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
        # Same resolver as the REST path: built-in HMAC (with the XERK-236 liveness
        # check that keeps a deleted user's still-unexpired token from opening a
        # capture socket) or, when enabled, an Authentik OIDC token (XERK-649).
        return principal_from_bearer(token)
    except AuthError:
        return None


async def _account_exists(user_id: str) -> bool:
    return await asyncio.to_thread(get_user_store().get_by_id, user_id) is not None


def _ws_reject_reason(ws: WebSocket) -> str:
    """Why ``_ws_principal`` returned None, for the rejection log line."""
    auth_header = ws.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else None
    token = token or ws.query_params.get("token")
    if not token:
        return "missing token"
    try:
        principal_from_bearer(token)
    except AuthError as exc:
        return str(exc)
    return "unknown"


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    principal = _ws_principal(ws)
    if principal is None:
        # Reject AFTER accepting, and log it. Closing before accept surfaces to
        # browser/RN clients as an opaque failed handshake (HTTP 403 → close code
        # 1006), indistinguishable from a network blip — so a client whose token
        # had merely expired reconnected forever ("stuck reconnecting" across
        # every component, 2026-07-28) instead of seeing the 1008 its close
        # handler treats as fatal-please-re-login. And nothing was logged, so the
        # loop was invisible server-side. Accepting first costs one round-trip
        # and delivers a close frame the client actually receives.
        log.warning("ws rejected: %s", _ws_reject_reason(ws))
        metrics.incr("ws.unauthorized")
        await ws.accept()
        # 1008 = policy violation; the client must present a valid token first.
        await ws.close(code=1008, reason="invalid or expired token")
        return
    await ws.accept()
    session: Session | None = None

    async def send(msg: ServerMessage) -> None:
        await ws.send_text(serialize(msg))

    async def close_removed() -> None:
        # A revoke calls this for every socket that ever bound the session, so skip
        # one that is already gone rather than count it as a removal.
        if WebSocketState.DISCONNECTED in (ws.client_state, ws.application_state):
            return
        # 1008, not a bare drop: clients treat 1006 as a blip and reconnect.
        log.warning("ws closed: account no longer exists")
        metrics.incr("ws.account_removed")
        await ws.close(code=1008, reason="account removed")

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
                # Auth runs at the handshake, and deleting a user only revokes the
                # sessions in the registry — so a socket with none at that moment (not
                # yet started, or after session.end) outlives its account. Re-check
                # here so it can't start recording into the household (XERK-1504).
                try:
                    alive = await _account_exists(principal.user_id)
                except Exception:
                    log.exception("account check failed for household %s", principal.household)
                    await send(_err("internal", "could not start session"))
                    continue
                if not alive:
                    if session is not None:
                        # The delete's revoke normally got here first; if not, finalize
                        # it now rather than let the finally below park it for resume.
                        registry.unregister(session)
                        await session.revoke("account deleted")
                        session = None
                    await close_removed()  # no-op if the revoke already closed it
                    break
                # A resume id the server could not have issued is not a resume id.
                # It reaches the conversation store AND the audio object key
                # ({household}/{id}.wav), so an id like "../other-hh/<their-id>"
                # addresses another household's retained audio — it reads back as
                # this session's resume offset and gets rewritten on session.end.
                # Drop it and start fresh under a server id (XERK-236).
                if msg.sessionId is not None and not is_valid_session_id(msg.sessionId):
                    log.warning(
                        "rejecting malformed resume id from household %s: %r",
                        principal.household,
                        msg.sessionId[:64],
                    )
                    metrics.incr("sessions.bad_resume_id")
                    msg = msg.model_copy(update={"sessionId": None})
                # Resume a still-live session if the client presents its id and both
                # the household AND the owner match: rebind to it, preserving the
                # transcriber state, instead of starting fresh. Owning the socket is
                # not enough — a recording belongs to the principal that created it,
                # so a different member (even in the same household) can never resume
                # into another user's live session and append to their recording
                # (XERK-651).
                resumable = registry.get(msg.sessionId) if msg.sessionId else None
                if (
                    resumable is not None
                    and resumable.household == principal.household
                    and resumable.user_id == principal.user_id
                ):
                    if session is not None and session is not resumable:
                        registry.unregister(session)
                        await session.close()
                    session = resumable
                    await session.rebind(send)
                    # A revoke must drop THIS socket too, not only the one the session
                    # was started on, or a resumed socket outlives its account (XERK-1504).
                    session.on_disconnect(close_removed)
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
                # Cold resume of a *persisted* recording is owner-gated too. create()
                # is idempotent by id, so without this a member presenting another
                # user's (or a legacy admin's) conversation id would append this
                # sitting's audio/segments onto that recording and overwrite its audio
                # key on end. Only the owner may reopen their own recording; anything
                # else starts fresh under a server id (XERK-651).
                if requested_id is not None:
                    convs = get_conversation_store()
                    existing = (
                        await asyncio.to_thread(convs.get, principal.household, requested_id)
                        if convs is not None
                        else None
                    )
                    if existing is not None and existing.owner != principal.user_id:
                        log.warning(
                            "rejecting cross-user resume of recording owned by another "
                            "user in household %s",
                            principal.household,
                        )
                        metrics.incr("sessions.cross_user_resume")
                        requested_id = None
                if session is not None:
                    registry.unregister(session)
                    await session.close()
                # A real backend (model/DB) can raise from Session()/start(); surface
                # it as an error frame instead of aborting the socket so a transient
                # backend outage doesn't 500 the connection.
                try:
                    new_session = Session(
                        send,
                        session_id=requested_id,
                        household=principal.household,
                        user_id=principal.user_id,
                    )
                    await new_session.start(
                        mic_source=msg.micSource,
                        source_lang=msg.sourceLang,
                    )
                except Exception:
                    log.exception("session.start failed for household %s", principal.household)
                    metrics.incr("sessions.start_errors")
                    await send(_err("internal", "could not start session"))
                    continue
                # Let an account deletion drop this socket, not just finalize
                # the session behind it (XERK-236).
                new_session.on_disconnect(close_removed)
                session = new_session
                registry.register(session)
                metrics.incr("sessions.started")
                # A delete that ran while start() was awaiting scanned the registry
                # before this session was in it. Checking only now that it is
                # registered closes that window: either the delete's scan sees it,
                # or its store.delete already happened and this check sees that.
                try:
                    alive = await _account_exists(principal.user_id)
                except Exception:
                    # The check above passed moments ago; don't drop a live
                    # recording over a transient store error.
                    log.exception("account re-check failed for session %s", session.session_id)
                    alive = True
                if not alive:
                    registry.unregister(session)
                    await session.revoke("account deleted")
                    session = None
                    break
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
