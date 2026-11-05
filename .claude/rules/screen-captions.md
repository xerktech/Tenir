---
paths:
  - "api/src/api/screen/**"
  - "api/tests/test_turma_delivery.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_screen_teams.py"
  - "api/tests/test_screen_teams_pane.py"
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
  - A stored line extends only on whole-word growth, so the ASR finishing its last word after
    it was stored ("deploy" -> "deployment failed") loses the rest of that line. Accepted
    (XERK-2597): three char-growth rules each traded it for stray OCR chars stored ("yesl",
    "timeout'" freezing the line). `_simulate` never appends chars; re-tune on real captures.
    Tests: `test_ocr_jitter_on_the_stored_bottom_line_is_not_stored`,
    `test_ocr_jitter_on_a_stored_line_above_the_bottom_is_not_stored`.
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
- A tracker carries its ended meeting's last lines into the next merger (`carried`, XERK-2615):
  a panel left on screen past the end timer re-read them as a second conversation. No meeting
  starts until a crop holds a line the carry doesn't explain.
  - `ScreenMeetings` keeps the carry when the sweep drops a detached tracker (`_carries`,
    capped): a client reconnecting over the break otherwise got a fresh tracker (QA).
    A process restart still loses it.
  - Carried lines never grow nor extend (their segments are the ended conversation's): a grown
    carried line aligned onto it, was refused, and the continuation was lost (QA).
  - Accepted: a new meeting whose opening line closely repeats one of the last carried lines
    (any speaker before a roster) is stored from its next line.
  - Tests: `test_a_panel_re_sent_after_its_meeting_ended_starts_no_new_meeting`,
    `test_a_reconnect_over_the_break_still_starts_no_new_meeting`,
    `test_the_last_speaker_going_on_after_the_break_is_a_new_line`.
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
- Zoom crop: an off-roster header below the top row is decided by pixels (`zoom._dim_rows`):
  the panel draws names dimmer than text. Only without readable pixels does the old "header
  only under a row ending . ? !" rule apply; it stored names after unpunctuated entries as
  speech (XERK-2644). Row 0 stays position-only: the client starts the crop at the name line.
  - A dim name-shaped row (`name_rows`) is a header whatever follows: the last row, a
    name-shaped first line ("Sounds Good"), a one-word name. Each was stored as speech (QA).
  - Tests: `test_an_off_roster_name_under_an_unpunctuated_entry_is_a_header_not_speech`,
    `test_an_off_roster_name_on_the_crop_s_last_row_is_never_stored_as_speech`.
- Not live, a Zoom crop needs a header (XERK-2693): a roster match, or off the roster a dim
  name row in its pixels (any header without pixels). Header-less dark docs pages started two
  false meetings. Live, header-less crops still pass (an entry whose name scrolled off).
  - A dim row of any display-name shape (`_is_display_name`: "jsmith", "Raman, Priya") is a
    header, not speech. Only one that reads as a capitalised name (`_name_form`) vouches for a
    start: any-shape vouching let docs pages' grey meta lines start 55/63 probes (QA).
  - Open (XERK-2700): lower-case-only speakers start nothing; a grey Title Case docs line, or
    all-Title-Case rows with no roster, still start one; legacy off-roster `Name: text` doesn't.
    Telling these apart (avatar?) needs real Zoom crops.
  - Toolbar rows are dropped before parsing (`_is_toolbar_row`): Title Case labels need >= 2
    and 3/4 of the row, so speech like "Share more of it" stays.
  - Not done: dropping wide/dense share text bleeding into the crop; needs real crops to tune.
  - Tests: `test_a_dark_docs_page_in_the_caption_band_starts_no_meeting`,
    `test_zooms_toolbar_rows_in_the_caption_crop_are_not_stored`,
    `test_an_off_roster_speaker_s_dim_name_still_starts_a_meeting`,
    `test_a_dim_last_first_or_guest_name_starts_a_meeting_and_is_not_speech`,
    `test_a_grey_meta_line_on_a_docs_page_starts_no_meeting`.
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
  - With a roster, a name row before a non-name row is an unverified header only where one can
    sit (`caption_blocks(unknown_headers=<row predicate>)`, Zoom passes none): first row, after a
    punctuated row, or carrying avatar evidence (initials in its box or a circle beside it).
    Punctuation alone lost "Acme Global Partners" mid-caption when absent, and when present
    merged a new speaker after an unfinished line into the last one (both QA, real OCR).
  - Mis-cased avatars ("So Sean O'Neil") strip only off a box starting left of the caption text
    right under it (`_strip_header_boxes`); "Hi Hank Ives" mid-caption starts where the text
    does. Not a median column: slide rows link into the panel and shift it. All initials, 2+.
  - A short token before a roster name on a box left of the text below it is a misread circle
    ("3 Lee, Bo", "BI Lee, Bo" at 1920/1680, real OCR) and is stripped (`_strip_misread_avatar`).
  - Caption crops read their pixels for avatars (`_pixel_avatars`, XERK-2614): a round run of
    ink rows left of the text column marks the topmost box beside it (its top half). When one
    is found, avatars alone decide headers the roster doesn't match (`caption_blocks(avatars=)`).
    A photo has no OCR box, so text rules merged its speaker into the last one. Tests:
    `test_a_photo_avatar_speaker_off_the_roster_gets_their_own_header`,
    `test_a_name_shaped_first_caption_line_stays_under_its_speaker`.
  - No avatar found (faint photo, odd layout) keeps the position rule. An avatar-only rule
    there lost 11/96 real-OCR cases with no roster (QA). Test:
    `test_with_no_avatar_found_a_roster_keeps_the_position_rule`.
  - Real OCR reads name boxes from the avatar's initials ("DA Diego Alvarez" at x 13): the text
    column is the median start of caption-text rows only, and a box counts as beside an avatar
    by its right edge. Test: `test_a_name_box_that_swallowed_its_initials_keeps_the_other_avatars`.
  - Background = the most common colour (`_mode_colour`) of the gap between avatar and text on
    caption-text rows: the crop's median was a tile's grey (light panel all ink), and the gap's
    median drifted onto an avatar edge (1920 hole_ok_only, real OCR). Test:
    `test_avatars_are_found_when_the_gap_sample_catches_other_colours`. Round = ink *count* per line, not
    span (JPEG speckles), and the column ends 0.2 rows from the text (nearer clipped circles).
  - Read-in initials need an avatar-sized lit run (0.8-4 rows) starting by the box's middle and
    reaching 0.25 rows under it: a slide page in the column lit it top to bottom and vouched
    for every heading (QA, 1920). Test:
    `test_a_slide_page_filling_the_avatar_column_vouches_for_no_heading`.
  - Score any threshold change on the real-OCR corpora, roster own AND empty: with the QA kit's
    `rescore2.py` (96-case matrix) and `score4.py`; one fix measured fine on own, -11 on empty.
  - Mid-meeting, a Teams crop with no header the roster or an avatar vouches for
    (`CaptionBlock.avatar`) must re-read a recent line (`CaptionMerger.knows_any`) or it is
    dropped (a slide, its headings unknown headers). Requiring a roster match alone dropped
    every off-roster speaker's lines (QA). Lines said while every frame of them was dropped are
    lost. Test: `test_a_crop_sharing_no_recent_line_and_no_roster_header_is_dropped_mid_meeting`.
  - Initials read into a name box ("DA Diego Alvarez", either strip route) are avatar evidence
    only with the avatar column lit just under the box (`_ink_below`; no pixels: the box starts
    left of the text column). "AI Artificial Intelligence" over indented bullets passed a
    position gate and was stored as speech (QA x2). Tests:
    `test_initials_read_into_a_name_box_are_avatar_evidence_when_its_circle_is_missed`,
    `test_an_initials_heading_over_indented_slide_text_is_no_avatar`.
  - A crop is verified by a header the roster matches or one with an avatar (`b.avatar`);
    with no roster and pixels showing no avatar, it starts nothing. Lines said then are lost
    if they scroll off before a full frame or an avatar is seen (QA demonstrated; accepted).
  - Test the avatar flag through the parser, not hand-built blocks: a mutant dropping it at
    the parser passed every tracker test. Test:
    `test_a_photo_speaker_off_the_roster_alone_in_the_panel_is_stored`.
  - Tuned on synthetic renders only (dark, light, pastel, dark photo); a photo within ~18
    levels of the panel is missed. `test_ws_passes_the_crop_image_to_the_teams_parser` guards
    the jpeg reaching the parser. Re-tune on XERK-2506 captures.
  - A 2-letter title-case token ("So", "Al") is a tile circle only when >= 1.4x the median text
    height: circles measured 1.43x+, and "Al"/"Jo" are real display names (QA).
  - `_caption_lines`: a name row links at most half its OWN height: OCR can read avatar + caption
    as one tall box whose height let the tile label above link in.
  - With pixel avatars found, a roster-matched row with no avatar is caption text at ANY
    position (XERK-2604). A rule also gated on position flipped back to a header once the
    header above scrolled off, and the entry was stored twice (QA). A missed avatar merges
    that speaker into the previous one, and when a later frame finds it his lines are stored
    again under him (flaky detection: one copy each): accepted by the operator.
  - A crop showing no avatar keeps the position rule, so a wrapped name row atop it is read as a
    header and the entry can be stored twice. Accepted: real Teams (one 150% capture, real OCR)
    draws an avatar beside every entry, even one speaker's consecutive ones, so this needs one
    entry filling the panel. Four QA rounds of merge-side "un-read the top header" rules each
    traded a duplicate for a misattributed real speaker; don't retry without real scroll captures.
  - Real-capture check (XERK-2604): Teams name rows are a smaller bold font, but Paddle boxes
    overlap (names 18-25 px, text 22-31 px at 150%): box height can't tell them apart.
  - Full frames: only rows just under an initials-avatar'd header (`_under_avatar_headers`,
    within 1.3 header heights) are caption, never the whole stack: a compact People pane
    links a photo-avatar name under an initials one (QA). Real OCR at 100% glues the initials
    onto the caption row ("KS Okay"). Test: `test_real_ocr_of_a_one_word_interim_is_not_a_participant`.
  - Avatar initials OCR glued onto an entry's first caption row ("MH Yeah", "P( No", "M I")
    are stripped (`_strip_glued_initials`, XERK-2695) only when the row's box starts on the
    avatar (left of the name line and text column): a caption opening "I"/"OK" at the text
    column is the speaker's words. Tests: `test_avatar_initials_glued_onto_the_first_caption_row_are_stripped`,
    `test_a_first_caption_word_at_the_text_column_is_kept_even_as_initials`.
  - `clean_name` skips text over 100 chars: suffixes strip one per pass (quadratic, ~1 s/frame).
- Teams' caption pane is located on full frames by the api (`ws.locate_pane`, XERK-2608): pixels
  first (`teams_pane.candidates`: a divider of luma 40-170 with black above, black margins
  below), then OCR of only the strip above each divider for "Live Captions" (`pane_of`).
  - Never find the heading in the full frame's OCR: over a text-dense desktop every strip is
    too_dense and the heading is never read (QA, real capture: 25 of 25 strips).
  - Pane rows: the left OR right margin strip >= 0.6 black. Whole-row darkness (0.5) had no
    headroom on real panes (0.36 at JPEG q60) and shrank the box onto stale entries (QA).
  - A downscaled window splits the divider over two rows (25 + 54, 17 + 41): a top row >= 15
    plus the row under it is the line, with no cap on the sum (an avatar under it bleeds in).
    An avatar under a 1 px line lifts it to 143, hence the 170 cap (QA, real 0.79x). Each
    threshold here lost a real capture at some scale when tightened: re-sweep all captures at
    1.0/0.9/0.79/0.7/0.6 x q80/q60 after any change.
  - Near rows of one divider (JPEG ringing above it) are one candidate, the wider run kept.
  - The heading strip is 0.15 of the pane's width tall (48-120 px): 0.3 took in the toolbar
    and came back too_dense at 0.6x. Candidates whose strip is >= 0.45 black with >= 20 glyph
    pixels in its left half are OCR'd first, then the blackest: wider dark windows under grey
    lines pushed the pane out. Four such windows with heading-like text over black still can.
  - The pane is located alongside the frame's own OCR, not after it: that read took up to
    43 s of the 45 s timeout (QA), and a failed one still tells the client its region.
  - `cropHeight` is 6 heading heights (~7 caption lines); pane lines from full-frame OCR may
    be missing, and a heading-height pitch overshot to 13 of 16 boxes (QA).
  - `tell_pane` sends `screen.region` on change only, per socket, records it after the send,
    and an OCR error keeps the client's region. Lines inside the pane are never participants.
  - The bottom caption panel has no heading or divider: unsupported, captions dock right.
  - Tests: `test_screen_teams_pane.py`.
- Teams' "Speaker N" rows are headers of their own speaker (`teams._SPEAKER_LABEL`), counted as
  avatar evidence, matched on a roster copy so they never become participants. Without it a
  real meeting recorded nothing (QA). Tests: `test_teams_speaker_labels_are_headers`,
  `test_a_speaker_label_heads_its_entry_even_where_pixels_show_no_avatar`.
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
- Roster: a name that starts another and reads alike (>= `_NAME_MATCH`) is one person, kept
  under the longer reading (`zoom._same_name`, XERK-2695): a truncated tile label stored "Tim
  Mahl" and "Tim Mahler" as two speakers. The tracker relabels the merger's held lines on a
  rename (`CaptionMerger.rename`) or the box re-stored them; lines already written keep the cut
  name. "Priya Ra" (0.84) and "Speaker 1"/"Speaker 12" (digit end) are never merged.
  Tests: `test_a_cut_off_name_is_the_same_person_as_its_full_reading`,
  `test_a_cut_off_name_replaced_mid_meeting_stores_no_line_twice`.
- Zoom tile names: a name-shaped line is a tile label only if a near-black row (the tile gap)
  runs within one label height below it (`zoom._over_tile_gap`, XERK-2643). A share's text sits
  on its content: a real Jira share put sidebar items and ticket keys in the roster.
  - Measured on the real capture: gap luma ~5, light share 255. A dark-themed share still
    passes (accepted). Without decodable pixels every line is kept, as before.
  - Filter before deduping names: a share showing an assignee's name above their tile
    otherwise claimed the name and the real tile was dropped (QA).
  - A label inset more than one label height above the gap fails the check; all real labels
    measured 3-5 px above it. Re-measure on a real gallery / speaker-view capture.
  - Tests: `test_a_screen_shares_text_is_not_taken_for_participants`,
    `test_a_dark_screen_share_still_passes_as_tiles`,
    `test_a_label_with_no_pixels_under_it_is_kept`.
- A too-dense caption crop is split only on Teams, and only once (`read(split_once=)`, XERK-2652):
  a real Teams pane crop (11-12 lines + avatars) passes tenir-ocr's 16 boxes. On Zoom a dense
  crop is a slide, so never split it. Test:
  `test_a_dense_teams_caption_crop_is_halved_once_and_both_halves_read`.
- The halves read a line in their overlap differently ("CO Okafor, Chidi" / "Okafor, Chidi"),
  so exact-text `_dedupe` kept both and a doubled header misattributed lines (real Paddle, QA).
  `_drop_rereads` dedupes by box overlap.
  Test: `test_a_line_both_halves_read_differently_is_kept_once`.
- Meeting titles come from the transcript (`topic.py`, at delivery), never the window's top band:
  on a Horizon window that band is the remote-desktop toolbar. Don't reintroduce `frame.title`
  storage. Tests: `test_screen_topic.py`, `test_an_untitled_meeting_is_titled_by_topic_once_before_delivery`.
