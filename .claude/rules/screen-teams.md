---
paths:
  - "api/src/api/screen/teams.py"
  - "api/src/api/screen/meetings.py"
  - "api/src/api/screen/ws.py"
  - "api/tests/test_screen_teams.py"
---

# Teams screen parsing (`teams.py`)

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
  - With no roster, the last row, name-shaped, where a header can sit (after a punctuated row or
    with avatar evidence, never alone) is a pending header (XERK-2794, `caption_blocks`, shared
    with Zoom): the next entry's header shows before its text, and read as text it was stored as
    speech for good. A mid-panel name row is a header anyway once a row follows; a finished
    caption tail gets Teams' "." and is text again.
    - It verifies no crop (`zoom.pending_header`): slide text over a name started meetings (QA).
      Requiring a header above it instead re-stored names once the header scrolled off.
    - Accepted: a name-shaped tail never punctuated ("Sounds Good", "Hi Hank Ives") is lost.
    - Tests: `test_without_a_roster_a_trailing_name_after_a_finished_line_is_a_pending_header`,
      `test_without_a_roster_or_pixels_a_header_shown_before_its_text_is_never_stored`,
      `test_without_a_roster_slide_text_over_a_trailing_name_starts_no_meeting`.
  - Mis-cased avatars ("So Sean O'Neil") strip only off a box starting left of the caption text
    right under it (`_strip_header_boxes`); "Hi Hank Ives" mid-caption starts where the text
    does. Not a median column: slide rows link into the panel and shift it. All initials, 2+.
  - Caption crops drop a box reading exactly "Go to current" (`_drop_pane_buttons`) before
    `row_groups`: level with a header it joined the row and lost the header (XERK-2792). Only
    unpunctuated and well right of the text column: a spoken "go to current." stays (QA).
    Tests: `test_the_go_to_current_button_is_not_caption_text`,
    `test_go_to_current_spoken_at_the_text_column_stays`.
  - A row glued onto the avatar sheds a 2-capital token one letter off the circle, compared
    position by position (first+last initial: "LA" for "TA", real OCR). A shared letter alone
    stripped "AI"/"IT" openers under Tom Adams (QA). Tests:
    `test_avatar_initials_misread_by_one_letter_are_stripped`,
    `test_a_two_capital_word_not_one_letter_off_the_circle_stays_text`.
  - A short token before a roster name on a box left of the text below it is a misread circle
    ("3 Lee, Bo", "BI Lee, Bo" at 1920/1680, real OCR) and is stripped (`_strip_misread_avatar`).
  - Caption crops read their pixels for avatars (`_pixel_avatars`, XERK-2614): a round run of
    ink rows left of the text column marks the topmost box beside it (its top half). When one
    is found, avatars alone decide headers the roster doesn't match (`caption_blocks(avatars=)`).
    A photo has no OCR box, so text rules merged its speaker into the last one. Tests:
    `test_a_photo_avatar_speaker_off_the_roster_gets_their_own_header`,
    `test_a_name_shaped_first_caption_line_stays_under_its_speaker`.
  - An avatar'd header the roster doesn't match names its speaker by its cleaned text
    (`caption_blocks`, XERK-2791): a meeting whose window isn't on screen fills no roster, and a
    real one stored 50 of 64 rows with no speaker. Measure with `caption_replay.py --rows`.
  - The tracker names such a speaker by the spelling most read this meeting
    (`MeetingTracker._as_heard`): the merger compares speakers exactly, so one frame misreading
    "Omar Patcl" stored the whole panel again (QA). A spelling under `_HEARD_OWN` frames, or read
    `_HEARD_SHARE`x less than a like one, is a misread of it; else a person of their own.
    - Keeping the first reading instead named a speaker by a misread first frame all meeting
      (QA): a spelling outreading a rarer like one takes its held lines (`CaptionMerger.rename`).
    - Accepted: a new person whose name reads like an off-roster speaker's has their first
      entry stored under that name as well (a misread can't be told from a newcomer yet).
      Roster names and "Speaker N" labels are never unified. Ties go to the first heard.
    - Crafted names cost difflib (QA: ~2 s a frame on the event loop). So likeness is computed
      once per new spelling (`_likes`, linked both ways), at most `_NEW_PER_FRAME` new a frame,
      never past `_LIKE_MAX_CHARS`, and `_as_heard` runs on `CPU_POOL` (worst 90-340 ms a frame
      while 200 names fill, by node load), only for frames with an off-roster speaker.
      Tests: `test_off_roster_names_are_compared_once_each`,
      `test_a_frame_brings_in_at_most_a_few_new_spellings`,
      `test_off_roster_names_are_unified_off_the_event_loop`.
    - Per meeting (cleared in `end`). Tests:
      `test_a_misread_on_a_speakers_first_frame_names_none_of_their_lines`,
      `test_a_long_running_speakers_name_misread_on_three_frames_is_stored_once`,
      `test_two_people_with_like_names_off_the_roster_stay_apart`.
  - Accepted: a name read off-roster then matched to a differently spelled tile once a full
    frame fills the roster can split one entry across two names (unmeasured on real frames).
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
- Teams' "Speaker N" rows are headers of their own speaker (`teams._SPEAKER_LABEL`), counted as
  avatar evidence, matched on a roster copy so they never become participants. Without it a
  real meeting recorded nothing (QA). Tests: `test_teams_speaker_labels_are_headers`,
  `test_a_speaker_label_heads_its_entry_even_where_pixels_show_no_avatar`.
