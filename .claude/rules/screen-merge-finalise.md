---
paths:
  - "api/src/api/screen/merge.py"
  - "api/tests/test_screen.py"
---

# Screen caption merge: Teams finalising an entry (XERK-2774)

- Split from `screen-merge.md` (size cap); its invariants still apply here. These refine
  `_revisions` (Teams rewrites a finished entry, XERK-2694).
- Rows reading exactly the draft's first words pass coverage too (`_cut_back`, XERK-2774):
  Teams finalising an entry cuts its live tail ("do you have something and then like" ->
  "Do you have something?"); 5 of 25 real duplicates (2026-11-10). Needs `_CUT_WORDS` (3)+
  words and half the draft's, else "We need to." after "we need to fix the cluster..." was
  folded into it. Unpunctuated, the cut row aligns as a misread instead (draft kept).
  - Only the draft's last line may lose words: dropping a whole row left it known but
    unshown and re-stored rows (QA fuzz seed 78). It also refuses a draft running on into
    her later entries, which let a new entry opening like her entry before last overwrite
    it past the "reach the newest line" guard (QA).
  Tests: `test_an_entry_teams_cuts_back_as_it_finalises_is_stored_once`,
  `test_a_short_new_line_opening_like_the_last_is_not_a_cut_back`,
  `test_a_new_entry_opening_like_her_entry_before_last_is_not_a_cut_back`,
  `test_a_cut_dropping_a_whole_row_of_the_draft_stores_nothing_twice`.
- Any share of the draft passes when the words cut off open her next entry right below:
  Teams split the entry in two ("hi good morning so yesterday we" -> "Hi, good morning." /
  "So yesterday we..."; real sampler frames, 2026-11-11). Only while the draft changed within
  `_SAID_BETWEEN_MS`: a long-finished entry was overwritten by a new pair opening like it (QA).
  And only when the draft reaches her newest line: a new entry reopening the words of one she
  had just finished was split, and the finished one taken as the draft was overwritten (fuzz).
  Tests: `test_an_entry_teams_splits_in_two_as_it_finalises_is_stored_once`,
  `test_a_finished_entry_is_not_taken_for_a_split`,
  `test_a_split_of_her_live_entry_keeps_the_like_one_she_just_finished`.
- With no row aligned (every known row scrolled off), the first run may take its draft from
  a later one of her recent lines, not only her oldest: that one claimed nothing and the
  split was stored beside its draft. A later start is taken only as a split (`split_only`)
  and never skips lead rows: by coverage, a new entry sharing 2 of a 3-word draft's words
  overwrote it, a start past her first draft line lost it, and a repeat under a garbled
  re-read ("~kay" / "okay") claimed the old line (QA, Zoom fuzz 425/512).
  Tests: `test_a_split_entry_with_no_known_row_left_on_screen_is_stored_once`,
  `test_a_new_entry_after_dropped_frames_does_not_overwrite_her_last_one`,
  `test_a_later_draft_start_never_skips_her_first_draft_line`.
- Real Teams frames (sampler, XERK-2774), two more causes of a box stored again:
  - `_score` pairs a row with the same words as a known line as exact: a stored entry Teams
    re-punctuates ("go to Cy" -> "go to Cy.") was only a ratio match, under the cost of
    leaving the rewritten live entry unshown.
  - `_update` takes growth that adds punctuation at the join ("All right" -> "All right,
    let's..."). Not by words alone: a re-flowed row then overwrote "pods." with the row above
    (`_simulate_teams` seed 125). Test: `test_an_entry_punctuated_as_it_grows_is_stored_once`.
    - Only on her last line: above her next one it is Teams re-laying the entry, moving a word
      up ("...locally" -> "...locally, or"); taken as growth, the rows below kept too few of
      the draft's words to claim it and its stale lines were stored (QA fuzz seed 84). Test:
      `test_a_word_moved_up_with_a_comma_above_her_next_line_is_a_rewrite_not_growth`.
  - The words-only match needs `_CUT_WORDS`+ words: "Yes??" after "Yes." is as often her next
    entry, and matched by words its draft went to the entry after and was lost (QA fuzz).
    Test: `test_a_short_entry_punctuated_otherwise_is_not_a_re_read_of_her_last`.
  - The real meeting replays at 0 duplicate rows with its roster seeded, 1 without (main: 2
    and 3); `_simulate` (1000 x 4) and `_simulate_teams` (200 x 2) identical to main per seed.
  - Still open: a split below another speaker's re-punctuated short entry ("Cy" -> "Cy.")
    stores the draft twice (XERK-2805); a short entry Teams reorders around a live entry
    stores a line twice (XERK-2806). Both on main too.
