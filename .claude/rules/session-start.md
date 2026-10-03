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
- Never await another session's teardown, or anything that takes the start lock, while
  holding it: a resume waits on that lock.
- A cold resume of a persisted id also reports `resumed=True`; `resumed` does not tell
  warm from cold.
- Worktree pitfall: the host's pip-installed `api` package may be another worktree's
  editable install. Check `python -c "import api; print(api.__file__)"`, and run tests
  with `PYTHONPATH=src` from `api/`, or you test someone else's code.
- Tests: `api/tests/test_resume.py::test_racing_cold_resumes_of_one_id_share_a_single_session`.
