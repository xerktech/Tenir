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
- Grace-lapse, session.end, revoke and a cancelled close() caller all leave a teardown the
  registry can't see. Shutdown waits on `session.teardowns_in_flight()`.
- `close()` only awaits its shielded `_teardown` task (XERK-1460), so cancelling a `close()`
  call stops nothing. The deadline cancels the teardown tasks themselves.
- `_close_teardown()` retains audio *before* the model drains and finalizes in a `finally`. A
  teardown cancelled at the deadline still writes its WAV and marks the conversation ready.
- The first retain is shielded and awaited in the finally. The store write runs in a thread a
  cancel can't stop, and a racing second write stores the audio twice.
- `_persist_audio` trims the buffer right after `put`, before `set_audio_key`. Trimming later
  re-stores the audio behind its own stored copy whenever the key write fails.
- A resume landing mid-teardown (grace lapsed, so the old Session is unregistered) takes its
  offset from `_closing`, the closing sitting's in-memory timeline end (XERK-1500). Never make
  `start()` wait on the old teardown: that held session.ready 9-29 s, and a second reconnect in
  that window started a duplicate sitting with overlapping segments.
- A resumed sitting's audio retain is ordered behind the closing sitting's (via `_first_retain`
  and, when that failed, `_prior_teardown` in `_persist`). The stored WAV is append-only, so
  storing out of order desyncs History playback.
- The deadline cancel lands on whatever the teardown is awaiting, including an await in its
  `finally`. An await there that must finish catches that one `CancelledError`, completes,
  then re-raises (see `_persist`).
- uvicorn cancels a WS handler or HTTP request still running at `--timeout-graceful-shutdown` and
  logs anything it raises, a re-raised `CancelledError` included, as "Exception in ASGI
  application". `ShutdownCancelMiddleware` swallows that one cancel, matched by uvicorn's message
  (`_UVICORN_SHUTDOWN_CANCEL`, XERK-1530/1602), and answers an unstarted HTTP response 503.
  No lifespan flag can mark it: the lifespan shutdown runs only after the cancel.
- That middleware must stay pure ASGI and outermost (added last). The traceback only shows when
  the lifespan shutdown outlasts the cancel's unwinding — a bare local uvicorn run with an instant
  shutdown exits first and logs nothing, so a repro needs a slow lifespan shutdown.
- Tests: `api/tests/test_shutdown.py`; resume-during-teardown in
  `api/tests/test_session_persistence.py` (`test_resume_during_prior_teardown_*`,
  `test_shutdown_cancel_while_waiting_on_failed_prior_still_stores_audio`).
- Some mutants hang rather than fail, so give hang-shaped tests their own `wait_for` timeout.
