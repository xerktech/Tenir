---
paths:
  - "api/src/api/screen/merge.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_screen_teams.py"
---

# Screen caption merge: growth of stored Teams entries (`_grow`, `_apply_grows`)

- Split from `screen-merge.md` (size cap); its invariants still apply here.
- Teams adds words to a stored entry above the bottom (finishing it after the next began, or
  rewording it; XERK-2721). Rows there keep their text and aligned as misreads, so it was lost.
  `CaptionMerger._grow` takes a recent (`_REVISE_MS`) stored line's read that keeps all its
  words in order and adds whole ones (`_inserted`), once two frames in a row agree.
  - A run of added words with no 2+ char word ("x", "l") is jitter: static pixels repeat a
    misread on every frame, so two frames agreeing isn't enough (QA). Doubled words and "a"
    inside a longer run are kept: refusing them lost a second growth main stored (QA).
    Accepted: a word OCR doubles on every frame ("the the") is stored.
  - A thin stroke read twice ("ll", "ii") is a stray glyph pair too (`_spoken`, XERK-2728).
    Never widen it to vowel-less pairs: "PM", "Dr", "vs", "mm" are speech (QA). Test:
    `test_a_two_letter_word_inserted_mid_entry_is_stored`.
  - Applied after `_reflow` (`_apply_grows`), and only while the row's block's other rows and
    the speaker's rows right above/below read as their lines: else it is a re-flow or rewrite
    (within a block or across her entries), and taking the row alone stored the moved words
    twice (QA). Checking only for the moved words missed a word inserted beside them.
  - A neighbour counts as changed only when it lost a word the row adds (XERK-2728): a word
    re-flows only off a row that lost it. Counting any change refused real growth for good
    when both entries grew in one frame, or the neighbour was jittered on one frame, misread
    on every frame ("on fridav") or re-punctuated. Test:
    `test_a_neighbour_changing_without_losing_the_added_words_does_not_refuse_a_growth`.
    - An adjacent neighbour whose edge word facing the row changed counts too, but only when
      the row gained a word on that side: a misaligned or reworded row never shows the moved
      word itself (`_simulate_teams` seed 61). Tests:
      `test_a_reworded_word_moving_across_the_break_between_entries_is_not_growth`,
      `test_a_word_moved_within_an_entry_is_not_growth` (the multiset check's only pin).
      - Never on the facing edge change alone: one jittered frame of it ("tben") refused a
        real insertion for good and lost words in all three fuzzers (QA). Accepted: a word
        reworded as it moves and landing mid-row ("and tomorrow test it") is taken. Test:
        `test_one_jittered_frame_of_a_neighbours_facing_word_does_not_refuse_a_growth`.
    - A gain at the row's start is refused when the entry's row above is off screen (the
      crop's header-less top row) or, in the same block, reads as no known line (rewritten):
      the neighbour check can't see the loss, and the moved words were stored twice (QA).
      Accepted: a real word Teams adds at the crop top row's start is lost. A mid-row
      insertion there is kept. Tests:
      `test_words_moved_down_from_a_row_off_the_crop_are_not_growth`,
      `test_a_word_moved_off_a_row_rewritten_past_recognition_is_not_growth` (fz seed 103),
      `test_a_word_inserted_mid_row_on_the_crops_top_row_is_stored`.
    - Not a block's top row whose line wasn't its entry's first (rows merged into it): that
      refusal left the row unpaired and re-stored the box (QA fz seed 176). Accepted: such a
      merge with steady neighbours is taken as growth, its first row's words stored twice, as
      on main (XERK-2734: needs a rewrite claiming both lines). QA's hand repro is still
      refused (`test_an_entry_merging_its_rows_into_its_last_is_not_growth`); fz seed 190 isn't.
    - Measured per seed vs main (c6669c5): Zoom `_simulate` (1000 seeds, 4 modes) and fz.py
      zoom identical, kit zf.py one seed better; `_simulate_teams` (200) lost 143 -> 104,
      stored twice 139 -> 139; `noise=0.2` lost 175 -> 130, twice 1350 -> 1340. Kit x2701
      fz.py teams (200 x 12): extra falls or holds on every mode, lost never rises; one
      seed worse (190, the XERK-2734 merge, +6). Kit x2716 tf.py: lost falls on every mode; ~18/2400 seeds score a
      word or two more "extra", each traced to a real growth main lost that its word
      alignment misplaces (seeds 89, 124, 51), or a noise-garbled row main's block re-flow
      happened to rewrite a frame later (late/0.2/8 seed 75). fz "extra" up is real
      duplication; tf "extra" up was mostly the scorer (QA).
  - A read refused so is refused for good (`_Line.grow_refused`), from its first frame: only
    waiting let it land once the changed entry scrolled off (QA). Accepted: if that entry is
    OCR-missed on exactly the frames the word moves, nothing shows the re-flow.
  - A row with no known line (the live entry's new bottom row, a glitch) counts as unchanged:
    counting it refused real insertions for good (QA: Teams-sim words lost 143 -> 185).
  - A read dropping words is taken only beside an added word (`_edited`, XERK-2732): Teams
    rewording ("build on friday" -> "new build friday"). Two frames agree, as for insertions.
    - A read that only drops words is never taken: a deterministic OCR miss repeats on every
      frame, so no frame count tells it from Teams (QA: a 6-frame hold stored 6-30-frame misses,
      "we do not ship it" -> "we do ship it"). Accepted: Teams dropping a word alone keeps
      it (gaining a word on one frame and dropping another later keeps the dropped one).
    - It must keep half the words and the row's first and last (the crop clips a row's ends
      alike on every frame), and add nothing resembling a dropped word ("should" -> "sh uld"),
      by span or word by word (a garble beside a miss folds into one span: "ship the" -> "th").
      Accepted: a reword inflecting a word it keeps ("to test it" -> "tested it") keeps the draft.
    - Insertion-only reads go through `_inserted` first (greedy subsequence): difflib alone
      misaligned repeated words and refused insertions main took (QA).
    - Measured per seed vs daf7ffb, words extra/lost: Zoom `_simulate` identical;
      `_simulate_teams` 140/143 -> 140/132 (noise 0.2: 1355/176 -> 1328/168), no seed worse.
      QA's drop fuzz 735/423 -> 634/321; with 6-20-frame OCR misses 866/577 -> 781/494.
      Holding drops 3 or 6 frames lost more words than 2 on every fuzz. Re-tune on real
      captures (`samples.py`).
    - Tests: `test_a_rewrite_inserting_and_dropping_a_word_is_stored`,
      `test_a_one_frame_rewrite_dropping_a_word_is_never_stored`,
      `test_a_word_dropped_alone_is_never_taken`,
      `test_a_lasting_misread_dropping_words_keeps_the_stored_entry`.
  - A matched stored line growing by whole words within `_EXTEND_MS` extends on one frame
    (any line), unless the added words are the leading words the row below just lost, or are
    all 1-char on a line the speaker has said on from (`_moved_up`, XERK-2701): those were
    stored for good ("today" twice, "notes l"). Her newest line takes "I"/"5" at once:
    `_inserted` refuses 1-char growth on every frame, so refusing it there lost it (QA).
    - Not "only the speaker's newest line": Zoom with dropped frames completes a line on the
      one frame it shows it after her next line appeared; 26/1000 noise+drop seeds lost words.
    - Tests: `test_one_frame_of_a_stray_glyph_on_a_matched_stored_entry_is_never_stored`,
      `test_a_word_re_flowing_up_from_the_next_row_is_not_stored_twice`,
      `test_a_speakers_newest_line_still_takes_a_one_char_last_word`,
      `test_growth_matching_another_speakers_unchanged_row_below_is_taken`.
  - A growth whose leading added words are the entry's rows above it is a re-wrap onto fewer
    rows (XERK-2734): `_merged_above` hands the row all of them, the entry's first line takes the
    text (its id and start, the last row's end_ms) and the rest are blanked. Growing the line
    alone kept the rows above stored twice.
    - Only the first row under the entry's header: a header-less top row may have lost the rows
      above off the crop, and a re-flow there blanked the head row's other words (QA).
    - The rows claimed are the ones Teams showed right above it in its block (`_Line.above`).
      Walking the speaker's lines up to a `head` instead ran past an entry first seen with its
      header cut off and overwrote her previous entry (QA).
      Links come only from rows reading exactly as their lines: a header-cut top row aligned
      as a clipped re-read of the previous entry linked the two (QA).
    - The walk also stops at a line that headed its entry: links are never cleared, and one
      frame of OCR missing her next entry's header linked it to her previous one, which a later
      merge blanked (QA).
    - Not `head` for the grown line either: a merged row aligned to the second line makes it a
      head on the rewrite's first frame, and no merge fired after it.
    - The rows' words, aligned once and in order against the lead, must hold `_REVISED` of each
      row's distinct words (stutters/fillers dropped). One shared word ("enable") blanked three
      rows (kit fz.py: 13 words lost); pooled over the rows, or counted anywhere in the lead,
      her previous entry linked in by a header never read was blanked on words the new entry
      merely shared with it (QA kit x2734 af4.py).
    - And the lead must start with the entry's first word: a previous entry holding 60% of it
      by chance ("right we check" / "We check it now...") passed and was blanked (QA). Accepted:
      a merge that drops a leading filler ("um so we" -> "So we") keeps the draft, as main.
    - Refusing the growth instead (XERK-2728) left the row unpaired and re-stored the box.
    - Not handled: the entry as the bottom (live) block, another block above it: rows 2.. are
      stored twice, and some shapes re-store the box (as main; XERK-2746).
    - Accepted, as `_reflow`: the old layout coming back after the merge confirmed re-stores a
      blanked row as new, after the next speaker (QA flicker fuzz; Teams doesn't revert).
    - Measured vs main (ce18657), per seed: `_simulate`, `_simulate_teams`, kit tf.py identical;
      kit x2701 fz.py Teams fewer extra words on 5 seeds, none worse; QA kit x2734 af*.py no
      seed loses a said word (af2 counts a dropped filler, or a one-frame entry main's copies
      masked, as lost: 3 seeds); af merge kind 47/300 seeds fewer extra words.
    - Tests: `test_an_entry_rewritten_from_two_rows_into_one_is_stored_once`,
      `test_an_entry_rewritten_from_three_rows_into_one_blanks_both_rows_it_merged`,
      `test_a_merged_row_aligned_to_the_entrys_second_row_still_merges_it`,
      `test_a_growth_in_front_of_an_entrys_row_blanks_no_row_it_did_not_merge`.
  - A row reading another known line exactly is never growth of the line it misreads (as
    XERK-2719's `_grew_over_twin`): growing it copied that line (`test_a_row_reading_another_line_exactly_is_not_revised_into_it`).
  - Measured vs main (d386ad0): `_simulate_teams` (200 seeds) words lost 236 -> 143 (stored
    twice 139 -> 140); `noise=0.2` lost 255 -> 176 (1353 -> 1355). Zoom `_simulate` (1000
    seeds): clean, noise, drops identical per seed; noise + drops 494 -> 453 runs with a
    duplicate (single runs can do worse). QA's multi-row re-flow fuzz: no seed stores a word
    twice that main didn't. `_simulate` returns each segment once: an upsert re-emits it.
  - Tests: `test_a_middle_entry_finishing_after_the_next_began_is_stored`,
    `test_a_reworded_top_entry_keeping_its_words_is_stored`,
    `test_a_word_inserted_mid_entry_on_a_matched_stored_row_is_stored`,
    `test_a_one_frame_growth_of_a_stored_entry_is_never_stored`,
    `test_ocr_jitter_adding_a_word_on_every_frame_is_not_stored`,
    `test_a_rewrite_re_flowing_words_onto_the_next_row_is_not_taken_as_its_growth`,
    `test_an_entry_growing_twice_keeps_a_doubled_word`,
    `test_a_word_moving_between_two_of_a_speakers_entries_is_not_growth`,
    `test_a_word_moved_between_entries_stays_refused_once_its_entry_scrolls_off`,
    `test_one_letter_words_inside_a_growth_are_kept`,
    `test_a_word_inserted_as_the_live_entry_gains_a_row_is_stored`,
    `test_another_speakers_jittered_row_does_not_hold_back_a_growth`,
    `test_a_carried_entry_gaining_words_is_not_revised`, `test_an_old_entry_gaining_words_is_not_revised`.
