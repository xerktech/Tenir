---
paths:
  - "api/src/api/screen/**"
  - "api/tests/test_turma_delivery.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_screen_teams.py"
  - "api/tests/test_pg_screen_live.py"
  - "api/tests/fixtures/screen/**"
  - "api/src/api/history.py"
  - "api/tests/test_history_meetings.py"
  - "api/src/api/auth/deps.py"
  - "api/tests/test_device_tokens.py"
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
- A device token is live only while its `device_tokens` row (id = the token's `jti`, owned by the
  token's user) is unrevoked and unexpired (XERK-2594). `principal_from_live_token` checks it for
  every scoped token; a token without `jti` is refused (unrevocable), never let through.
  - Revoke closes open sockets via `screen.ws.revoke_token`; the handler re-checks the row after
    registering its revoker, as it does for account deletion.
  - A screen socket closes at its token's `exp` (`Principal.expires_at`, scoped tokens only,
    XERK-2612). Without it a mint purging the expired row left the socket unrevocable.
  - Tests: `test_device_tokens.py`; SQL `test_pg_users_live.py::test_device_tokens_round_trip_and_revoke`.
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
- A too-dense full-frame strip is halved and re-read in the api (`ocr.halves`, `MAX_SPLITS`),
  not by raising tenir-ocr's 16-box cap: that cap is the per-request time bound, and on caption
  crops `too_dense` still means "a slide, store nothing" (XERK-2610). Tests:
  `test_a_too_dense_full_frame_strip_is_halved_and_re_read`,
  `test_a_frame_of_slides_stops_splitting_within_the_budget`,
  `test_a_7x7_gallery_frame_is_halved_down_to_row_height_on_every_strip`.
  - `MIN_SPLIT_HEIGHT` (128) keeps 4K strips (125 px) whole: halving them doubled a 4K slide
    frame to 76 requests / 54 s, past the 30 s full-frame cadence (QA).
  - Never cap halving by wall clock: a 15 s cap lost a gallery row whenever OCR was slow (QA).
- `HttpOcrClient.read(full=True)` (ws passes the frame's region) splits full frames under the
  pixel cap too; a caption crop is never halved, or a slide crop would be stored as captions.
  Tests: `test_a_small_window_frame_is_split_but_a_caption_crop_never_is`; `replay` asserts
  the `full=` ws passed per region (`StubOcr.full`).
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
- Alias editing (XERK-2606) is not a small endpoint: ingest stores the *aliased* name in
  `segments.speaker`, so aliases must also be keyed by shown names, and those keys collide.
  A per-request "name taken" check missed raw names in other meetings and lost a race (QA on
  XERK-2507 merged two people for good three ways); it needs one model + an org-level lock.
- Teams (`teams.py`) reuses Zoom's roster, block grouping and merge; only layout differs.
  - Teams captions are capitalised sentences ("Sounds Good"), so `is_name_like` can't tell
    them from names: full frames exclude the caption panel by geometry (full width, it is
    left-aligned). Test: `test_a_capitalised_caption_line_in_the_panel_is_not_a_participant`.
  - Initials avatars are removed as OCR boxes before `row_groups`: a 2-row-tall circle joins
    the name row and the first text row into one. Test:
    `test_an_initials_avatar_spanning_two_rows_is_dropped`.
  - A Teams crop with no caption text returns no blocks: a header-only block would otherwise
    start a meeting from tile labels. Test: `test_a_crop_of_only_names_yields_nothing`.
  - `is_teams_name` rejects a trailing "." (bar an initial): short punctuated replies on a full
    frame otherwise joined the roster and then swallowed later caption rows as headers (QA).
    Test: `test_short_punctuated_replies_in_the_panel_are_not_participants`.
  - Full frames tell the caption panel from names by row spacing (`_caption_lines`): lower-half
    rows within one line height of each other (similar left edge) form a stack; a stack holding
    a caption-like line (>= 4 words, ending . ? !, or starting lower-case) is all caption text.
    Boxes around sentence lines failed five QA rounds (pane wiped, status lines, lower-case
    guests, edges leaking). Tests: `test_people_pane_names_level_with_the_captions_survive`,
    `test_a_panel_without_long_sentences_still_hides_its_captions`,
    `test_a_lower_case_guest_in_the_people_pane_keeps_the_other_names`.
  - Real Paddle boxes of stacked rows overlap by 1-4 px: rows link at gaps down to -line_h/2.
    Synthetic fixtures with clean positive gaps can't show it. Test:
    `test_overlapping_ocr_boxes_of_stacked_caption_rows_still_link`.
  - These full-frame geometry rules were tuned over five QA rounds on synthetic layouts only;
    each fix traded one synthetic case for another. Re-tune on real captures before adding more.
  - A trailing "." is still a name after an initial, a dotted degree or Jr./Sr. ("Martin King
    Jr." was lost to the previous speaker). Test: `test_teams_name_shape`.
  - Real OCR of rendered Teams windows (XERK-2609) is the regression set: tests under
    "real OCR of rendered Teams windows" in `test_screen_teams.py` hold Paddle's boxes.
    The QA kit (render + OCR pod + rescore) lived outside the repo; tenir-ocr's netpol admits
    only `tenir` pods, so drive it through `kubectl port-forward` (restart it per batch).
  - Not live, a crop needs a roster-matched header (any header with no roster): tile initials,
    misread labels and slides started meetings. Live (`parse_caption_crop(live=True)`, from
    ws.py) header-less crops pass: refusing them dropped long entries and ended the meeting
    mid-entry (QA). `MeetingTracker.on_caption` doesn't refresh `last_caption_ms` for a Teams
    header-less snapshot equal to the last one. Tests: `test_a_crop_with_no_speaker_header_yields_nothing`,
    `test_a_slide_heading_with_a_roster_starts_no_meeting`, `test_a_resent_header_less_crop_lets_a_teams_meeting_end`.
  - With a roster, a name row is an unverified header only after a punctuated row (or first) and
    before a non-name row (`caption_blocks(unknown_headers=True)`, Zoom off). Without the
    punctuation check, "Acme Global Partners" mid-caption was lost (QA, real OCR).
  - Mis-cased avatars ("So Sean O'Neil") strip only off a box left of the caption-text column
    (`_strip_header_boxes`): punctuation can't place headers (small OCR drops the final "."),
    and on the column "Hi Hank Ives" is caption. All initials, two letters at least.
  - A 2-letter title-case token ("So", "Al") is a tile circle only when >= 1.4x the median text
    height: circles measured 1.43x+, and "Al"/"Jo" are real display names (QA).
  - `_caption_lines`: a name row links at most half its OWN height: OCR can read avatar + caption
    as one tall box whose height let the tile label above link in.
  - Full frames: a 1280x720 or 960x540 gallery strip has over 16 OCR boxes, so tenir-ocr
    refuses it (`too_dense`) and the roster comes back empty; the parser can't fix that.
  - `clean_name` skips text over 100 chars: suffixes strip one per pass (quadratic, ~1 s/frame).
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
