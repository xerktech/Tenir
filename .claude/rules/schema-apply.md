---
paths:
  - "api/src/api/persistence/postgres.py"
  - "api/src/api/auth/sql_users.py"
  - "api/src/api/main.py"
  - "api/tests/test_pg_schema_apply.py"
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
- Unit tests use fake pools (psycopg isn't in CI's api env); real-Postgres coverage of schema.sql
  itself is `test_pg_schema_live.py` (needs `TENIR_TEST_PG_DSN`).
- Tests: `test_pg_schema_apply.py` — `test_boot_fails_when_the_database_rejects_the_schema`,
  `test_database_rejections_are_schema_errors`, `test_connection_lost_mid_apply_is_not_fatal`,
  `test_concurrent_first_use_applies_the_schema_once`.
