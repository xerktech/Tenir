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
- Merge dedup is bounded: a final absorbs a line only while seen on screen (`_GONE_MS`) and
  at most once per snapshot. Unbounded, every repeated "okay" was dropped (QA, XERK-2505).
- Final lines wait in `MeetingTracker._unsaved` until written: the merger counts a line final
  once, so a failed write re-derived from frames would dedup away.
- Full frames are decoded only at the OCR'd size and ≤4096² px: a 2.6 MB JPEG decoded to
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
