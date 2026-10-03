---
paths:
  - "api/src/api/stt/streaming.py"
  - "api/src/api/session.py"
  - "api/tests/test_streaming_stt.py"
---

# Streaming STT decode worker (XERK-1424)

- `StreamingTranscriber.push()` must never await an engine call. It runs on the `/ws` receive
  loop. An awaited decode stops the socket being read, so keepalive pongs go unread and uvicorn
  closes every live session with 1011 when STT hangs or runs slower than real time.
- All decodes run in one ordered per-session worker (`_work`).
  - Order keeps a turn's partials ahead of its final.
  - Order also lets the worker own the per-turn state (`_turn_partial`, `_agreement`).
  - Never decode concurrently.
- Partials degrade, finals don't.
  - At most one partial is outstanding; a cadence that falls due meanwhile is skipped.
  - A partial that waited more than `_PARTIAL_STALE_S` in the queue is dropped.
  - Finals always queue.
- `finalizing` (XERK-1377 translation hold) counts speech finals that are queued or decoding,
  plus finals that are queued but not yet handled.
  - Decrement only in the worker's `finally`, after `_finalize` has counted its final as
    undelivered.
  - Otherwise the flag either dips false mid-turn or sticks true.
- `Session.close` bounds `flush()` with a flat 15 s `wait_for`, then always calls `close()`.
  - Do not replace it with "wait while decodes keep succeeding".
  - QA showed a slow or flaky engine then outlasts the pod's 30 s termination grace.
  - A SIGKILL before `_persist` loses the whole recording; the flat cap only drops tail turns.
- A final whose audio arrived more than `_STALE_FINAL_S` ago (`Session._final_age_s`) is stored
  only: no caption push, translation or cue (XERK-1447).
  - Finals queue through an outage and land as one burst on recovery; pushing them flooded the
    glasses' caption band with turns from tens of seconds earlier.
  - Age is dated by audio *arrival* time, not the session timeline, so tests that push audio
    faster than real time never read as stale.
- Tests: push through the `_push` / `_drain` helpers, which join the job queue. A bare
  `t.push` returns before any decode has run.
- Repro harness for regressions: real uvicorn (`ws_ping_interval=5`) plus a `websockets`
  client, with an engine that sleeps 15 s. Main closes every session with 1011 at about 10 s.
  `API_AUTH_SECRET` must be at least 32 characters.
