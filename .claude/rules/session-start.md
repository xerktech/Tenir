---
paths:
  - api/src/api/main.py
  - api/src/api/registry.py
  - api/src/api/session.py
  - api/tests/test_resume.py
  - packages/client-core/src/ws.ts
  - packages/client-core/tests/ws.test.ts
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
- A warm resume onto a session still bound to an OPEN socket closes that socket with
  `WS_CLOSE_RESUMED_ELSEWHERE` (4001) before `rebind()`. Its handler is marked `displaced` first,
  so neither a queued frame nor a start already in flight touches the session the new socket owns.
- `displaced` is re-checked after every await in the `session.start` branch; a new await there
  needs its own check. The close runs as a background task: a frozen peer's close handshake can
  block 20 s (uvicorn legacy websockets) while the resume holds the start lock.
- The displaced handler awaits that close task at the end of its `finally`. Returning first lets
  uvicorn drop the transport and the 4001; the client sees 1006 and reconnects into a bounce. Clients must not
  reconnect on 4001, or two sockets with one id take the session from each other forever
  (`packages/client-core/src/ws.ts`, XERK-1526).
- Once a start knows it runs under a fresh server id, it releases the presented id's start lock
  before `start()`: holding it let anyone who knew a victim's id stall that victim's resume.
- A cold resume of a persisted id also reports `resumed=True`; `resumed` does not tell
  warm from cold.
- Worktree pitfall: the host's pip-installed `api` package may be another worktree's
  editable install. Check `python -c "import api; print(api.__file__)"`, and run tests
  with `PYTHONPATH=src` from `api/`, or you test someone else's code.
- Tests: `api/tests/test_resume.py::test_racing_cold_resumes_of_one_id_share_a_single_session`,
  `::test_warm_resume_closes_the_socket_it_takes_over`,
  `::test_a_start_in_flight_on_the_displaced_socket_leaves_the_session_alone`,
  `::test_a_cold_resume_in_flight_on_the_displaced_socket_leaves_the_session_alone`,
  `::test_a_foreign_id_start_does_not_hold_the_owner_off_its_resume`,
  `api/tests/test_resilience.py::test_start_cancelled_during_create_does_not_leave_the_row_live`.
