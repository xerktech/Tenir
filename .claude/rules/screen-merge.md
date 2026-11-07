---
paths:
  - "api/src/api/screen/merge.py"
  - "api/src/api/screen/meetings.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_screen_teams.py"
---

# Screen caption merge (`merge.py`, tracker carry)

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
  - A row only resembling a known line never pairs with it when rows below read that text
    exactly more often than later known lines hold it, and an earlier known line holds it
    (`_reread_below`, XERK-2719): [okay, ship it][okay.] over stored okay./okay. paired the
    grown row with the newest "okay." and stored the row below anew on every frame.
    - Both conditions are needed (QA): without the count, the live bottom twin growing stored
      a third copy; without the earlier copy, a garbled top row over a repeat was stored.
    - A row aligned as a misread that grew its line by whole words, while a row below reads
      that line's later twin, revises the line (`_grew_over_twin`, in `feed`). Refusing the
      misread instead left the row unpairable and re-stored the whole box (QA).
      - Like `_reflow`'s grew-glitch rule: two frames in a row must read it alike, and only a
        line first seen within `_REVISE_MS` (one frame of "like l o" was stored, QA).
      - Never when the row reads another known line exactly: it is that line, paired with
        one it starts with, and revising made a second copy (QA fuzz seeds 183, 161).
    - Both twin guards skip carried lines (as twin or as earlier copy): carried shapes keep
      main's behaviour. Refusing the misread for a carried line moved its growth onto the new
      twin and stored a carried neighbour again (QA). Tests:
      `test_a_carried_line_growing_above_its_twin_is_stored_as_new`,
      `test_a_carried_twin_growing_never_re_stores_a_carried_line`.
    - The unpaired grown row is then revised by `_revisions` via XERK-2717's draft cut (its
      twin below cuts the draft). Without that cut, the run's later entries diluted coverage.
    - Test: `test_a_grown_entry_above_a_same_text_entry_is_stored_once`.
  - A stored line extends only on whole-word growth, so the ASR finishing its last word after
    it was stored ("deploy" -> "deployment failed") loses the rest of that line. Accepted
    (XERK-2597): three char-growth rules each traded it for stray OCR chars stored ("yesl",
    "timeout'" freezing the line). `_simulate` never appends chars; re-tune on real captures.
    Tests: `test_ocr_jitter_on_the_stored_bottom_line_is_not_stored`,
    `test_ocr_jitter_on_a_stored_line_above_the_bottom_is_not_stored`.
  - OCR text is attacker-controlled (device token): results, roster and snapshots are capped
    (`ocr.MAX_LINES`, `zoom._MAX_ROSTER`, `merge._MAX_SHOWN/_MAX_CHARS`) and parse + merge run
    in `to_thread`. On the loop, one crafted frame stalled every socket ~8 s.
  - The caps alone still let 12 low-entropy rows ("a b a b") cost difflib ~1.5 s per frame on
    the shared 2-thread `CPU_POOL` (XERK-2730). `_match` charges each difflib comparison to a
    work bucket (`MergeBudget`, refilled per second of snapshot `ts`); a frame it can't pay
    for raises `OverBudget` before any mutation, and `on_caption` drops it
    (`screen.caption_over_budget.<platform>`), holding no meeting open.
    - Work is counted (`_work`: first longest-match pass + len*len/4), never timed: thread CPU
      doubles on a busy node, dropped real frames, and dropped frames re-store lines (QA).
    - One bucket per user (`ScreenMeetings.put`, two windows' worth), never per merger: after
      a meeting ends every frame gets a fresh merger, which had a full burst each (QA).
    - A refusal keeps what is left: emptying it refused a costly real frame on every retry.
    - Only `_match` may refuse (`charge`): work after it mutates the merger, so `_reflow`,
      `_moved`, `_held` `spend` into debt and `start` refuses frames until it is repaid.
      Unmetered, a crafted box re-sent unchanged cost 0 units and ~130 ms a frame (QA).
    - Every difflib call in merge.py is paid for; a new one must `charge` or `spend` too.
    - The memo (full ratio per pair, LRU) is exact; FIFO eviction thrashed on a dense box.
      Never share it across users: one would flush another's.
    - Sized for a dense 12 x 160-char English box, OCR noise on 60% of rows, at the 1 fps
      meeting cadence (~490k units a frame): 0 drops at 1 fps; at 2 fps only up to ~30%
      noise (60% drops half). Half the rate dropped frames at 1 fps. One user's 8 crafted
      windows: ~0.2-0.3 s CPU a second (was ~8). Raising
      `screen_meeting_fps` needs the rate scaled. Retune from `screen.merge_cpu.<platform>`.
    - Accepted: several users' device tokens together can still fill the pool.
    - Accepted: one window's clock (up to +60 s) stalls the refill for the user's others.
    - Tests: `test_crafted_caption_frames_spend_at_most_the_windows_work_budget`,
      `test_a_frame_over_budget_mid_alignment_changes_nothing`,
      `test_a_crafted_box_re_sent_unchanged_pays_for_its_reflow`,
      `test_frames_after_a_meeting_ended_spend_the_windows_budget`,
      `test_a_users_windows_share_one_work_budget`,
      `test_a_caption_frame_over_budget_is_dropped_and_counted`.
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
- Teams rewrites a finished entry (punctuation, fixed words, re-wrapped lines; XERK-2694), so its
  rows align as new. `merge._revisions` offers rows the alignment calls new to the recent
  (`_REVISE_MS`) unshown known lines after the last aligned one, block by block, by words
  (case/punctuation stripped, `_REVISED` coverage); matches upsert those segments (same ids).
  - Revising must reach the newest recent known line: else "okay", Bo, "okay so lets start"
    folds Ana's new line into her old one.
  - A run's claims are a prefix of its rows: [okay, sounds good] after a dropped frame over a
    known "sounds good" is a repeat, not a revision (QA: the reply was lost).
  - Aligned rows of a revised block take new words only once two frames read them alike
    (`_Line.pending`, confirmed in `_update`); case/punctuation at once. Taking the one rewrite
    frame stored misreads for good (XERK-2597); a word-move heuristic refused real Teams edits
    (it drops words too) and lost the moved word. `end_ms` stays put.
  - For `_REVISE_MS` after a rewrite, a matched row takes the first differing text two reads
    in a row agree on; a read equal to the stored text drops what is held. Confirming whenever
    the held text recurred stored jitter flipping back later; only the very next read lost the
    rewrite to one noisy frame (both QA). Two identical misreads in a row are stored (accepted),
    but on the crop's header-less top row (the only row a crop clips) a read that is the stored
    or held text with its start cut off is ignored: a clipped static row reads alike on every
    frame (QA). Accepted: a rewrite there dropping leading words keeps the draft row. "Top row"
    is the crop's, not shown[0] after the `_MAX_SHOWN` cut (QA, 13+ rows). Applied to
    every row, it refused real rewrites under a header or below another block (QA).
  - Carried lines are never revised (`_revise` refuses them): a rewrite of their block re-emitted
    the ended meeting's segment into the next conversation (QA). Test:
    `test_a_rewrite_never_revises_the_ended_meetings_carried_line`.
  - The merger re-emits the same `Segment` object it mutates: copy a field before asserting
    it changed or didn't (a test compared an object with itself).
  - A fresh header block revising nothing ends revising: [Bo, Cy yes, Ana okay] is a repeat.
  - A run's draft (the speaker's consecutive unshown lines) is cut where her next block
    re-reads one of them verbatim (XERK-2717): each Teams entry is its own block, so a one-row
    entry growing above her next one ("green." -> "green, so ship it") was measured against
    both and stored twice. `_align` can't pair it: a block's top row (XERK-2716, below).
    - Cuts are tried before the full draft, longest first: a full draft passing coverage
      swallowed a short third entry ("ok.") unclaimed, and it was stored again (QA).
    - Only her own block cuts (exact speaker): Bo repeating her line broke a valid revision.
    - Only a line seen within `_SAID_BETWEEN_MS` cuts: at 1 fps a new line plus a repeat of her
      next entry can't both be said between snapshots; after dropped frames they can ("yes." /
      "yes I think so too." stays a new line). Same frames, no gap: it is the entry growing.
    - Measured per seed vs main (after XERK-2716): Zoom `_simulate`, `_simulate_teams` and kit
      zf.py identical; kit x2716 tf.py "both" (200 seeds) extra words 8635 -> 7212, lost
      5566 -> 5602 (late words now stored after the next entry; main's copies masked them).
    - Tests: `test_an_entry_growing_above_the_same_speakers_next_entry_is_stored_once`,
      `test_a_short_entry_after_the_same_speakers_next_one_is_stored_once`,
      `test_bo_repeating_anas_next_entry_cuts_none_of_her_draft`,
      `test_the_longest_cut_of_a_draft_is_tried_first`,
      `test_a_header_less_next_block_still_cuts_the_draft`,
      `test_a_new_line_starting_with_an_old_ones_words_is_not_explained_away` (the gap).
  - Failing the cuts and the full draft, the draft up to her next entry is tried last: a line
    first shown under a header (`_Line.head`, XERK-2720). Her next entry still growing,
    misread or re-punctuated re-reads none of her lines verbatim, so no cut fired.
    - Only last: Zoom heads a speaker's run once (a scrolled-up row becomes a block's first
      row), and cutting there first stored more lines on Zoom `_simulate` noise+drop seeds.
    - Any cut (verbatim or this one) needs the cut entry seen within `_SAID_BETWEEN_MS`: a
      short entry scrolled off was claimed by a later row holding its few words, and lost (QA).
      The XERK-2717 cut did it too, masked only by the later rows failing. Neither simulation
      reaches this path; a same-speaker Teams fuzz did.
    - Not "seen on the last frame": one frame of OCR missing her block re-stored the box (QA).
    - Accepted: OCR missing her block for over `_SAID_BETWEEN_MS` (3+ frames at 1 fps) re-stores
      the box (main stored it once). Every looser gate QA tried also lost the scrolled-off
      line; telling a miss from a scroll needs another signal (a row below scrolling up).
      Test: `test_a_grown_entry_ocr_missed_for_3_s_is_stored_again_but_never_lost`.
    - Tests: `test_a_grown_entry_above_the_speakers_next_entry_is_stored_once`,
      `test_a_grown_entry_above_a_live_entry_is_stored_once`, `test_a_grown_middle_entry_is_stored_once`,
      `test_an_entry_scrolled_off_is_never_claimed_by_a_later_one`,
      `test_a_zoom_speakers_scrolled_lines_are_revised_as_one_run`,
      `test_a_grown_entry_is_stored_once_after_ocr_missed_a_block_for_a_frame`.
  - New lines go just above the known line shown below them (Teams adds lines to an entry
    above another speaker's); appending them broke the next snapshot's order.
    - The store orders by start, so such a line also takes the known line's start and that line
      and the ones after it move down a ms each until there is room (`_place_above`, XERK-2729);
      moved stored segments are re-emitted (upsert). First-seen time stored a wrapped row after
      the next entry. `first_ms` stays real time (it gates `_REVISE_MS` and friends).
      Tests: `test_a_grown_entrys_wrapped_row_is_stored_before_the_next_entry`,
      `test_lines_after_a_wrapped_row_keep_unique_increasing_starts`,
      `test_making_room_never_moves_or_re_emits_a_carried_line` (carried lines are skipped).
  - A rewrite onto fewer rows than the draft blanks the draft lines no row claims whose words
    (`_REVISED` of them) its rows took, after the first claimed one (`_claims`, XERK-2739):
    "so we need to" / "check the cluster" -> one row claimed only the first; the rest stored twice.
    - Only unshown draft lines are candidates, so a real next row still on screen is never blanked.
    - Never a line from her next entry on (`_Line.head`): it may repeat the words the rewrite fixed
      ("to tst first" / "to test first"); OCR missing its block on the re-lay frame blanked it
      for good (QA). Accepted: Zoom heads scrolled rows too, so fewer Zoom stale copies go.
    - Only the accepted revision blanks (`p == len(cands)`), and only lines `_REVISED` taken.
    - Accepted: a draft line the rewrite dropped (a stutter) stays stored.
    - Measured per seed vs main: Zoom `_simulate` (4000 runs) identical; `_simulate_teams` 2/400
      seeds differ, each storing fewer words twice and losing none.
    - Tests: `test_an_entry_re_laid_onto_one_row_blanks_its_later_rows`,
      `test_a_row_a_rewrite_dropped_is_kept`,
      `test_the_speakers_next_entry_ocr_missed_during_a_re_lay_is_never_blanked`,
      `test_a_refused_rewrite_blanks_nothing`, `test_a_draft_row_the_rewrite_took_one_word_of_is_kept`.
  - Measured (1000 seeds, vs. before): clean, noise and 30% drops unchanged; noise + drops
    389 dup runs (409), 118 lines lost (113). `_simulate` is Zoom-only: it can't show rewrites.
  - Tests: `test_a_rewritten_teams_entry_is_stored_once` (real pairs from a 2026-11-04 meeting),
    `test_a_rewritten_entry_keeps_the_drafts_segment_ids`, `test_a_new_entry_like_an_old_one_is_not_a_rewrite`,
    `test_a_repeat_below_a_new_line_after_dropped_frames_is_not_a_rewrite`,
    `test_a_rewrap_moving_a_word_onto_a_matched_row_keeps_it`,
    `test_ocr_jitter_on_a_rewritten_entrys_matched_row_is_not_stored`,
    `test_a_one_frame_misread_of_a_rewritten_row_is_never_stored`.
- Teams re-flows a growing entry (XERK-2715): a word inserted or fixed moves words across row
  breaks, so row-level rules stored a stale draft beside its re-flowed copy (~7% of real lines).
  `CaptionMerger._reflow` then upserts each block as one entry: rows replace their lines' texts
  in order; same-speaker lines between them whose words the nearby rows hold are dropped.
  - A dropped stored line is re-emitted with text "" (same id): the store has no delete, and
    `utterances.sentences` skips blanks. Every reader must go through it. `segmentCount` counts them.
  - Only a re-flow is taken (`_moved`: a word crossed a break and the rest of the row reads
    alike); a word changed within a row is jitter for the row rules. Without the "rest reads
    alike" check, Zoom `_simulate` noise made up text in 9 runs instead of 2.
  - Two frames in a row must read the rows above the bottom alike (`_reflow_reads`).
  - A skipped line is dropped only when the rows around it hold its words AND they moved there
    (those rows gained them, or no longer read as their lines, or its words sit at their break
    and a neighbour changed since it was last seen: the row rules may have grown it already).
    The live bottom line doesn't count as that change: it grows every frame (QA). Holding the words alone blanked
    a real line OCR missed on 2 identical frames next to rows sharing its words, Teams and
    Zoom alike (QA); a per-seed Zoom A/B caught it where aggregate counts matched.
  - A stored bottom row is confirmed on two frames too; a live one is left to the row rules.
    Excluding it stored a misread landing on the confirming frame for good (QA).
  - The crop's header-less top row clipped mid-word (a char suffix of its line) keeps the line.
  - Glitch rows between placed rows pair with the lines between them (`_fill_glitches`): a
    re-flowed row resembling its draft aligns as a misread, and its new words were lost. A run
    at the block's bottom pairs with its speaker's unshown lines right after: an ended entry
    re-flows with the next speaker's block below it, which aligns (the ticket's own example).
  - Case/punctuation-only changes are not a re-flow; the draft's case stays (as before).
  - A row whose words start with its known line's ("green." -> "green, so ship it") is a
    `_misread` of it (XERK-2716) when it isn't its block's top row: unpaired under a header it
    cost `_UNPAIRED`, so a 3-row entry re-flowing above the same speaker's next entry aligned
    nothing and the box was stored again. The row above needn't match (a re-wrap moves words).
  - Never pair a block's top row that way: it may be a new line starting with an old one's words
    ("yes." / "yes I think so too."), and `_fill_glitches` can't place a top-row glitch, so its
    growth was lost (QA: hundreds of words in a "late growth" fuzz). A row of the block above
    doesn't count: dropping that check lost hundreds more.
  - `_reflow` takes such a glitch row that grew by whole words even with no word moved (else
    "like" -> "like with the keto." was lost): a glitch row only (the live bottom row grows every
    frame; taking it rewrote misaligned rows, `_simulate_teams` seed 125), and only for lines
    under `_REVISE_MS` old (older ones stored OCR junk). Accepted: a stray glyph read on every
    frame of a recent row is appended.
  - Measured per seed vs. main: `_simulate_teams` identical; Zoom `_simulate` differs on 18/1000
    noise+drop seeds: fewer wrong lines on all, one loses a line more (3829). QA's kit
    (`~/.cache/qa-tenir-kit/x2716`, Teams late-growth fuzz) is better in total on every mode;
    ~1.6% of its seeds do worse (noisy rows re-stored).
  - Tests: `test_a_finished_entry_reflowing_above_the_same_speakers_next_entry_is_stored_once`,
    `test_a_reflow_moving_two_words_down_above_the_same_speakers_next_entry_is_stored_once`,
    `test_a_one_row_entry_growing_above_the_next_speaker_keeps_its_new_words`,
    `test_a_new_line_starting_with_an_old_ones_words_is_not_explained_away`,
    `test_an_old_row_re_read_longer_is_jitter_not_growth`,
    `test_a_growing_bottom_row_never_rewrites_the_rows_above`.
  - Measured with `_simulate_teams` (200 seeds, 38k words): words stored twice 607 -> 139, lost
    522 -> 236; with `noise=0.2`, 1978 -> 1353 and 595 -> 255. Zoom `_simulate` (1000 seeds,
    all four modes): every seed stores exactly what main does. After tuning `_reflow`, diff
    per seed, not totals; `_simulate_teams` is synthetic, so replay real crops when there are some.
  - Tests: `test_simulated_teams_reflow_stores_few_words_twice`,
    `test_a_rewrite_pulling_a_word_onto_the_next_row_is_stored_once`,
    `test_a_word_pulled_up_blanks_its_stored_draft_row`,
    `test_a_rewrapped_middle_row_takes_its_new_words`, `test_a_one_frame_rewrap_is_never_stored`,
    `test_a_middle_row_missed_on_two_frames_is_kept_when_its_neighbours_share_its_words`,
    `test_a_misread_bottom_row_on_the_confirming_frame_is_never_stored`,
    `test_a_top_row_clipped_mid_word_keeps_its_line_during_a_reflow`,
    `test_a_finished_entry_reflowing_above_the_next_speaker_is_stored_once`,
    `test_a_middle_row_missed_while_the_bottom_row_grows_is_kept`,
    `test_a_line_the_ocr_missed_between_rows_is_kept`.
- Teams adds words to a stored entry above the bottom (finishing it after the next began, or
  rewording it; XERK-2721). Rows there keep their text and aligned as misreads, so it was lost.
  `CaptionMerger._grow` takes a recent (`_REVISE_MS`) stored line's read that keeps all its
  words in order and adds whole ones (`_inserted`), once two frames in a row agree.
  - A run of added words with no 2+ char word ("x", "l") is jitter: static pixels repeat a
    misread on every frame, so two frames agreeing isn't enough (QA). Doubled words and "a"
    inside a longer run are kept: refusing them lost a second growth main stored (QA).
    Accepted: a word OCR doubles on every frame ("the the") is stored.
  - Applied after `_reflow` (`_apply_grows`), and only while the row's block's other rows and
    the speaker's rows right above/below read as their lines: else it is a re-flow or rewrite
    (within a block or across her entries), and taking the row alone stored the moved words
    twice (QA). Checking only for the moved words missed a word inserted beside them.
  - A read refused so is refused for good (`_Line.grow_refused`), from its first frame: only
    waiting let it land once the changed entry scrolled off (QA). Accepted: if that entry is
    OCR-missed on exactly the frames the word moves, nothing shows the re-flow.
  - A row with no known line (the live entry's new bottom row, a glitch) counts as unchanged:
    counting it refused real insertions for good (QA: Teams-sim words lost 143 -> 185).
  - Accepted: a rewrite with fewer words ("green done now." -> "green.") keeps the draft.
  - Pre-existing, kept: a matched stored line growing by whole words within `_EXTEND_MS`
    extends on one frame (any line, not only the newest); waiting delayed a line finalized
    by the next block and broke `test_extended_line_never_ends_before_it_starts`.
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
