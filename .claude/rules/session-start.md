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
- A cold resume of a persisted id also reports `resumed=True`; only `warm` (true solely from the
  `rebind()` path) tells warm from cold. Clients drop held translation/song boxes on
  `warm: false`, since the old sitting's done markers died in its buffer (XERK-1736).
- A displaced socket may be dead but open (uvicorn notices only at its ping timeout), so frames
  written to it are lost. `rebind()` first replays finals/translations/cues sent in the
  `_RECENT_WINDOW_S` before the old socket was given up (its detach, else the takeover), then the
  detached buffer, then the live `song` re-anchored; `resend_ended_asides()` repeats done markers
  after the ready (XERK-1736, XERK-1771).
  - Replay before the buffer, and hold new frames in the buffer until caught up: clients append
    turns in arrival order, so a lost turn replayed later lands after newer ones (QA).
  - No delivery is ever confirmed, so the replay overlaps what the client has: every client
    (client-core reducer, Even controller) must drop a re-delivered final/translation/cue by id.
  - Never replay `translation.done`: it names no run, so a stale one ends a live run early.
  - Never replay a stored `song`/`song.sync` as-is: the offset is anchored on arrival.
- Worktree pitfall: the host's pip-installed `api` package may be another worktree's
  editable install. Check `python -c "import api; print(api.__file__)"`, and run tests
  with `PYTHONPATH=src` from `api/`, or you test someone else's code.
- Tests: `api/tests/test_resume.py::test_racing_cold_resumes_of_one_id_share_a_single_session`,
  `::test_warm_resume_closes_the_socket_it_takes_over`,
  `::test_a_start_in_flight_on_the_displaced_socket_leaves_the_session_alone`,
  `::test_a_cold_resume_in_flight_on_the_displaced_socket_leaves_the_session_alone`,
  `::test_the_displaced_socket_gets_its_4001_even_with_a_frame_queued`,
  `::test_a_foreign_id_start_does_not_hold_the_owner_off_its_resume`,
  `::test_warm_resume_replays_what_a_dead_socket_swallowed`,
  `::test_rebind_after_a_detach_replays_lost_frames_ahead_of_the_buffer`,
  `api/tests/test_resilience.py::test_start_cancelled_during_create_does_not_leave_the_row_live`.
