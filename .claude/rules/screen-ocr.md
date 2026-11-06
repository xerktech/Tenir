---
paths:
  - "api/src/api/screen/ocr.py"
  - "api/src/api/screen/ws.py"
  - "api/tests/test_screen.py"
  - "api/tests/test_screen_teams.py"
---

# Screen OCR client (`ocr.py`)

- tenir-ocr (ocr/README.md) refuses images over 0.5 Mpx and returns no width/height:
  `ocr.HttpOcrClient` reads the size from the JPEG header and cuts full frames into
  overlapping strips, dropping lines that touch a cut edge (a fragment like "Priya Ra" would
  otherwise become a participant). Change ocr/README.md in the same PR as the contract.
- A too-dense full-frame strip is halved and re-read in the api (`ocr.halves`, `MAX_SPLITS`),
  not by raising tenir-ocr's 16-box cap: that cap is the per-request time bound, and on caption
  crops `too_dense` still means "a slide, store nothing" (XERK-2610). Tests:
  `test_a_too_dense_full_frame_strip_is_halved_and_re_read`,
  `test_a_frame_of_slides_stops_splitting_within_the_budget`,
  `test_a_7x7_gallery_frame_is_halved_down_to_row_height_on_every_strip`.
  - `MIN_SPLIT_HEIGHT` (128) keeps 4K strips (125 px) whole: halving them doubled a 4K slide
    frame to 76 requests / 54 s, past the 30 s full-frame cadence (QA).
  - Never cap halving by wall clock: a 15 s cap lost a gallery row whenever OCR was slow (QA).
- `HttpOcrClient.read(full=True)` (ws passes the frame's region) splits full frames under the
  pixel cap too; a caption crop is never halved, or a slide crop would be stored as captions.
  Tests: `test_a_small_window_frame_is_split_but_a_caption_crop_never_is`; `replay` asserts
  the `full=` ws passed per region (`StubOcr.full`).
- A too-dense caption crop is split only on Teams, and only once (`read(split_once=)`, XERK-2652):
  a real Teams pane crop (11-12 lines + avatars) passes tenir-ocr's 16 boxes. On Zoom a dense
  crop is a slide, so never split it. Test:
  `test_a_dense_teams_caption_crop_is_halved_once_and_both_halves_read`.
- The halves read a line in their overlap differently ("CO Okafor, Chidi" / "Okafor, Chidi"),
  so exact-text `_dedupe` kept both and a doubled header misattributed lines (real Paddle, QA).
  `_drop_rereads` dedupes by box overlap.
  Test: `test_a_line_both_halves_read_differently_is_kept_once`.
