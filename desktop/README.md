# tenir-capture — Windows meeting-caption capture client (XERK-2506)

Capture only, with no web or Android counterpart (a deliberate parity exception, see
`CLAUDE.md`). It watches the meeting windows on the operator's Windows PC with Windows
Graphics Capture, crops each frame to the meeting's caption box, and streams it to Tenir's
`/ws/screen`. Tenir does the OCR and parsing and stores each meeting as a conversation with
speakers. Design and wire format: [`docs/meeting-captions.md`](../docs/meeting-captions.md).

## Install on the meeting PC

1. Download `tenir-desktop-v<version>.exe` from the latest Tenir release and save it as, for
   example, `%LOCALAPPDATA%\Tenir\tenir-capture.exe`.
2. Write `%APPDATA%\Tenir\capture.json` (start from [`capture.example.json`](capture.example.json)).
   Use `tenir-capture.exe --list-windows` to find the exact window titles. Titles name
   clients, so this file stays on the PC and is never committed.
   - `title`: the exact title, or a substring that matches exactly one window. A title that
     matches nothing pauses that window. Example: Devolutions RDM renames its window when
     another tab is in front, so an exact RDM title pauses capture until the session tab is
     back in front.
   - `org`: the label the meeting's org has in Turma (Brief → Meetings → Tenir labels). By
     default it is the org's name in lowercase with dashes (e.g. `yprime`); lowercase letters,
     digits, `-` and `_`, at most 40 characters. Use one org per window. A label Turma doesn't
     know isn't lost: Tenir keeps retrying the meeting, and the label shows up under Tenir labels
     for you to map to an org.
   - `orgName`: the org's display name in Tenir.
   - Older configs with `orgSiteKey` (a Turma site key) still work; `org` wins when both are
     set (a bad `org` is an error, never a fall-back to `orgSiteKey`). Switching a window from
     its site key to a label starts a new org key in Tenir: speaker names learned so far and the
     history org filter stay with the old key, under which the earlier meetings remain.
   - `platform`: `zoom` or `teams`. In Teams, turn on live captions and keep them docked on
     the right (or popped out): the bottom caption panel isn't read.
3. Pair it once: `tenir-capture.exe --pair` asks for your Tenir username and password and
   saves a screen-only device token to `%APPDATA%\Tenir\device-token`. The token works for 365
   days, and only on `/ws/screen`. It is listed under this PC's hostname in the web Devices tab
   (Android: Settings → Devices), where revoking it disconnects this PC alone.
4. Start it at logon: `tenir-capture.exe --install-startup` creates the Task Scheduler task
   "Tenir capture". If Windows answers "Access is denied", run it from an elevated prompt.
   `--uninstall-startup` removes the task.

Logs go to `%APPDATA%\Tenir\logs\capture.log`; the INFO and warning lines are also sent to
the Tenir server's log (tagged `client`, with org names, never window titles or captions), so a
failed meeting can be diagnosed there. Exit codes: 2 bad or missing config or token,
3 device token refused (run `--pair` again), 4 client older than the server's minimum.

## What it does

- Every `1/fps` seconds it crops each window's newest frame to the caption box. The server
  sets the rate: 0.2 fps idle, 1 fps during a meeting. A caption crop is sent only while a
  caption is on screen. A full window frame is sent every `fullFrameSeconds`, and right before
  the first caption after none, so the server can read the participant names.
- It reads windows while they are hidden behind other windows. A minimized window can't be
  read: the client pauses it and shows a desktop notification.
- When the server sees a Zoom meeting with no captions for a couple of minutes, it sends a
  notice and the client shows a desktop notification.
- On start it reads `GET /status` → `clients.desktop.minVersion` (`API_DESKTOP_MIN_VERSION`).
  If this build is older, it shows a notification and exits. If `/status` can't be reached, it
  starts anyway.
- It reconnects with backoff (1 s up to 60 s). If the server refuses the token (close 1008),
  it shows a notification and exits.

## Develop

```bash
cd desktop && pip install -e '.[dev]' && pytest     # runs on any OS; 85% coverage gate
desktop/build.sh 0.0.0                               # on Windows (Git Bash): dist/tenir-capture.exe
```
