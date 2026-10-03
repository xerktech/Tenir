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
- A warm resume onto a session still bound to an OPEN socket closes that socket with
  `WS_CLOSE_RESUMED_ELSEWHERE` (4001) before `rebind()`. Its handler stops reading frames first,
  so a queued `session.end` can't end the session the new socket owns. Clients must not
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
  `::test_a_foreign_id_start_does_not_hold_the_owner_off_its_resume`.
