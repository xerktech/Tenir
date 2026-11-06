---
paths:
  - "api/src/api/screen/utterances.py"
  - "api/src/api/screen/turma.py"
  - "api/src/api/screen/topic.py"
  - "api/src/api/history.py"
  - "api/tests/test_screen_utterances.py"
  - "api/tests/test_history_meetings.py"
  - "api/tests/test_turma_delivery.py"
---

# Screen segments as sentences (`utterances.py`)

- Segments stay one per wrapped caption row; readers re-join them into sentences
  (`utterances.sentences`, XERK-2696): history detail/export, `turma.meeting_body`, `topic`.
  Joining in the merger would fight its row alignment. Any new reader of screen segments
  goes through it too. The history list's `segmentCount` still counts stored rows.
  - None-speaker rows never join (Zoom stores every off-roster speaker as None) and a pause
    over `MAX_GAP_MS` breaks: both spliced different people / minutes-apart text (QA).
  - One `_ends_at` test decides mid-row cuts and row-end breaks; separate rules split wraps
    after "Dr." / "e.g." (QA). Cuts per row are capped (`MAX_CUTS`): "A. A. ..." rows fanned
    out ~50x. Title checks scan only the chars before the period (a prefix rescan was O(n²)).
  - One closer set (`_CLOSERS`, ASCII + full-width) feeds every rule: each rule that carried its
    own copy lost a closer once (「」, then （）) across five QA rounds. ASCII " and ' open as
    often as they close, so they never block a cut and 'Really? "' orphans its quote (accepted:
    a "only closers left in the row" guard broke wrap parity, since a row isn't the text's end).
    Not » or ›: German opens quotes with them.
  - After any `_CUT` change, A/B one row vs the same text wrapped (QA kit `split2.py`).
  Tests: `test_screen_utterances.py`, `test_wrapped_caption_rows_reach_the_hub_as_sentences`.
