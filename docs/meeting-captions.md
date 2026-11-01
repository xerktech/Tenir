# Meeting captions: capture and OCR decision (XERK-2504)

Spike for epic XERK-2503 (meeting captions to tickets). Tenir watches the operator's four
remote-meeting windows on their central Windows PC (three Omnissa Horizon desktops and one
Devolutions RDM session tab), reads the meeting captions off them, and posts the conversations
on to Turma. This page records what the spike measured and decided. The harness is in
[`scripts/caption_ocr/`](../scripts/caption_ocr/README.md). Client and window names stay out of
this public repo; they live in the tickets and in the operator's local `windows.json`.

## Decision

| Question | Decision |
|---|---|
| OCR engine | **PaddleOCR, PP-OCRv5 mobile** (`PP-OCRv5_mobile_det` + `en_PP-OCRv5_mobile_rec`) |
| Fallback engine | Tesseract 5, only if CPU is short (loses speaker names on small windows) |
| CPU or GPU | **CPU**, in the `tenir-ocr` service on a k8x node (XERK-2512); no GPU |
| CPU budget | 4 cores, dropping stale frames; ~6.4 cores to never drop one (see *Sizing*) |
| Frame size | Capture each window at native size; never downscale before OCR |
| Crop | Client side: bottom band → find the caption box → upscale to 1080p-equivalent height |
| What crosses the wire | Only the caption-box crop (684–1025 × 31–145 px at 1080p-equivalent on the synthetic set), only when a caption is on screen |

Why:

- **Speaker names decide the engine.** A wrong or missing name misattributes an action item.
  Tesseract drops or garbles the first word of a line (the speaker's first name) on ~12% of
  lines. Paddle-mobile read every caption line within the 95%-similarity bar, added no extra
  lines, and got the speaker right on 99.3% of lines.
- **Paddle-server loses.** It is 3x slower than mobile and *less* accurate on English captions
  (its recogniser is the multilingual model).
- **The box crop halves Paddle's cost and removes the noise.** ~1.0 vs ~1.6–2.6 core-seconds per
  caption frame against the bottom band, and the band crop also OCRs tile names and toolbars
  (band precision 0.59–0.67: a third or more of its lines are not captions). With no caption on screen
  the finder returns nothing and the frame costs ~14 ms instead of a full OCR pass.
- **CPU is enough, so the GPU stays free.** The GPU nodes serve the STT, translation and cue
  models; a few CPU cores are cheaper than contending for them.
- **Crop on the PC, OCR in the cluster.** The capture client (XERK-2506) already has the frame;
  cropping there cuts upload from ~25 MB/s of raw 1080p frames to a few small PNGs per second,
  and the model lives in one place (`tenir-ocr`) instead of on the operator's desktop.

## Sizing

Paddle-mobile costs ~0.92 core-seconds per caption frame at p50 and ~1.6 at p95 (CPU time
matched wall clock within 2%; threads don't help one frame, so parallelise across windows).
At 1 fps x 4 windows with someone talking in all four meetings at once:

- p50: ~3.7 cores; p95: ~6.4 cores to keep up with every frame.
- Frames without a caption cost ~14 ms, so quiet meetings are nearly free.
- A caption stays on screen for several seconds and is re-read on the next frame, so a worker
  that falls behind can drop the stale frame and take the newest without losing text.

So: 4 workers on 4 cores, latest-frame-wins per window. Measured on a 35 W i7-11700T shared
with other jobs; re-measure on the `tenir-ocr` node before fixing its CPU request (XERK-2512).

## Measurements

90 synthetic Zoom frames, 30 per window scale (1.0, 0.75, 0.5 of 1080p), JPEG quality 60-85 to
mimic a remote-desktop codec. A third show camera-off tiles (near-black, white names, the hard
case for the box finder); a fifth have no caption on screen. Single thread, PaddlePaddle 3.3.1,
PaddleOCR 3.7.0, Tesseract 5.3.4. See *Caveats* for why synthetic.

- **Line accuracy**: share of caption lines read at ≥95% character similarity.
- **Precision**: share of OCR'd lines that are a caption line (extra lines count against it).
- **Speaker accuracy**: share of lines whose `Name:` prefix is read right (case-insensitive,
  otherwise exact).
- Truth and OCR lines are paired one-to-one. Latency is per caption frame, wall clock, leaving
  out the first OCR call of each run (model warm-up). Raw output: `caption_bench.py --synth 30`.

Caption-box crop (the decided strategy):

| Engine | Frames | Line | Precision | Speaker | p50 | p95 |
|---|---|---|---|---|---|---|
| Paddle-mobile | scale 1.0 | 1.000 | 1.000 | 1.000 | 866 ms | 1645 ms |
| Paddle-mobile | scale 0.75 | 1.000 | 1.000 | 0.979 | 960 ms | 1565 ms |
| Paddle-mobile | scale 0.5 | 1.000 | 1.000 | 1.000 | 937 ms | 1427 ms |
| Paddle-mobile | camera-off | 1.000 | 1.000 | 1.000 | 910 ms | 1396 ms |
| Paddle-mobile | all 90 | 1.000 | 1.000 | 0.993 | 924 ms | 1598 ms |
| Tesseract | scale 1.0 | 1.000 | 1.000 | 0.841 | 162 ms | 196 ms |
| Tesseract | scale 0.75 | 1.000 | 1.000 | 0.915 | 179 ms | 211 ms |
| Tesseract | scale 0.5 | 0.935 | 0.935 | 0.891 | 172 ms | 208 ms |
| Tesseract | camera-off | 1.000 | 1.000 | 0.936 | 174 ms | 208 ms |
| Tesseract | all 90 | 0.978 | 0.978 | 0.883 | 174 ms | 208 ms |

Bottom band upscaled, same frames: Paddle-mobile line 0.98–1.00, precision 0.59–0.67, p50
1.6–2.6 s (and ~0.9 s on frames with no caption at all); Tesseract line 0.87–0.98, precision
0.65–0.67.

Earlier frame set (video tiles only, every frame captioned, scorer without precision), kept for
the engines it ruled out:

| Engine | Crop | Line | Speaker | p50 |
|---|---|---|---|---|
| Paddle-server | caption box | 0.71–0.91 | 0.74–0.90 | 2.9–3.2 s |
| Paddle-server | full window | — | — | ~38 s (19 frames, then stopped) |
| Paddle-mobile | full window | 1.000 | 1.000 | 1.4–4.1 s |
| Tesseract | full window | 0.48–0.85 | 0.41–0.90 | 0.2–0.6 s |
| RapidOCR (PP-OCRv5 mobile, onnxruntime) | band, upscaled | 1.000 | 0.984 | ~2.3 s |
| RapidOCR | caption box, no border | 0.929 | 0.929 | ~2.5 s |

Dead ends, so nobody repeats them:

- **Paddle with oneDNN (MKLDNN) on.** PaddlePaddle 3.3.1 raises
  `ConvertPirAttribute2RuntimeAttribute not support` on CPU; `enable_mkldnn=False` is required.
  oneDNN is Paddle's fast CPU path, so a version where it works may be much faster — the first
  thing `tenir-ocr` (XERK-2512) should try before sizing its CPU request.
- **A tight crop straight into PP-OCR.** Its detector missed whole lines on 37 px crops (5 of
  156); a 32 px black border around the crop recovered every one.
- **Recognition only, skipping detection.** Feeding line strips straight to the recogniser was
  no faster: recognition, not detection, is the cost.
- **RapidOCR.** Same models on onnxruntime, no faster than Paddle on this CPU.
- **More threads.** 4 threads gave Paddle-mobile no speedup on one frame.
- **Hard-threshold binarisation for Tesseract.** Much worse than plain inversion
  (white-on-dark → dark-on-light) with `--psm 6`.
- **Finding the box by its dark fill alone.** Fails on camera-off meetings, where the video is
  as dark as the box. The finder keys on white glyphs centred in the window, then checks the
  fill is mostly dark so dark-on-white slide text is rejected (`find_caption_box`).

## Capture (Windows Graphics Capture)

Not yet run: it needs the operator's central PC. `scripts/caption_ocr/capture_windows.py` is the
probe. On that PC, list the window titles, put the four in a local `windows.json`
(`{"slug": "exact title"}`, never committed), then:

```powershell
pip install windows-capture pillow
python capture_windows.py --list
python capture_windows.py --windows windows.json --label occluded       # behind others
python capture_windows.py --windows windows.json --label minimized      # all four minimized
python capture_windows.py --windows windows.json --label rdm-dashboard  # RDM Dashboard in front
```

It saves frames per window and appends a row per window to `captures/capture-report.jsonl`
(frames received, size, mean luminance, black share). Exit code 0 means every window delivered
non-black frames. A title that matches more than one window is refused, not guessed. Record:

| Window | Client | Occluded | Minimized | RDM Dashboard tab |
|---|---|---|---|---|
| Horizon desktop 1 | Omnissa Horizon | _pending_ | _pending_ | n/a |
| Horizon desktop 2 | Omnissa Horizon | _pending_ | _pending_ | n/a |
| Horizon desktop 3 | Omnissa Horizon | _pending_ | _pending_ | n/a |
| RDM session tab | Devolutions RDM (docked) | _pending_ | _pending_ | _pending_ |

Expected, to be confirmed: WGC reads occluded windows (DWM still composes them); a minimized
window stops delivering frames because DWM stops rendering it. RDM draws only the active tab, so
with the Dashboard in front the session isn't drawn; its window title may also change and then
match nothing, which the probe reports as "no visible window matches".

## Screen ingest in the api (XERK-2505)

The capture client (XERK-2506) streams to `/ws/screen`; the api OCRs, parses and stores. Code in
`api/src/api/screen/`, contract in `contract/ws-messages.schema.json` (`ScreenFrameHeader`,
`ScreenCadence`).

**Wire format: hybrid.** The spike sends only caption-box crops, but speaker validation needs
the tile names and the active-speaker border, which are outside the crop. So frames carry
`region`:

- `caption`: the caption-box crop, at the cadence's `fps`, only while a caption is on screen.
- `full`: the whole window, every `fullFrameSeconds` and right before the first caption crop
  after none. Read for participant names, the green border and the meeting title.

OCR cost stays near the spike's sizing: a full frame (~5 strips) every 30 s per window adds
about one strip-read in six to the caption crops at the meeting cadence.

**Frames.** Binary: 4-byte big-endian header length, `ScreenFrameHeader` JSON
(`windowTitle`, `orgSiteKey`, `orgName`, `platform`, `ts`, `region`), then the JPEG. Frames
are held in memory only; nothing but derived text is stored. Per window and region only the
newest frame waits for OCR; a newer one replaces it (`screen.frames_dropped`).

**Cadence.** On connect the server sends the idle default (`API_SCREEN_IDLE_FPS`, 0.2); when a
window's meeting starts it sends that window `API_SCREEN_MEETING_FPS` (1), and the idle rate
again when it ends. Full frames every `API_SCREEN_FULL_FRAME_SECONDS` (30).

**Auth.** A device token: `POST /auth/device-token` while logged in (built-in or OIDC) mints a
`screen`-scoped token for the caller (`API_AUTH_DEVICE_TOKEN_TTL_SECONDS`, 365 days). It opens
only `/ws/screen`, is refused on every other route, never slides, and dies with the user.
Meetings it records are owned by that user. Login and OIDC tokens can't open `/ws/screen`.

**tenir-ocr** (`ocr/`, contract in `ocr/README.md`): `POST {API_OCR_URL}/v1/ocr` takes one
image of at most 0.5 Mpx and answers its lines with boxes; `too_dense` (a slide) has none.
- Caption-box crops fit and are sent whole.
- A full window frame doesn't: the api (`ocr.py`) cuts it into overlapping horizontal strips
  under the cap, drops lines touching a cut edge (the neighbouring strip reads them whole),
  and shifts boxes back into window coordinates. A 1080p frame is ~5 strips, ~5 OCR-seconds,
  hence full frames every 30 s. A strip that is too dense contributes nothing.
- 413 / 503 / timeout (45 s) drop that frame; the next one retries.
- Empty `API_OCR_URL` keeps `/ws/screen` off (close 1013).

**Zoom parser** (`zoom.py`):

- Participants: name-shaped lines (capitalised, ≤5 tokens, or an email) on the tiles, minus
  the title band, toolbar and caption box; `(Host)`-style suffixes dropped.
- Active speaker: the tile label with Zoom-green border pixels around its bottom-left corner;
  none when two tiles qualify. Used to break ties between near-equal roster matches and
  counted when it disagrees with the caption header (`screen.speaker_border_mismatch`).
- Caption blocks: a header is a line (or a `Name:` prefix) matching a roster name, exact or
  fuzzy (≥0.86). Any other line is caption text. Before any full frame there is no roster, so
  a name-shaped line followed by text is a header with no speaker.
- Speaker aliases (`speaker_aliases`, per member and org) rename tile names, e.g. an email to a
  first name. Only the meeting owner's aliases apply; one member's renames never show in
  another member's meetings (XERK-2606).

**Merge** (`merge.py`): each caption snapshot is a window onto the end of the transcript, so it
is aligned in order against the recent known lines (an edit-distance alignment). Known lines
above the window have scrolled off; snapshot lines below the last aligned one are new; an
unmatched line above it is an OCR glitch and is ignored. A line is final when another
speaker's block follows it, when unchanged for `API_SCREEN_LINE_STABLE_SECONDS` (3), or at
meeting end; a final that grows while still the newest line is extended in place (upsert by
segment id). The same words said again align as a new line; a line still on screen never
does. Known limits: the same words after a silence that cleared the box within 20 s are
dropped, and a short phrase repeated back to back by one speaker can lose a copy when OCR
also drops a line.

**Meetings** (`meetings.py`): one per (user, `orgSiteKey`), kept in the process across
reconnects. Starts when captions first appear, ends `API_SCREEN_MEETING_END_SECONDS` (180) after
the last one (a 5 s sweep), and becomes a `ready` conversation with `source='screen'`,
`org_site_key`, `org_name`, `platform`, `meeting_title`, `participants` and `segments.speaker`.
A graceful shutdown finishes open meetings; a crash leaves them to the boot stale sweep.
Deleting the user closes their open screen sockets.

## Capture client (XERK-2506)

`desktop/` is the Windows client on the central PC (install and config: `desktop/README.md`).
It stays dumb: it captures, crops and sends; the api reads and decides.

- **Capture**: Windows Graphics Capture by window handle (`windows-capture`), newest frame
  copied at most every 0.2 s. Windows come from a local `capture.json`: title (exact, or a
  substring matching one window) → `orgSiteKey`, `orgName`, `platform`. A title matching
  nothing pauses that window (RDM with another tab in front); an ambiguous one is refused.
- **Crop**: `tenir_capture/caption_box.py` is the spike's `find_caption_box`, moved; the
  bench imports it from there. Only `zoom` has a finder; Teams windows are skipped until
  XERK-2515 adds one.
- **Cadence**: as *Screen ingest* above; the client caps a server's ask at 5 fps and one full
  frame per 5 s, and treats a faster cadence as due at once.
- **Notices**: a minimized window is paused with a desktop notification (WGC gets no frames
  from it). `screen.notice` `captions_off` comes from the api: a Zoom toolbar (3+ of its
  buttons in the bottom band) seen in the last 10 min, and no caption for 2 min while no
  meeting is open; repeated every 15 min. The toolbar, not tile names, is the evidence,
  because a desktop's icon labels are name-shaped too. A caption read in the last 10 min silences
  it: a captioned meeting that goes quiet isn't "captions off". The cost: captions turned off
  mid-meeting, or a next meeting without them, is warned ~12 min in, not 2 (OCR can't tell
  silence from captions off).
- **Version gate**: `GET /status` carries `clients.desktop.minVersion`
  (`API_DESKTOP_MIN_VERSION`); an older client notifies and exits, an unreachable `/status`
  doesn't block start.
- **Release**: `tenir-desktop-v<version>.exe`, a PyInstaller one-file exe built by
  `release.yml` on `windows-latest`; `desktop.yml` builds it and smoke-tests WGC on every PR.

## Delivery to Turma (XERK-2513)

A ready screen meeting is posted to the Turma hub, which proposes work items from it. Code in
`api/src/api/screen/turma.py`; the wire contract is Turma's `.claude/rules/contract-meetings.md`
(`POST /api/orgs/<orgSiteKey>/meetings`, bearer = the hub's `TURMA_MEETINGS_TOKEN`).

- On when `API_TURMA_URL` and `API_TURMA_MEETINGS_TOKEN` are both set. Off, meetings wait
  `pending` and are sent once it is turned on.
- State on the conversation: `delivery_state` `pending` (from creation; a screen row with none,
  from before delivery existed, reads pending) → `sent` or `failed`, with `delivery_error` in
  the hub's words (while pending: the last retry's reason).
- Each pass looks at every pending meeting: after a retryable refusal the rest of that org waits
  for the next pass, and an unreachable hub ends the pass. One org's backlog never blocks another.
- The body fits the hub's limits: lines cut to 2000 characters, at most 10000 lines, and 1 MiB
  both on the wire and by the hub's per-org transcript measure (sanitized segments, each with a
  `speaker`). Past that the earliest lines are sent and the `sent` row's `delivery_error` says
  how many.
- A config that can't work (URL or token alone, or a URL that isn't http(s) with a host) keeps
  delivery off, logs why, and shows red on `/status` at once (no grace window: waiting can't fix
  it).
- A meeting is tried as soon as it turns ready (the tracker wakes the loop), else on a 10 s poll,
  which also picks up meetings the boot stale sweep closed.
- Exactly once at the hub: Tenir retries until a 200 `{ok: true}`, and the hub is idempotent on
  `meetingId` (the conversation id), so a request lost to a hub restart is just sent again.
- 400/401/403/413 fail at once; anything else (unreachable, 5xx, 404 while the restarted hub's
  hosts re-register, 409, 429) retries from 5 s, doubling to `API_TURMA_RETRY_MAX_SECONDS`
  (300), until `API_TURMA_GIVE_UP_SECONDS` (1 day) after the meeting ended.
- A meeting failed for a reason a config fix can change (401/403 from a wrong token, or giving
  up during a long hub outage) goes back to `pending` when Tenir starts (XERK-2605). To resend
  them, fix the token or the hub and restart Tenir. 400/413 and empty meetings stay failed.
- `/status` shows a "Meetings to Turma" light: red for failures of meetings that ended in the
  last 7 days, yellow while any wait, green when all were delivered. Counts and errors only.

## Caveats

- **Synthetic frames, not the operator's screenshot.** The screenshot was not reachable from the
  benchmarking host (nothing attached to XERK-2504/2503). Synthetic frames use a clean font with
  no ClearType fringing or caption animation, so absolute accuracy is optimistic; the engine
  ranking and the CPU cost carry over. Re-run on real crops:
  `python scripts/caption_ocr/caption_bench.py --images <dir with truth.json> --out <dir>`.
- **The box finder is Zoom-specific and tuned on synthetic frames.** It assumes a centred box of
  white text at Zoom's default caption size (`BOX_MAX_LINE`); retune on real captures, and again
  if the operator enlarges captions. Teams draws captions differently (XERK-2515) and needs its
  own finder; three of the four meetings are Teams.
- **A dark slide with white text centred low on screen reads as a caption.** It is the same
  pixels as a caption box; the speaker-label parser (XERK-2505) is the next filter. Dark text on
  light slides is rejected (the box fill must be mostly dark): 1 false box in 1,620 random
  white, mid-grey and coloured slides, against 0 missed boxes in 1,200 captioned frames.
- **Latency is on a 35 W mobile-class CPU** shared with other workloads.
