---
paths:
  - "desktop/**"
  - ".github/workflows/desktop.yml"
---

# Windows capture client (desktop/, XERK-2506)

- Design: `docs/meeting-captions.md` "Capture client"; operator install: `desktop/README.md`.
- Public repo: window titles and client names live only in the PC's `capture.json`
  (gitignored). Examples use "Horizon desktop 1" / "Org A", never a real client.
- Keep it dumb: no OCR or parsing on the PC. New behaviour that needs to read text goes in
  the api and reaches the client as a `/ws/screen` message (`screen.notice` is the pattern).
- Everything Windows-only is in `tenir_capture/win.py` (ctypes, WGC, plyer, schtasks) and
  omitted from coverage; the loop takes a `Platform`, so tests run on Linux with fakes and a
  real local WebSocket server. Keep new logic out of `win.py`.
- WGC's `frame_buffer` is a view of the mapped texture, valid only inside the callback: copy it.
- `Runner.run()` catches every exception but `TokenRejected`/`InvalidURI`: a logon task that
  dies needs the operator to log off. A handshake reset raises `InvalidMessage`, not OSError
  (QA D1). Test: `test_a_reset_during_the_handshake_is_retried_not_fatal`.
- A send can race the server's 1008 close; `session()` swallows `ConnectionClosed` and lets
  the close code decide. Test: `test_a_rejected_token_stops_the_client_even_while_it_is_sending`.
- `websockets` sync `connect()` must be used as a context manager (17.1 deprecates bare use).
- `plyer` picks its backend at run time: PyInstaller needs
  `--hidden-import plyer.platforms.win.notification` (in `build.sh`) or toasts silently fail.
- The version is stamped into `_version.py` by `build.sh`; "dev" never fails the
  `/status` min-version gate, so an unbuilt checkout always starts.
- Teams must crop the caption panel, never a fixed band. A bottom band failed real-OCR QA: idle
  windows sent tile initials and shared-document text that started meetings that never ended
  (the same band re-sent every tick), and busy stages hit tenir-ocr's 16-box too_dense limit.
- Teams finder levels come from a real capture: panel 0, camera-off tile ~10, stage ~31; it
  starts at the window's left edge and reaches the bottom. Synthetic windows mirror it in
  `tests/teams_synth.py` with fictional names and text only. Tests: `tests/test_teams_panel.py`.
- The panel's close X is its signature, checked as a lone cross (ink on both diagonals), and
  the panel is at most half the window tall. "Any small blob there" let a black slide with a
  slide number or logo in its corner start a never-ending meeting (QA delta 2).
  Tests: `test_no_caption_text_sends_nothing[12-0]`, `test_a_corner_mark_that_isnt_an_x_*`.
- Don't require the panel to span the window: an open Chat/People pane narrows it.
  Test: `test_captions_are_found_beside_an_open_side_pane`.
- Teams lines split on glyph cores (>120), not on any ink (>40): JPEG ringing from a remote
  session joins lines and the 8-line cap slips. Avatars are columns of round >40 blobs; in a
  small remote window lines merge, so `MAX_ENTRIES` (counted by avatars) also caps the crop.
- Lint with the pinned ruff (0.17.3): older local ruffs miss rules CI enforces (RUF059).
- Every `config.PLATFORMS` entry needs a `frames.FINDERS` entry; the runner indexes it directly.
  Test: `tests/test_frames.py::test_every_configurable_platform_has_a_finder`.
- Tests: `tests/test_runner.py::test_minimized_or_retitled_windows_pause_and_the_operator_is_told`,
  `tests/test_frames.py::test_caption_crops_go_only_while_a_caption_shows_with_a_full_frame_first`.
- `ruff` is pinned in `desktop/pyproject.toml`: unpinned, CI pulled a release with new default
  rules (BLE001, RUF100) and lint flipped between runs. Bump it deliberately.
