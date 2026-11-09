---
paths:
  - "api/src/api/screen/zoom.py"
  - "api/src/api/screen/meetings.py"
  - "api/tests/test_screen.py"
  - "desktop/tenir_capture/caption_box.py"
  - "desktop/tests/test_caption_panel.py"
---

# Zoom screen parsing (`zoom.py`)

- `_active_tile` scores min(green left of the label, green below it), never one window
  around it: on a 3x3 gallery that window caught the tile above's / left's border and nothing
  won. Full-frame lines are ordered by row band then x (`_reading_order`): real OCR boxes on
  a row differ by a pixel. Tests: `test_the_active_tile_wins_on_a_tight_gallery`.
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
    header, not speech.
  - Off the roster, with pixels and a text row, a dim row vouches for a start when it reads as
    a capitalised name (`_name_form`) or has Zoom's avatar circle left of it (`_has_avatar`,
    XERK-2700: "jsmith" meetings never started). Any-shape dim rows let docs grey meta lines
    start 55/63 probes (QA). Without pixels or a text row, the position rule's header stands.
  - The avatar only adds, never removes (operator's call): requiring it stopped meetings of
    off-roster speakers whose avatar was cut off, navy/maroon/dark grey, square or absent (QA).
  - Real Zoom crops (XERK-2705, sampler): the avatar is a ~26 px photo circle astride the
    name and its first text row, centred 2-6 px below the name box. "Starts level with the
    name" (the fixture) found it on 0/22 real rows. The window ends `_AVATAR_BELOW` of the
    text row's height past the rows' gap, not in name heights: OCR boxes x-height names
    ("vera cruz") shorter and lower while the avatar stays put, and missed 2/7 (QA). Without a
    row below, `_AVATAR_CENTRE` name heights. `top < y1` stops most next-row circles first.
  - Measured there: name ink / text ink 0.55-0.60, also at 0.5x q80 and 0.75x q30 through real
    tenir-ocr, so `_NAME_DIM` 0.8 has margin. Starts lost on heavy blur are OCR misreads of
    the name row, not the threshold. One speaker, one dark-theme meeting: re-measure on others.
  - That meeting showed only the panel, never the legacy one-line `Name: text` overlay, so
    off-roster overlay lines still start nothing (accepting any `Word:` would start docs pages).
  - Still open: a grey Title Case docs line ("Last Updated") or an all-Title-Case page with no
    roster starts a meeting; so does a filled disc / emoji beside a grey docs line.
  - The avatar is only visible because the client keeps `AVATAR_ROOM` px left of the box
    (`desktop/tenir_capture/caption_box.py`): cut at the glyphs, avatars darker than
    `BOX_WHITE` (Zoom's blue/green) never reached the api (QA).
  - Tests: `test_a_lower_case_speaker_beside_an_avatar_starts_a_meeting`,
    `test_with_no_avatar_seen_the_capitalised_name_rule_stands`,
    `test_with_no_text_row_to_compare_the_position_header_still_starts`,
    `test_the_avatar_needs_a_round_patch_level_with_the_name`,
    `test_a_bright_name_shaped_row_beside_an_avatar_vouches_for_nothing`,
    `test_a_circle_starting_below_the_name_s_middle_is_not_its_avatar`,
    `test_zooms_real_avatar_astride_the_name_and_its_text_starts_a_meeting`,
    `desktop/tests/test_caption_panel.py::test_the_crop_keeps_a_dark_avatar_left_of_the_name`.
  - Toolbar rows are dropped before parsing (`_is_toolbar_row`): Title Case labels need >= 2
    and 3/4 of the row, so speech like "Share more of it" stays.
  - Not done: dropping wide/dense share text bleeding into the crop; needs real crops to tune.
  - Tests: `test_a_dark_docs_page_in_the_caption_band_starts_no_meeting`,
    `test_zooms_toolbar_rows_in_the_caption_crop_are_not_stored`,
    `test_an_off_roster_speaker_s_dim_name_still_starts_a_meeting`,
    `test_a_dim_last_first_or_guest_name_starts_a_meeting_and_is_not_speech`,
    `test_a_grey_meta_line_on_a_docs_page_starts_no_meeting`.
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
