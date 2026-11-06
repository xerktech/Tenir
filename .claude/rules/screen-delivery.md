---
paths:
  - "api/src/api/screen/turma.py"
  - "api/tests/test_turma_delivery.py"
  - "api/tests/test_pg_screen_live.py"
---

# Screen meeting delivery to Turma (`turma.py`)

- Delivery to Turma (`turma.py`, XERK-2513) is at-least-once on purpose: exactly-once comes from
  the hub's idempotency on `meetingId`. Never mark `sent` before the hub's 200 `{ok: true}`, and
  never key the POST on anything but the conversation id, or a retry duplicates the meeting.
  - A 200 that isn't JSON `{ok: true}` (a proxy or login page) is retried, not `sent`.
  - 404 "no host is in that org" is retried: a restarted hub answers it until its hosts beat.
  - `pending_deliveries` is unbounded and a pass skips only meetings backing off: a LIMIT let
    one org's 20 stuck meetings (404/429) starve every other org for a day (XERK-2513 QA).
    Test: `test_one_orgs_stuck_backlog_never_starves_another_org`.
  - The body is fitted to the hub's 1 MiB / 10000-line caps (`meeting_body`): over them the hub
    413s, which is permanent, so a long meeting would never be sent.
  - Count lines as the hub's `meetingSegmentBytes` does (sanitized, `"speaker":""` on each), not
    by wire size: its per-org 1 MiB budget 429s a meeting over it forever (delta QA).
    Test: `test_a_big_meeting_fits_the_hubs_per_org_transcript_budget`.
  - Boot re-queue (XERK-2605) matches the start of `delivery_error` (`REQUEUE_ON_BOOT`): a
    change to a reason `_deliver`/`_hub_error` writes must keep those prefixes, or failed
    meetings silently stop being resent. Test:
    `test_the_written_failure_reasons_match_the_requeue_list`.
  - Tests: `test_a_hub_restart_mid_delivery_is_retried_with_backoff_and_lands_once`;
    SQL: `test_pg_screen_live.py::test_meeting_delivery_state_round_trips`.
