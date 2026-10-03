---
paths:
  - api/src/api/main.py
  - api/src/api/session.py
  - api/tests/test_shutdown.py
---

# Pod shutdown and Session.close (XERK-1458)

- Prod SIGKILLs at `terminationGracePeriodSeconds=30`. One close against a hung model can take
  ~30 s (15 s STT flush + 15 s translation drain), so shutdown closes concurrently under one
  deadline (`_SHUTDOWN_DEADLINE_S`) in `close_all_sessions`. Never go back to a sequential loop.
- uvicorn waits for in-flight WS handlers and HTTP requests *before* the lifespan shutdown runs,
  with no timeout. Closes driven from a handler (session.end, revoke) are not bounded by
  that deadline.
- Grace-lapse, session.end and revoke unregister the session before closing it, so shutdown
  can't find those closes in the registry. It waits on `session.closes_in_flight()`.
- `Session.close()` retains audio *before* the model drains, and finalizes in a `finally`, so a
  close cancelled at the deadline still writes its WAV and marks the conversation ready.
- The first retain is shielded and awaited in the finally. The store write runs in a thread a
  cancel can't stop, and a racing second write stores the audio twice.
- `_persist_audio` trims the buffer right after `put`, before `set_audio_key`. Trimming later
  re-stores the audio behind its own stored copy whenever the key write fails.
- Tests: `api/tests/test_shutdown.py`. Some mutants hang rather than fail, so give hang-shaped
  tests their own `wait_for` timeout.
