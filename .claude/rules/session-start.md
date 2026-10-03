---
paths:
  - api/src/api/main.py
  - api/src/api/registry.py
  - api/src/api/session.py
  - api/tests/test_resume.py
---

# Session start / resume (ws_endpoint)

- The registry is keyed by id alone, and `Session.start()` awaits store reads before
  `registry.register()`. Anything that checks the registry then starts must hold
  `registry.start_lock(id)` until it registers, or two reconnects with one id each start
  their own Session on the same conversation, one invisible to /health, revoke and
  shutdown (XERK-1514).
- Keep the start lock out of `close()`/teardown/grace paths: the handler closes the socket's
  previous session while holding it, so a teardown that took it would deadlock.
- A cancel can't stop an `asyncio.to_thread` store call: the thread runs on. The failed-start
  cleanup's `finish()` must wait for the `conversations.create` future, or the INSERT commits
  `live` after the row was finished (XERK-1529).
- A cold resume of a persisted id also reports `resumed=True`; `resumed` does not tell
  warm from cold.
- Worktree pitfall: the host's pip-installed `api` package may be another worktree's
  editable install. Check `python -c "import api; print(api.__file__)"`, and run tests
  with `PYTHONPATH=src` from `api/`, or you test someone else's code.
- Tests: `api/tests/test_resume.py::test_racing_cold_resumes_of_one_id_share_a_single_session`,
  `api/tests/test_resilience.py::test_start_cancelled_during_create_does_not_leave_the_row_live`.
