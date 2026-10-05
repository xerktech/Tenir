---
paths:
  - api/src/api/main.py
  - api/src/api/session.py
  - api/src/api/persistence/postgres.py
  - packages/client-core/src/api.ts
  - packages/client-core/src/ws.ts
  - api/tests/test_db_unavailable.py
  - api/tests/test_pg_outage_live.py
  - api/tests/test_pg_unreachable.py
---

# Database outage contract (XERK-1510)

- A database outage is 503 + `Retry-After` on REST and an accept-then-close **1013** on the WS
  handshake. Never a 500, never a 401/1008 — clients read 401/1008 as "re-login".
- `is_database_unavailable()` (postgres.py) is the one classifier. Don't treat every psycopg
  `OperationalError` as an outage: disk full (53100) / index row too large (54000) are real faults.
- Handlers are registered per class from `database_error_types()`; psycopg is an optional extra,
  so never import it at module top in code the in-memory install loads.
- Any request-path `get_by_id` outside a route (e.g. the sliding-renewal middleware) must swallow
  an outage itself — exception handlers don't cover middleware, so it would 500 a good response.
- Starlette runs app exception handlers for websocket routes too — `_database_unavailable` must
  keep its `WebSocket` branch (close 1013), or an outage mid-socket crashes on `request.method`.
- Its 1013 close is best-effort: a client that left while the pool waited still reads CONNECTED
  (starlette learns of a disconnect only on `receive()`), and `close()` raises per ws impl.
- client-core resets the WS reconnect backoff on `session.ready`, not on open: an accept-then-1013
  would otherwise retry at the base delay for the whole outage.
- client-core turns a 503 into `ServerUnavailableError`, a `NetworkError` subclass on purpose:
  every client already keeps the session and retries on `NetworkError` (web, Android, Even).
- A session ending mid-outage must not raise out of `Session.close()`, and the client never
  resumes an ended session: `_persist` defers the finish to `_finalize_deferred` (XERK-1531).
  The boot stale sweep only covers a *previous* process's rows, so nothing else would finish it.
- Every transcript write (segment, translation, cue, song) goes through `Session._store`: one
  ordered queue per session, flushed off the pump. Inline, each write waited out the 5 s pool
  timeout during an outage, so captions went stale; an outage raised there killed the pump or
  translation worker and lost every later turn.
- The queue is ordered on purpose: a translation's UPDATE must land after its segment's INSERT.
- `_finalize` refuses while an *outage* left the audio key unwritten (else ready but unplayable).
  Any other failure must finalize (XERK-236): one stuck session stalls the serial retry.
- Deferred finalizes retry serially in one task: an outage ties up one executor thread, not one
  per ended session. Held writes are lost if the process exits before the database is back.
- A pass stops at the first session held by an *outage*; one held by its own row's lock (57014)
  is skipped so the sessions behind it still finalize (`_held_by_outage`).
- Pools come from `PoolOpener` only: its `GuardedPool` bounds a hung server client-side (XERK-1513).
  A SIGSTOPped server ACKs at TCP, so connect_timeout, keepalives and statement_timeout never fire.
- The watchdog severs with `shutdown()` on a dup of libpq's fd; never close libpq's own fd.
- The borrow watchdog is armed only after the boot schema applied: DDL may run for minutes.
- Every pooled connection gets `statement_timeout` (STATEMENT_TIMEOUT < QUERY_TIMEOUT): severing
  only the client left lock-blocked backends running, and the pool grew ~4 conns/15s (QA).
  `apply_boot_schema` lifts it with `SET LOCAL` before taking the advisory lock.
- Its 57014 is not an outage (stays a 500): the stale sweep and admin seed would retry a statement
  slow every time forever. `test_reconcile_is_retryable_classification`.
- Session-held writes (`_flush_writes`, `_finalize`, `_retain_audio`) use `is_retryable_write`
  (outage or 57014): dropping on 57014 lost segments and the audio key behind a >10s lock.
- `reconnect_timeout` stays short: psycopg's 5-min default backs off to 64s, so the first request
  after an outage succeeded up to a minute late. Giving up also opens the outage breaker.
- The breaker opens only on a failed reconnect with no successful borrow for OPEN_TIMEOUT, never
  on a `PoolTimeout`: load must not trip it, nor one unrefillable slot at max_connections.
  It shortens the wait rather than skipping `getconn`, which is what starts the next reconnect.
- Don't cap the pool's `max_waiting`: min=max=4, so 40 healthy concurrent requests hit any cap.
- Tests: `api/tests/test_finalize_outage.py`.
- Tests: `api/tests/test_pg_outage_live.py` (TCP proxy that refuses or freezes a real Postgres).
- Tests: `api/tests/test_db_unavailable.py`; `packages/client-core/tests/api.test.ts` (503 case).
