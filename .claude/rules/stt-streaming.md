---
paths:
  - "api/src/api/stt/streaming.py"
  - "api/src/api/session.py"
  - "api/tests/test_streaming_stt.py"
  - "api/src/api/stt/parakeet.py"
  - "api/tests/test_stt_backends.py"
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
  - A final that raises is retried in place with backoff, not treated as empty (XERK-1499).
    During an outage no partial decodes, so the XERK-174 partial fallback is empty too and
    the turn was lost from the stored transcript.
  - The retry budget (`_FINAL_RETRY_BUDGET_S`) runs from the outage's first failure, not per
    turn, and any successful decode resets it. Per-turn budgets would hold the worker and
    the translation hold for budget x queued turns on a dead upstream.
  - Retry only an outage, never one bad input: real Parakeet 500s every time on 10-20 ms tails.
    Retrying that held every later turn for the budget on a healthy upstream, and a session
    ending meanwhile lost them all. After a turn's first failure a 1 s silence probe decides;
    no probe once an outage is known (a hung upstream would cost a timeout per turn).
  - A probe that answers earns the turn one immediate retry before the fallback: a final sent
    during a brief outage can fail after it cleared, and the probe can't tell that apart.
  - Tests: `test_streaming_stt.py::test_turns_whose_final_raises_during_an_outage_land_once_it_recovers`,
    `::test_final_retries_give_up_once_the_outage_outlasts_the_budget`,
    `::test_a_turn_the_upstream_rejects_is_not_retried_as_an_outage`,
    `::test_a_final_that_fails_as_a_brief_outage_ends_still_lands`. The autouse fixture
    swaps `_retry_sleep` for one that advances the fake clock.
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
- An engine slower than real time sheds load by merging the final backlog (XERK-1498).
  - Trigger: `behind_real_time` and the next final waited over `_FINAL_BEHIND_S`.
  - `behind_real_time`: over the last `_BEHIND_WINDOW` single-turn final decodes, leaving out
    stalls (over `_STALL_FACTOR` x the median), decode time summed exceeds audio summed.
    - Summed, not "N slow in a row": with a fixed request cost a long turn beats its own audio,
      and a streak reset on it switched the shed off entirely for mixed turn lengths (QA).
    - Stalls by the median, not "drop the slowest": on a steady engine jitter picks the
      slowest, and with mixed turn lengths that flipped the verdict and flapped the status (QA).
  - Never merge on wait time alone: an outage backlog drains turn by turn, keeping boundaries.
  - Merged decodes and finals that got no answer add no sample to the window.
  - An outage clears the window: pre-outage samples made a recovered fast engine read as
    behind and merge the outage backlog (QA).
  - Size a merge by the last final's decode rate to fit `_COALESCE_DECODE_BUDGET_S`, not by
    audio length alone: QA showed a 1.3x engine timing out (Parakeet's 15 s deadline) on a
    big merge and losing every merged turn. Peek before taking, so an over-limit turn stays queued.
  - Queued partials among the merged turns are dropped; `_speech_finals_pending` is decremented
    once per merged speech turn, in the same `finally`.
  - Tests: `test_streaming_stt.py::test_a_merge_is_sized_to_decode_within_the_request_timeout`,
    `::test_a_slow_engine_is_shed_with_turns_of_varying_length`,
    `::test_one_stalled_decode_on_a_healthy_engine_does_not_merge_turns`.
  - Timing tests use `VirtualEngine` (decodes wait on the fake clock in the loop). A threaded
    fake engine races a test that advances the clock, so measured latencies flake.
- `caption.status` tells the clients when captions are delayed (XERK-1498).
  - It is set by any final older than `_LATE_FINAL_S`. It clears only after `_CAUGHT_UP_S` of
    on-time finals and once the transcriber is no longer `behind_real_time`. Otherwise it
    flaps while merged bursts land.
  - It is sent on change only. Clients reset it on every `session.ready`, because a cold resume
    starts a new session that never sends `false`. A warm resume repeats a still-delayed status
    after its ready (`Session.send_caption_status`).
  - Tests: `test_resilience.py::test_client_is_told_when_captions_fall_behind_and_catch_up`,
    `test_resume.py::test_ws_warm_resume_repeats_a_delayed_caption_status_after_ready`.
- Tests: push through the `_push` / `_drain` helpers, which join the job queue. A bare
  `t.push` returns before any decode has run.
- Repro harness for regressions: real uvicorn (`ws_ping_interval=5`) plus a `websockets`
  client, with an engine that sleeps 15 s. Main closes every session with 1011 at about 10 s.
  `API_AUTH_SECRET` must be at least 32 characters.
- An engine's HTTP timeout must be a whole-request deadline, not httpx's `timeout=` alone: that
  is per phase and per read, so a trickling upstream holds the single decode worker forever.
  `ParakeetEngine._post` wraps the request in `asyncio.wait_for` (XERK-1448).
  - Tests: `test_stt_backends.py::test_parakeet_timeout_is_a_whole_request_deadline`.
- Don't run that engine via `asyncio.run`: it joins the default executor on exit, so a hung
  `getaddrinfo` thread outlasts the deadline. `loop.close()` shuts it down without waiting.
  - Tests: `test_stt_backends.py::test_parakeet_deadline_covers_a_hung_dns_lookup`.
