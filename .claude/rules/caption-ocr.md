---
paths:
  - "scripts/caption_ocr/**"
  - "desktop/tenir_capture/caption_box.py"
  - "docs/meeting-captions.md"
---

# Caption OCR (XERK-2504)

- The repo is public: never commit client names, window titles or real captures. Titles go in a
  local `windows.json` (gitignored); docs say "Horizon desktop 1", not the client.
- PaddlePaddle 3.3.1 on CPU crashes with `ConvertPirAttribute2RuntimeAttribute not support`
  unless `enable_mkldnn=False`; that slow path is what the latency numbers measured.
- PP-OCR's detector drops whole lines on a tight caption crop; keep `_detector_margin`'s border.
- Paddle-mobile's cost is recognition, not detection: skipping detection saves nothing.
- Paddle CPU threads don't speed up a single caption crop; parallelise across windows.
- Tesseract needs white-on-dark captions inverted and `--psm 6` on a crop; thresholding loses.
- `find_caption_box` lives in `desktop/tenir_capture/caption_box.py` (the capture client ships
  it); `caption_bench.py` imports it from there, so the bench measures the code that runs.
- Real Zoom (2026 capture) draws captions in a panel: avatar, a dim grey name line, then the
  text. `with_speaker_line` adds that name line to the crop (the avatar fuses it into the text
  block, so the finder alone dropped every speaker). It takes one dim line only: a white line
  above is the previous entry. 0 of 240 synthetic overlay boxes change. Test:
  `desktop/tests/test_caption_panel.py`.
- `crop_box` keeps `AVATAR_ROOM` px left of the box for the avatar: the api reads it to tell
  a speaker from a docs page's grey line (XERK-2700). Don't trim the crop back to the glyphs.
- `find_caption_box` keys on centred white glyphs, not the box's dark fill: dark fill fails on
  camera-off meetings. Any change must keep the camera-off and no-caption tests green.
- Its thresholds trade recall against slide false positives (`FILL_DARK` 85 → 65 missed
  captions in 1,200; 100 → 9 false boxes in 1,620 slides). Re-run both sweeps when tuning.
- Synthetic frames must stay deterministic: seed every random source from the frame's rng
  (`Image.effect_noise` is unseeded and once made a box test flaky).
- Scoring pairs lines one-to-one and reports precision; a scorer without it hid the band crop's
  tile-name noise (QA finding D2).
- `caption_replay.py` (XERK-2731) replays sampler windows through the live `MeetingTracker`, never
  a re-implementation: its numbers decide merge tuning. Its test must keep matching the socket
  path's stored lines (`test_replaying_the_teams_fixture_stores_what_the_socket_stores`).
- The sampler keeps no band read of the Teams pane heading: replay uses the full frame's own
  heading line, and when OCR missed it caption lines join the roster and rows are lost (QA).
  Read `full_frames_without_pane`; non-zero → re-run with `--ocr` (real tenir-ocr).
- `compare()` pairs rows by the longest in-order alignment, never a greedy search: best-match
  over all later rows paired a reworded line with its repeat far ahead, and a bounded window
  lost sync for good after 16 unmatched rows (both QA, everything after counted as lost).
- Its chain maximises pairs first, own-speaker pairs only as the tie-break: speaker-blind it
  invented speaker changes, same-speaker-only it hid real misattribution (both QA).
- Replay must read the sampler's own file names (`<ts>-<run>-<n>-<region>`): a hand-built test
  window once hid a format the harness couldn't load. Keep
  `test_a_window_the_sampler_itself_wrote_replays_like_the_fixture` writing via `samples.save`.
