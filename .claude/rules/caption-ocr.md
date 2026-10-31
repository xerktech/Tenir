---
paths:
  - "scripts/caption_ocr/**"
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
- `find_caption_box` keys on centred white glyphs, not the box's dark fill: dark fill fails on
  camera-off meetings. Any change must keep the camera-off and no-caption tests green.
- Its thresholds trade recall against slide false positives (`FILL_DARK` 85 → 65 missed
  captions in 1,200; 100 → 9 false boxes in 1,620 slides). Re-run both sweeps when tuning.
- Synthetic frames must stay deterministic: seed every random source from the frame's rng
  (`Image.effect_noise` is unseeded and once made a box test flaky).
- Scoring pairs lines one-to-one and reports precision; a scorer without it hid the band crop's
  tile-name noise (QA finding D2).
