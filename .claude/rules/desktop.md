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
- Teams sends a fixed bottom band every tick, never None: deciding "no caption" needs OCR,
  which stays on the api (its Teams parser yields nothing for a band without caption text).
  Test: `tests/test_frames.py::test_the_teams_finder_sends_the_bottom_band_under_the_ocr_cap`.
- Every `config.PLATFORMS` entry needs a `frames.FINDERS` entry; the runner indexes it directly.
  Test: `tests/test_frames.py::test_every_configurable_platform_has_a_finder`.
- Tests: `tests/test_runner.py::test_minimized_or_retitled_windows_pause_and_the_operator_is_told`,
  `tests/test_frames.py::test_caption_crops_go_only_while_a_caption_shows_with_a_full_frame_first`.
- `ruff` is pinned in `desktop/pyproject.toml`: unpinned, CI pulled a release with new default
  rules (BLE001, RUF100) and lint flipped between runs. Bump it deliberately.
