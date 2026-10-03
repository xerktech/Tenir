---
paths:
  - "api/src/api/persistence/postgres.py"
  - "api/src/api/auth/sql_users.py"
  - "api/src/api/auth/users.py"
  - "api/src/api/main.py"
  - "api/tests/test_pg_schema_apply.py"
  - "api/tests/test_pg_list_live.py"
  - "api/tests/test_pg_users_live.py"
  - "schema.sql"
---

# Boot schema apply (Postgres stores)

- schema.sql is applied eagerly in lifespan (`SqlConversationStore.open()`); a rejected schema
  raises `SchemaApplyError` and aborts startup, so the pod crashloops instead of going Ready.
  - Prod probes are all `/health` (liveness-shaped); a lazy or swallowed apply failure rolls out
    a "healthy" pod whose every session.start fails (XERK-1406, XERK-1409).
- Only an unreachable DB is non-fatal at boot (logged; `/ready` reports it; next use retries).
  - "Connection lost" means `conn.broken` or SQLSTATE class 08 / 57P01–57P03 — never "any psycopg
    `OperationalError`": that class includes real rejections (54000, 53100, 40P01).
  - A missing psycopg is fatal (permanent misconfig), not "not reachable".
- Cache a store's pool only after its schema applied, and close it on failure — caching first
  meant one failed apply was never retried.
- Pool open + apply runs under a per-store lock: without it concurrent first callers run the DDL
  in parallel and Postgres deadlocks (reported as a schema rejection).
- Across processes, every DDL apply (both stores) first takes `lock_schema()`
  (`pg_advisory_xact_lock(SCHEMA_LOCK_KEY)`) in the same, non-autocommit transaction.
  - Without it two api processes booting together (rolling update, >1 replica) fail one with 40P01
    or a `pg_type` unique violation, which aborts startup as a rejected schema (XERK-1509).
  - It does not cover apply-vs-request-traffic: a boot apply can still hit 40P01 against live
    writes, so 40P01 at boot is not proof the schema itself is bad.
  - Tests: `test_schema_apply_takes_the_cross_process_lock_first`,
    `test_concurrent_boot_applies_do_not_collide` (live).
- Both stores apply through `apply_boot_schema`: the user store runs schema.sql before its own
  DDL, since the users DDL references households (empty DB → UndefinedTable, XERK-1430).
- `get_user_store` retries `reconcile_admin` only while `_reconcile_is_retryable` (DB unavailable,
  its schema apply failed, 40001/40P01); any other failure is logged once, not retried.
  - Caching after a failed reconcile (DB down at boot, back empty) never seeded the env admin.
  - Retrying a permanent failure (e.g. FK on a non-default admin household, XERK-1508) fails
    every login and authenticated request, since they all go through `get_user_store`.
  - `get_user_store()` may block on the DB: call it inside `to_thread`, never as an argument
    evaluated on the event loop (`main.py` renewal middleware).
  - Tests: `test_auth.py` `test_failed_admin_reconcile_is_retried_on_next_access`,
    `test_permanent_admin_reconcile_failure_is_logged_not_retried`; live
    `test_concurrent_first_use_of_both_stores_on_an_empty_database`.
- Both stores open their pool through `PoolOpener` (postgres.py). It bounds every wait at
  `OPEN_TIMEOUT_SECONDS`: `pool.open(wait=True, timeout=)`, libpq `connect_timeout`, and the
  pool's per-request connection `timeout`. Callers within that window of a failed open share
  its error.
  - psycopg's 30s default, paid per call and serially behind `_pool_lock` while the DB was down,
    blocked boot ~2 min and every `/ready` 30-60s; with the DB dead after open, 50 requests
    each held a worker thread 30s and stalled every sync endpoint (XERK-1434).
  - Without `connect_timeout`, `pool.close()` after a timed-out open waits on connects to a
    blackholed host that never return.
- Pools are created with `check=ConnectionPool.check_connection`: without it every connection
  pooled before a Postgres restart failed one request (AdminShutdown) before being dropped.
- Token resolution reads the user store: never call it on the event loop (WS auth runs in
  `to_thread`) — a blocking read there froze the whole server while the DB was down.
- Probes (`/ready`, `/status`) call the store's bounded `ready()`, never a request-path query;
  `/ready` is single-flight so a burst of the public endpoint holds one worker thread.
  - Tests: `test_pg_unreachable.py`.
- Unit tests use fake *pools*, but CI installs `[persistence]` (psycopg): keep it.
  `test_database_rejections_are_schema_errors` raises real psycopg errors and skips without it,
  so dropping the extra silently disarms the OperationalError-misclassification guard.
- Real-Postgres coverage of schema.sql itself is `test_pg_schema_live.py` (`TENIR_TEST_PG_DSN`).
  - No Docker on the dev hosts: `pip install pgserver` and use `pgserver.get_server(dir).get_uri()`
    as the DSN. Its socket dies with the session that started it — restart it, don't reuse a DSN.
- One request-path read = one pool borrow. list()/search() read a whole page's children with
  `= ANY(%s)` via `_assemble`; never call get() per row (1+N queued borrows on a 4-conn pool,
  XERK-1518). Child ORDER BY carries the id as tie-breaker so get/list/search agree.
  - Tests: `test_pg_list_live.py`.
- Tests: `test_pg_schema_apply.py` — `test_boot_fails_when_the_database_rejects_the_schema`,
  `test_database_rejections_are_schema_errors`, `test_connection_lost_mid_apply_is_not_fatal`,
  `test_concurrent_first_use_applies_the_schema_once`.
- Never add a unique index that existing data can violate as a plain boot statement: the apply
  aborts and the pod crashloops. Guard it in a `DO` block that skips it when violated, and log the
  offending rows (`users_username_lower_idx`, XERK-1535).
  - A `DO $$ ... $$` body can't go in schema.sql: `iter_statements` splits on `;`. Put it in the
    store's `_ENSURE_SCHEMA`.
  - Tests: `test_pg_users_live.py` `test_legacy_case_variant_duplicates_do_not_abort_boot`.
