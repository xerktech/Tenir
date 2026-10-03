---
paths:
  - api/src/api/main.py
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
- Tests: `api/tests/test_db_unavailable.py`; `packages/client-core/tests/api.test.ts` (503 case).
