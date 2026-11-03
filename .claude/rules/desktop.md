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
- Tests: `tests/test_runner.py::test_minimized_or_retitled_windows_pause_and_the_operator_is_told`,
  `tests/test_frames.py::test_caption_crops_go_only_while_a_caption_shows_with_a_full_frame_first`.
- Teams crops come from the box the api sends (`screen.region`, `caption_pane.PaneCrop`), never
  from client-side geometry: the captured window is a whole remote desktop with Teams floating
  in it. Six QA rounds of a pixel panel finder never converged, and it found nothing on real
  remote-desktop frames. Each window gets its own `PaneCrop` (`FINDERS` holds factories).
  - The region is dropped when the window is lost and on every new socket; the api re-sends.
    Tests: `test_a_teams_window_crops_only_where_the_api_said_and_forgets_it_with_the_window`,
    `test_a_reconnect_forgets_the_pane_until_the_api_says_it_again`.
  - The crop ends at the box's lowest glyph row (a pane fills from the top, then scrolls) and
    opens on a blank row; a box no longer mostly black sends nothing. Tests: `test_caption_pane.py`.
  - The lowest text is looked for inside the pane's margins (8% left, 4% right) and in columns
    lit on < 90% of rows: a window border or corner caught at the box's edge, or the scrollbar
    thumb, made every real 0.6-0.7x crop blank (QA). Not 50%: an avatar column is lit ~75%.
  - The blank row a crop opens on is checked on every column: skipping lit columns moved real
    crops 9 px, OCR then read initials into the names, and a real meeting recorded nothing (QA).
- `ruff` is pinned in `desktop/pyproject.toml`: unpinned, CI pulled a release with new default
  rules (BLE001, RUF100) and lint flipped between runs. Bump it deliberately.
