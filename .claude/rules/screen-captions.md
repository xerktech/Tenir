---
paths:
  - "api/src/api/screen/**"
  - "api/tests/test_screen.py"
  - "api/tests/test_pg_screen_live.py"
  - "api/tests/fixtures/screen/**"
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
- `/ws/screen` sockets aren't registry sessions: `delete_user` calls `screen.ws.revoke_user`.
  Test: `test_deleting_the_user_closes_their_open_screen_socket`.
- Merge dedup keys on creation order, not time: a line re-reads a final only if nothing newer
  sits above it in the snapshot, each final absorbs one line per snapshot, and only the newest
  line may be extended. A time-only window dropped "okay"s repeated within it and duplicated
  lines OCR missed for longer. `_GONE_MS` (20 s) only splits a repeat after silence from a miss.
  Tests: `test_a_reply_said_again_after_someone_else_is_kept_at_any_gap`,
  `test_an_ocr_miss_of_many_frames_does_not_store_a_line_twice`.
- Only a line an earlier snapshot showed may raise that order floor: a garbled one-frame top
  line raising it re-stored every line below. A top line seen once that is a fragment of a
  known line is dropped, never one lower down (a short "yes" is real).
  Tests: `test_a_one_frame_ocr_glitch_above_the_box_stores_nothing_twice`,
  `test_a_short_reply_seen_once_below_the_top_is_kept`.
- schema.sql: a column added to a table this repo already created needs its own
  `ALTER ... ADD COLUMN IF NOT EXISTS`, even a table created in the same PR's earlier commit.
- Final lines wait in `MeetingTracker._unsaved` until written: the merger counts a line final
  once, so a failed write re-derived from frames would dedup away.
- Full frames are decoded only at the OCR'd size and ≤3840x2400 px: a 2.6 MB JPEG decoded to
  gigabytes. One socket feeds at most `MAX_WINDOWS` windows.
- Conversation ids are dashed uuid4 strings; history 404s any other shape (`is_valid_session_id`).
- Merge: check a header-less line against the recent finals before giving it `last_speaker`,
  or a line of the previous speaker still on screen is stored again under the new one.
  Test: `test_replayed_zoom_meeting_becomes_a_ready_conversation`.
- `is_name_like` keys on capitalisation: caption text is lower-case sentences. Loosening it
  turns caption lines into participants on full frames.
- The fixture is synthetic OCR output (no real capture may be committed: public repo). Re-tune
  thresholds on real frames once XERK-2506 can record them.
- Replay tests wait on the `screen.frames_handled` counter: frames are processed off the
  socket loop, and latest-frame-wins drops frames sent faster than they are handled.
