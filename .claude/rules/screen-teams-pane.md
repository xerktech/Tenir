---
paths:
  - "api/src/api/screen/teams_pane.py"
  - "api/src/api/screen/ws.py"
  - "api/tests/test_screen_teams_pane.py"
---

# Teams caption pane location (`teams_pane.py`)

- Teams' caption pane is located on full frames by the api (`ws.locate_pane`, XERK-2608): pixels
  first (`teams_pane.candidates`: a divider of luma 40-170 with black above, black margins
  below), then OCR of only the strip above each divider for "Live Captions" (`pane_of`).
  - Never find the heading in the full frame's OCR: over a text-dense desktop every strip is
    too_dense and the heading is never read (QA, real capture: 25 of 25 strips).
  - Pane rows: the left OR right margin strip >= 0.6 black. Whole-row darkness (0.5) had no
    headroom on real panes (0.36 at JPEG q60) and shrank the box onto stale entries (QA).
  - A downscaled window splits the divider over two rows (25 + 54, 17 + 41): a top row >= 15
    plus the row under it is the line, with no cap on the sum (an avatar under it bleeds in).
    An avatar under a 1 px line lifts it to 143, hence the 170 cap (QA, real 0.79x). Each
    threshold here lost a real capture at some scale when tightened: re-sweep all captures at
    1.0/0.9/0.79/0.7/0.6 x q80/q60 after any change.
  - Near rows of one divider (JPEG ringing above it) are one candidate, the wider run kept.
  - The heading strip is 0.15 of the pane's width tall (48-120 px): 0.3 took in the toolbar
    and came back too_dense at 0.6x. Candidates whose strip is >= 0.45 black with >= 20 glyph
    pixels in its left half are OCR'd first, then the blackest: wider dark windows under grey
    lines pushed the pane out. Four such windows with heading-like text over black still can.
  - The pane is located alongside the frame's own OCR, not after it: that read took up to
    43 s of the 45 s timeout (QA), and a failed one still tells the client its region.
  - `cropHeight` is 6 heading heights (~7 caption lines); pane lines from full-frame OCR may
    be missing, and a heading-height pitch overshot to 13 of 16 boxes (QA).
  - `tell_pane` sends `screen.region` on change only, per socket, records it after the send,
    and an OCR error keeps the client's region. Lines inside the pane are never participants.
  - The bottom caption panel has no heading or divider: unsupported, captions dock right.
  - Tests: `test_screen_teams_pane.py`.
