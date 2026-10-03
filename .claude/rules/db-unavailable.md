---
paths:
  - api/src/api/main.py
  - api/src/api/session.py
  - api/src/api/persistence/postgres.py
  - packages/client-core/src/api.ts
  - packages/client-core/src/ws.ts
  - api/tests/test_db_unavailable.py
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
- `_finalize` refuses while the audio key is unwritten, or the row ends ready but unplayable.
- Deferred finalizes retry serially in one task: an outage ties up one executor thread, not one
  per ended session. Held writes are lost if the process exits before the database is back.
- Tests: `api/tests/test_finalize_outage.py`.
- Tests: `api/tests/test_db_unavailable.py`; `packages/client-core/tests/api.test.ts` (503 case).
