---
paths:
  - "api/src/api/screen/**"
  - "api/tests/test_turma_delivery.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_pg_screen_live.py"
  - "api/tests/fixtures/screen/**"
---

# Screen meeting captions (XERK-2505)

- Design and wire contract: `docs/meeting-captions.md` "Screen ingest in the api". Keep it,
  the schema defs and `contract/README.md` in step.
- `/ws/screen` messages stay outside the `/ws` `ClientMessage`/`ServerMessage` unions: adding
  them there widens the web/mobile switch types for a socket those clients never open.
- Device tokens (`scope='screen'`) must stay refused everywhere but `/ws/screen`:
  `principal_from_live_token` compares the scope exactly, and `renew_token_if_due` refuses a
  scoped token (it would otherwise slide forever). The test renews at 60% of the token's life;
  a shorter offset can't fail. Test:
  `test_device_token_is_minted_for_the_caller_and_scoped_to_the_screen_socket`.
- `/ws/screen` sockets aren't registry sessions: `delete_user` calls `screen.ws.revoke_user`.
  Test: `test_deleting_the_user_closes_their_open_screen_socket`.
- Merge = an ordered alignment of each snapshot against the recent known lines (`_align`),
  not per-line matching. Every per-line rule tried (time window, order floor, fragment filter)
  either dropped repeated replies or re-stored the whole box after one OCR glitch (four QA
  rounds). Lines above the aligned region that match nothing are glitches, never new.
  - An alignment must end on a match (or match nothing): ending on a skip let the newest
    unshown line dodge its penalty and swallowed a repeat ("yes", "thanks", "yes").
  - A glitch pairs with the known line it misreads ("misread" move, cheap). An unpaired line
    under a verified header above a match costs more than "new": it is a line said between
    two snapshots (dropped frames). Without that, [okay] then [Bo sure, okay] lost two lines.
  - Only the bottom line may change text; a line above it keeps its text (OCR jitter).
  - OCR text is attacker-controlled (device token): results, roster and snapshots are capped
    (`ocr.MAX_LINES`, `zoom._MAX_ROSTER`, `merge._MAX_SHOWN/_MAX_CHARS`) and parse + merge run
    in `to_thread`. On the loop, one crafted frame stalled every socket ~8 s.
  - A misread must look like its known line (same speaker, ratio >= 0.5): pairing any two
    lines "explained away" a reply said between snapshots.
  - Lines first seen in one snapshot get start_ms 1 ms apart: Postgres orders ties randomly.
  - An unpaired line under a verified header is never a glitch (it may sit between known
    lines: [Bo, Cy yes, Ana okay] after [Cy, Bo, Ana okay]); only header-less lines are.
  - Measured (`_simulate`, 1000 seeds x 25 lines): clean and 30%-dropped runs store 0
    duplicates and no made-up text (losses 2 / 152); OCR noise alone 4 duplicate runs. Noise
    and drops together duplicate in ~400 runs: re-tune on real XERK-2506 captures.
  - Tests: `test_simulated_rolling_captions_store_what_was_said`,
    `test_ocr_noise_never_duplicates_a_line` (seeded box simulation; run 1000 seeds, with
    and without `skip`, after tuning any score), `test_a_box_showing_a_repeat_stores_each_line_once`.
- schema.sql: a column added to a table this repo already created needs its own
  `ALTER ... ADD COLUMN IF NOT EXISTS`, even a table created in the same PR's earlier commit.
- Final lines wait in `MeetingTracker._unsaved` until written: the merger counts a line final
  once, so a failed write re-derived from frames would dedup away.
- Full frames are decoded only at the OCR'd size and ≤3840x2400 px: a 2.6 MB JPEG decoded to
  gigabytes. One socket feeds at most `MAX_WINDOWS` windows.
- tenir-ocr (ocr/README.md) refuses images over 0.5 Mpx and returns no width/height:
  `ocr.HttpOcrClient` reads the size from the JPEG header and cuts full frames into
  overlapping strips, dropping lines that touch a cut edge (a fragment like "Priya Ra" would
  otherwise become a participant). Change ocr/README.md in the same PR as the contract.
- `_active_tile` scores min(green left of the label, green below it), never one window
  around it: on a 3x3 gallery that window caught the tile above's / left's border and nothing
  won. Full-frame lines are ordered by row band then x (`_reading_order`): real OCR boxes on
  a row differ by a pixel. Tests: `test_the_active_tile_wins_on_a_tight_gallery`.
- Conversation ids are dashed uuid4 strings; history 404s any other shape (`is_valid_session_id`).
- `is_name_like` keys on capitalisation: caption text is lower-case sentences. Loosening it
  turns caption lines into participants on full frames.
- The fixture is synthetic OCR output (no real capture may be committed: public repo). Re-tune
  thresholds on real frames once XERK-2506 can record them.
- Replay tests wait on the `screen.frames_handled` counter: frames are processed off the
  socket loop, and latest-frame-wins drops frames sent faster than they are handled.
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
  - Tests: `test_a_hub_restart_mid_delivery_is_retried_with_backoff_and_lands_once`;
    SQL: `test_pg_screen_live.py::test_meeting_delivery_state_round_trips`.
