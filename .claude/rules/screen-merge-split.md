---
paths:
  - "api/src/api/screen/merge.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_screen_teams.py"
---

# Screen caption merge: a live line grown while OCR missed its row (`_split_grown`)

- Split from `screen-merge.md` (size cap); its invariants still apply here.
- OCR missing a row for a frame or more while the next entry appears grows the live line
  into the next entry ("yes." -> "yes I think so too."). When both rows show again, the row
  reading the line's leading words is paired with it and the line reverted to that text; the
  row below stays new (`_split_grown`, XERK-2796). Else the next entry was stored twice.
  - Only to a text the line held for `_HELD_MS` (`_Line.held`, noted where the live line
    changes in `_update` and where `_apply_grows`/rewrites land in `_revise`): a real repeat
    whose upper copy is OCR-clipped for a frame reads alike, and was cut for good (QA). A live
    entry holds each word-by-word prefix one frame.
  - Accepted (operator's call): a repeat clipped to a text the first copy paused on 2 s+ (or
    any text at 2 s+ frame gaps) is still cut ("Thank you so much." -> "Thank you"); on
    screen it reads exactly like the miss. Only lines changed within `_REVISE_MS`.
  - The row below may read an unfinished grown line (`_unfinished`: its base end char, past
    combining marks, is Unicode L/N; an emoji's VS16/keycap ends it finished. A mark list missed
    "。", "…", '."'; `isalnum` missed Hindi "है"; L/N/M cut "❤️" lines — all QA)
    re-punctuated or
    clipped (its words a prefix of the line's), holding more than the row it splits off
    (XERK-2804). A finished line must read exactly: clipped over her next entry's first
    frame (its words, unpunctuated) it reads alike, and was cut for good (QA fuzz seed 146).
    - Never the line grown on (the next entry still growing): a finished entry clipped on the
      frame her next entry, extending it, first shows was cut for good (QA). Accepted: a next
      entry still growing when the row returns is stored twice, as main; so is a live line
      ending on a comma, dash or emoji (read as finished). A finished line with no closing
      mark reads as live and may be cut, as main cuts it when read exactly (QA).
    - With no row aligned, the line is the one known line the whole split test passes for:
      matching on the row below alone, an older "Yes." matched too and nothing split (QA).
  - A next entry wrapped onto 2+ rows: its later rows pair with the lines after the grown one
    when they read them exactly (XERK-2804); left new, its second row was stored twice. Still
    stored twice (as main): that second row growing or re-wrapped as the row returns.
  - `_hold` counts from a `_reflow` too (`_Line.reflowed_ms`): else a rewrite within `_HELD_MS`
    of it noted the re-flowed text as held. Never move a stored line's `changed_ms` there: it
    reopens `_EXTEND_MS` and a one-frame stray glyph 30 s+ on was stored (QA).
  - Tests: `test_a_new_entry_read_while_ocr_missed_the_row_above_is_stored_once`,
    `test_a_finished_entry_grown_while_ocr_missed_the_row_above_is_stored_once`,
    `test_a_repeated_entry_ocr_clipped_on_a_frame_keeps_both_copies`,
    `test_a_paused_entry_repeated_long_after_keeps_both_copies`,
    `test_another_speakers_entry_read_while_ocr_missed_the_row_above_is_stored_once`,
    `test_the_row_below_a_missed_row_need_not_read_the_grown_line_exactly`,
    `test_a_two_row_entry_read_while_ocr_missed_the_row_above_is_stored_once`,
    `test_a_reflowed_stored_line_s_text_is_held_only_from_the_reflow`,
    `test_an_older_one_word_line_does_not_stop_the_split`,
    `test_a_one_frame_glyph_long_after_a_late_reflow_is_never_stored`,
    `test_a_finished_entry_clipped_as_her_next_entry_shows_is_kept_whole`,
    `test_a_finished_entry_clipped_over_her_next_entrys_first_frame_is_kept_whole`,
    `test_another_speakers_row_over_the_grown_entry_never_splits_it`,
    `test_a_live_line_ending_on_any_letter_digit_or_mark_is_unfinished`.
