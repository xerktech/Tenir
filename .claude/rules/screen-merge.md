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
    - "Twin" means the same words, or one char of one word misread (`_same_text`, XERK-2727):
      "0kay." under a grown "okay, ship it" stored 6 rows. Same word count + ratio >= 0.75
      took different lines for twins and re-stored whole boxes (QA fuzz). A grown row never is.
      - Only rows count as twins (`_reread_below`'s `below`, `_grew_over_twin`); known lines
        compare exactly in `_reread_below`: a one-char-misread stored line counted as a twin
        re-stored a 4-line box (QA fuzz, `test_qa_fuzz_panels_store_what_main_did`).
      - Accepted: counting rows by twin in `below` costs ~0.5% of QA's noisy Teams fuzz seeds a
        stored misread or an extra short row (vs. main); exact `below` loses ticket case 3
        (`twin_misread`).
      - A block's top row grown over its twin row may pair as a misread in `_align` (no other
        top-row misread fits: ratio < 0.5).
      - Accepted: under Teams re-attributing entries (QA relab fuzz) this pairing does worse
        than main on ~2% of seeds, once taking another speaker's words (XERK-2736).
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
    - Several users' tokens together: `CPU_POOL` serves users in turn (XERK-2737).
    - Accepted: one window's clock (up to +60 s) stalls the refill for the user's others.
    - Tests: `test_crafted_caption_frames_spend_at_most_the_windows_work_budget`,
      `test_a_frame_over_budget_mid_alignment_changes_nothing`,
      `test_a_crafted_box_re_sent_unchanged_pays_for_its_reflow`,
      `test_frames_after_a_meeting_ended_spend_the_windows_budget`,
      `test_a_users_windows_share_one_work_budget`,
      `test_a_caption_frame_over_budget_is_dropped_and_counted`.
  - A misread must look like its known line (same speaker, ratio >= 0.5): pairing any two
    lines "explained away" a reply said between snapshots.
  - A new Teams entry between two known ones is stored, not a glitch (`_inserted_entry`,
    XERK-2727): a header block's first row of the known line's speaker that starts with its
    words and adds more. Unpaired it cost `_UNPAIRED`, so the entries below were stored again.
    - Only when that known line can't be revised: carried (its growth is a new line), or the
      row above is aligned to it. A skipped revisable line is `_revisions`' (XERK-2717 shape):
      inserting there stored "green, so ship it" beside "green.".
    - Never a row that reads a known line exactly: it is that line wrapped, and inserting it
      stored it twice and lost the word wrapped below (QA fuzz dump in
      `test_qa_fuzz_panels_store_what_main_did`).
    - Never when another of the speaker's known lines is its twin: the row above may be that
      one (leading lines skip free), and this row the line grown (QA: a third "yes" stored).
    - Costs `_INSERTED` (below `_GLITCH`): a grown row over its twin stays a misread.
    - Tests: `test_a_same_speaker_entry_inserted_above_another_speakers_is_stored_once`,
      `test_a_carried_entry_grown_above_a_carried_line_stores_only_the_growth`,
      `test_an_entry_grown_below_its_twin_is_not_an_inserted_entry`.
  - A stored block re-read under another speaker's header (Teams re-attributing it) never
    pairs here: matching across speakers, even verbatim 3+ words, lost a real repeat and its
    reply (XERK-2727 QA). `_reheaded` re-labels it on its own signal (XERK-2736, below).
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
    - Stays 3 s although real Teams gaps run past it (XERK-2724, v0.6.169 Teams meeting:
      `caption_gap.teams` n=33 p50 2052 p95 4115 max 4149 ms; caption OCR p50 1.8 s). At 3 s
      ~1 gap in 10 reads as dropped frames and stores a grown entry twice. 5 s fixed that, but
      lost lines at 3-5 s gaps (QA fuzz, kit x2724g): a short entry claimed by her next one
      starting with its words (XERK-2799, fixed at <= 3 s; re-check it at 3-5 s), and [sounds
      good] x2 -> [okay / sounds good] via `_claims(lead=)`. A duplicate is the lesser harm.
      Faster snapshots (XERK-2798) shrink the duplicates; don't re-widen without both checked.
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
    - The panel's top run claiming only the entry above her next one (`own` cut) may claim one
      of her later entries instead (`_revise_runs(pick=)`, XERK-2799): "Yes." scrolled off and
      "Yes we can ship it." re-laid; the one word passed coverage, every later claim shifted
      up an entry and "Yes." was lost. The gap gate can't see a scroll within 3 s.
      - `_revisions` runs the whole panel once per candidate and keeps a switch only when the
        panel pairs strictly more words than without it: "Yes." still on top and rewritten
        ("Yes, we can.") pairs fewer once every later run shifts down an entry.
      - Never decide it by counting her header blocks: one misread header or stale draft
        line miscounted and it switched (QA).
      - Each candidate runs alone: a greedy pick starved her later runs (switch refused).
      - Among candidates the top run pairs whole, the latest wins (the panel shows the newest),
        else the most words: a stale draft or the scrolled entry it grew from ("Okay." ->
        "Okay, after the review we merge.") pairs as fully as the shown one (QA fuzz).
      - Measured vs main (kit fz2, 28800 runs): words lost 110251 -> 110205, extra 133282 ->
        130976; no run loses a row shown on screen (fz, 10800: 72 better, 1 trade).
      - Tests: `test_a_short_entry_scrolled_off_above_her_next_starting_with_its_words_is_kept`,
        `test_a_short_entry_still_shown_and_rewritten_is_revised_in_place`,
        `test_the_entry_a_top_run_re_lays_is_the_latest_it_pairs_whole`,
        `test_a_short_entry_rewritten_under_a_misread_header_is_never_kept_beside_it`.
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
    - `_align` can't match a row below such a new row (unpaired under a header: `_UNPAIRED`);
      only `_revisions` pairs it. So a known line past `_REVISE_MS` is still a candidate for a
      new row reading it verbatim, and never for any other (XERK-2738): else it was stored
      again and the new row ordered after the old copy.
    - Only while new rows reading it exactly outnumber the speaker's later known copies of it
      (as `_reread_below`): an older twin ("yes" said twice, one row shown) took the recent
      one's claim, `p == len(cands)` failed and a revision main made was lost (QA). "Newest
      copy only" instead left one of two rows re-reading a repeated line new.
    - Each gate (re-read, count, speaker, guard) has a test that fails when it is removed.
    - `_simulate_teams` only grows the last entry, so its per-seed A/B never reaches this path.
      Tests: `test_a_row_added_above_an_old_entry_is_stored_before_it_once`,
      `test_an_old_line_is_never_rewritten_by_a_new_row_resembling_it`,
      `test_an_older_twin_of_a_line_never_spoils_a_revision`,
      `test_rows_re_reading_an_old_repeated_line_each_pair_with_a_copy`,
      `test_another_speakers_copy_of_an_old_line_does_not_hide_it`,
      `test_an_old_line_no_row_re_reads_verbatim_is_not_offered`.
  - A rewrite with fewer rows than its draft (rows joined, stutters dropped) drops each draft
    line no row claimed whose every word the claiming rows hold (`_claims` `folded`, XERK-2701):
    blanked like `_reflow`'s stale lines. A draft line with a word the rows lack stays.
    - Held one frame (`_fold_held`): dropped when the next frame doesn't show the draft line.
    - On the drop, the claiming lines open the rewritten-row window (`rewrite_until`): with
      the draft row gone, later reads align onto them and keep their text, so a misread
      rewrite read on 1 or 2+ frames was stored for good (two QA rounds). Main revised it.
      Accepted, as for any rewritten row: two identical misreads right after the drop are
      stored (main kept the right text plus the duplicate row).
    - No check that the claiming lines are seen on the drop frame: a frame missing her block
      (another speaker only, scrolled, empty) then still takes the fold, and the rewrite seen
      again is stored once. Accepted: if the draft itself comes back after such a frame (a
      reverted rewrite), it is stored again, out of order (QA sc15 `v1_C_then_D`).
      Tests: `test_a_fold_is_taken_over_a_frame_showing_only_another_speaker`,
      `test_two_like_misreads_long_after_a_fold_are_not_taken` (the window's end).
    - Tests also: `test_a_jittered_read_right_after_a_fold_is_never_stored`,
      `test_a_draft_row_shown_again_is_never_folded`.
    - Only with a revision that stands: `_revisions` voids folds with its claims (`p`).
    - Tests: `test_a_rewrite_with_fewer_rows_than_its_draft_drops_the_folded_row`,
      `test_a_rewrite_keeps_a_draft_row_whose_words_it_does_not_hold`,
      `test_a_garbled_rewrite_frame_is_not_kept_by_the_fold`,
      `test_no_draft_row_is_folded_when_the_rewrite_revises_nothing`.
    - Re-lay onto one row starting with the head's words (XERK-2739) and the speaker's next entry
      repeating the fixed words while OCR-missed (own block, or read into this one with its
      avatar missed: its words pair with the rewrite, never blank it). Tests:
      `test_an_entry_re_laid_onto_one_row_blanks_its_later_rows`,
      `test_the_speakers_next_entry_ocr_missed_during_a_re_lay_is_never_blanked`,
      `test_the_next_entry_read_into_the_block_during_a_re_lay_is_never_blanked`,
      `test_a_re_lay_onto_fewer_rows_blanks_rows_below_a_second_claimed_row`,
      `test_a_row_a_rewrite_dropped_is_kept`, `test_a_refused_rewrite_blanks_nothing`,
      `test_a_draft_row_the_rewrite_took_one_word_of_is_kept`.
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
  - A block's top row pairs that way when the row above matched the line just before
    (`_misread(placed=)`, XERK-2791): one speaker's one-row Teams entries are all top rows, and
    one growing as the next appeared stored the box again (real frames). Off-roster speakers
    were None before, cheap glitches that hid it. No probe found yet tells the "row above
    matched" bound from "any row above"; keep it until real frames show it costs lines.
    Tests: `test_an_entry_growing_as_the_next_one_appears_below_is_stored_once`,
    `test_a_new_entry_under_a_shown_twin_of_its_start_is_a_new_line`.
  - Never pair any other block's top row that way: it may be a new line starting with an old
    one's words ("yes." / "yes I think so too."), and `_fill_glitches` can't place a top-row
    glitch, so its growth was lost (QA: hundreds of words in a "late growth" fuzz). A row of the block above
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
- Teams re-lays the bottom (live) entry onto fewer rows (XERK-2746); `_merged_above` covers
  a block below it (XERK-2734).
  - The bottom row re-reading its line with the entry's rows below merged in folds those lines
    (`_merged_below`, as `_claims` folds: dropped a frame later unless shown again). The live
    line takes the merged text in `_update`; nothing claimed the rows below, so they stayed.
  - Only when the line took the row's text, and the row holds every word of the line then of
    each folded line, in order. At `_REVISED` coverage words the row lacked were lost for good
    (QA: "prod", "quickly"; a re-flowed row's "check" OCR-missed). The line losing a word is a
    re-flow (`_reflow`'s): folding there lost the word moved up (`_simulate_teams` seed 32).
    Accepted: a re-lay that rewords or drops a filler keeps the row below, as before.
  - The line and the folded lines must be consecutive rows of the last frame: a bottom row
    paired with an older line it starts like ("itself.") folded the live row (seed 191, 0.2).
    Accepted: one frame of OCR missing a row right before the re-lay keeps it, as before.
  - The same-speaker check is defensive: another speaker's header finalizes the line above
    it, so that line can't grow over their row.
  - `_claims` coverage counts a stutter ("so so so") once: a stored entry re-laid without its
    repeats failed coverage and the whole box was stored again.
  - A header run's rows above its first claim stay new rows (placed above) when the draft was
    seen within `_SAID_BETWEEN_MS` (`_claims(lead=)`): Teams added words on a row above the
    entry's first. At 1 fps a line and a repeat of the next can't both be said between
    frames; after dropped frames [okay, sounds good] over a known "sounds good" is two lines.
  - Measured per seed vs main (e7131ea): Zoom `_simulate` (4 x 1000) 30 noise+drop seeds
    differ, none worse by line counts: duplicate lines 66 -> 31, lost 22 -> 22 (QA saw two
    keep main's misordered copy instead of the placed one). `_simulate_teams` (200): noise 0
    identical totals; 0.2 extra/lost words 1303/111 -> 1222/109; no seed worse.
  - Tests: `test_an_entry_re_laid_onto_its_bottom_row_is_stored_once`,
    `test_a_row_below_with_a_word_the_bottom_row_lacks_is_never_folded`,
    `test_a_row_added_above_an_entrys_first_row_is_stored_before_it_once`,
    `test_an_entry_grown_at_its_start_then_re_laid_above_another_is_stored_once`,
    `test_a_row_above_a_repeat_after_dropped_frames_is_a_line_said_between_them`,
    `test_a_bottom_row_growing_other_words_keeps_the_row_ocr_missed_below_it`,
    `test_a_row_the_bottom_line_did_not_take_is_never_folded`,
    `test_one_frame_reading_two_rows_as_one_blanks_neither`,
    `test_a_fold_never_takes_a_reflow_or_an_older_line`,
    `test_another_speakers_entry_is_never_folded_into_the_bottom_row` (no gate pin).
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
  - The row below need only share the grown line's words up to the shorter of the two
    (XERK-2804): Teams re-punctuates or grows the next entry, or OCR clips its row, on the frame
    the missed row returns. It must still hold more words than the row it splits off.
  - A next entry wrapped onto 2+ rows: its later rows pair with the lines after the grown one
    when they read them exactly (XERK-2804); left new, its second row was stored twice.
  - `_reflow` moves `changed_ms` and notes `held` for a stored line too, as `_revise` does: left
    at the old time, a rewrite within `_HELD_MS` noted the re-flowed text as held (XERK-2804).
  - Tests: `test_a_new_entry_read_while_ocr_missed_the_row_above_is_stored_once`,
    `test_a_finished_entry_grown_while_ocr_missed_the_row_above_is_stored_once`,
    `test_a_repeated_entry_ocr_clipped_on_a_frame_keeps_both_copies`,
    `test_a_paused_entry_repeated_long_after_keeps_both_copies`,
    `test_another_speakers_entry_read_while_ocr_missed_the_row_above_is_stored_once`,
    `test_the_row_below_a_missed_row_need_not_read_the_grown_line_exactly`,
    `test_a_two_row_entry_read_while_ocr_missed_the_row_above_is_stored_once`,
    `test_a_reflowed_stored_line_s_text_is_held_only_from_the_reflow`.
- Growth of stored Teams entries (`_grow`, `_apply_grows`, `_merged_above`; XERK-2721,
  XERK-2734): see `screen-merge-grow.md`.
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
- A snapshot reading the last one's rows exactly under other header names re-labels those
  lines (`CaptionMerger._reheaded`, XERK-2736): Teams re-attributed an entry, or OCR read
  another name. A misread needs the same speaker, so the block was stored again.
  - Only within `_REHEAD_MS` (1.5 s) of the last frame: after one dropped frame [Ana X][Bo ok]
    -> [Cy X][Bo ok] may be Cy repeating X and Bo his reply. A 3 s window lost both (QA), as
    did matching X across speakers by text alone (XERK-2727 QA).
  - Stays 1.5 s although real Teams frames reach the merger ~2 s apart, so a Teams re-label is
    stored twice (XERK-2797). Within one ~2 s frame pair a re-attribution and another speaker
    repeating the entry read the same, so any wider gate (fixed or cadence-relative, QA) loses
    the repeat round: [Dee X][Dee yes] -> [Cy X][Dee yes] kept 2 of 4 lines (fuzz: ~20 fewer
    duplicates, 9 more lost / 1500 seeds). A duplicate is the lesser harm; XERK-2798 is the fix.
    Test: `test_a_cross_speaker_repeat_round_at_a_two_second_cadence_keeps_every_line`.
  - Each guard lost real lines without it (QA, fuzz): a row must keep its name (an anchor: a
    one-row panel or a goodbye round said again in full turned over); the names must not read
    as the last frame's scrolled up ([Bo X][Ana X] -> [Ana X][Ana X]); each re-named entry
    needs `_REHEAD_WORDS` (3)+ words over its rows (a short reply is said whole between frames).
    Never a line with a twin (the speaker's other recent line of its text): the alignment may have
    folded a repeat into it, and re-labelling it lost the repeat (QA fuzz seed 936).
  - New names wait for the next frame to agree (`_pending`). If it doesn't, `feed` merges the
    held frame then it, as main would have; a held frame merging nothing lost its line (QA). Names
    flipping back drop the held frame (a one-frame misread).
    Other names again replace it: Bob -> Bib (misread) -> Ana ends on Ana, not three copies.
  - Accepted: a short entry re-attributed is stored again; so is one re-attributed with no
    anchor, with a scroll, or after a dropped frame. Carried lines keep their speaker (XERK-2615).
    The 1 fps [Ana X][Bo ok] -> [Cy X][Bo ok] (both said again within a second) is re-labelled.
  - Tests: `test_an_entry_re_attributed_to_another_speaker_is_stored_once_under_the_new_name`,
    `test_another_speaker_repeating_a_line_and_its_reply_after_dropped_frames_stores_both`,
    `test_a_cross_speaker_repeat_on_a_one_row_panel_keeps_every_line`,
    `test_a_long_line_said_again_as_the_panel_scrolls_is_kept` (scroll guard),
    `test_a_name_misread_on_one_frame_before_a_scroll_re_labels_nothing` (agreement),
    `test_a_held_frame_s_repeat_is_stored_when_the_panel_moves_on`,
    `test_an_entry_with_a_same_text_twin_is_never_re_attributed`,
    `test_names_read_on_one_frame_each_re_label_nothing` (names must agree).
  - Measured vs main: `_simulate` (4 x 1000 seeds) and `_simulate_teams` (200 seeds, noise 0/0.2)
    identical per seed; QA's cross-speaker fuzz (1500 seeds, re-labels in 30%) loses nothing more,
    stores fewer extras; a short-reply fuzz (18k runs, 1-3 row panels) identical.
- Final lines wait in `MeetingTracker._unsaved` until written: the merger counts a line final
  once, so a failed write re-derived from frames would dedup away.
