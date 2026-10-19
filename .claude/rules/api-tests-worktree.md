---
paths:
  - "api/**"
---

# Running the API tests from a worktree

- `import api` may resolve to ANOTHER worktree's editable install (`pip install -e` in a sibling
  checkout), so pytest silently tests that tree's source, not yours.
- Check first: `python -c "import api.session; print(api.session.__file__)"`.
- Pin it per run with `PYTHONPATH=$PWD/src` from `api/` rather than re-installing over a peer's env.
- Install `.[dev,persistence]` like CI: without psycopg the outage classifier sees no pool, and
  the faked-pool cases in `test_pg_unreachable.py` skip (they failed before XERK-2057).
- Don't pass `--no-cov` to `test_langid.py`: its `no_cover` timing tests then fail with
  `'NoneType' object has no attribute 'pause'` (pytest-cov's marker needs coverage on).

# Live Postgres tests (`TENIR_TEST_PG_DSN`) without Docker

- Hosts here have no Docker daemon; `pip install pgserver 'psycopg[binary,pool]'` in a scratch venv
  and `pgserver.get_server(<dir>).get_uri()` gives a real Postgres DSN for the `test_pg_*_live` suites.
- Its socket can vanish mid-run when the starting process exits; re-run `get_server` if it stops answering.
- Put pgserver's data dir under `/var/tmp`, not the session scratchpad: `/tmp/claude-0` is reset to
  0700 root, so the `pgserver` user loses access mid-run ("$libdir/plpgsql" permission errors, PANIC).
