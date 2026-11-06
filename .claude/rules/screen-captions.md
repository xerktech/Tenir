---
paths:
  - "api/src/api/screen/**"
  - "api/tests/test_screen*.py"
  - "api/tests/test_turma_delivery.py"
  - "api/tests/test_pg_screen_live.py"
  - "api/tests/fixtures/screen/**"
  - "api/src/api/history.py"
  - "api/tests/test_history_meetings.py"
---

# Screen meeting captions (XERK-2505)

- Design and wire contract: `docs/meeting-captions.md` "Screen ingest in the api". Keep it,
  the schema defs and `contract/README.md` in step.
- `/ws/screen` messages stay outside the `/ws` `ClientMessage`/`ServerMessage` unions: adding
  them there widens the web/mobile switch types for a socket those clients never open.
- Per-source rules load with their files: `screen-merge`, `screen-zoom`, `screen-teams`,
  `screen-teams-pane`, `screen-ocr`, `screen-delivery`, `screen-utterances`,
  `screen-device-tokens`. Only cross-cutting bullets belong here.
- schema.sql: a column added to a table this repo already created needs its own
  `ALTER ... ADD COLUMN IF NOT EXISTS`, even a table created in the same PR's earlier commit.
- Full frames are decoded only at the OCR'd size and ≤3840x2400 px: a 2.6 MB JPEG decoded to
  gigabytes. One socket feeds at most `MAX_WINDOWS` windows.
- Conversation ids are dashed uuid4 strings; history 404s any other shape (`is_valid_session_id`).
- The fixture is synthetic OCR output (no real capture may be committed: public repo). Re-tune
  thresholds on real frames once XERK-2506 can record them.
- Replay tests wait on the `screen.frames_handled` counter: frames are processed off the
  socket loop, and latest-frame-wins drops frames sent faster than they are handled.
- Alias editing (XERK-2606) is not a small endpoint: ingest stores the *aliased* name in
  `segments.speaker`, so aliases must also be keyed by shown names, and those keys collide.
  A per-request "name taken" check missed raw names in other meetings and lost a race (QA on
  XERK-2507 merged two people for good three ways); it needs one model + an org-level lock.
- Meeting titles come from the transcript (`topic.py`, at delivery), never the window's top band:
  on a Horizon window that band is the remote-desktop toolbar. Don't reintroduce `frame.title`
  storage. Tests: `test_screen_topic.py`, `test_an_untitled_meeting_is_titled_by_topic_once_before_delivery`.
