---
paths:
  - "api/src/api/screen/**"
  - "api/tests/test_screen*.py"
  - "api/tests/test_turma_delivery.py"
  - "api/tests/test_pg_screen_live.py"
  - "api/tests/fixtures/screen/**"
  - "api/src/api/history.py"
  - "api/tests/test_history_meetings.py"
---

# Screen meeting captions (XERK-2505)

- Design and wire contract: `docs/meeting-captions.md` "Screen ingest in the api". Keep it,
  the schema defs and `contract/README.md` in step.
- `/ws/screen` messages stay outside the `/ws` `ClientMessage`/`ServerMessage` unions: adding
  them there widens the web/mobile switch types for a socket those clients never open.
- Per-source rules load with their files: `screen-merge`, `screen-merge-grow`,
  `screen-merge-finalise`, `screen-merge-split`, `screen-zoom`, `screen-teams`,
  `screen-teams-pane`, `screen-ocr`, `screen-delivery`, `screen-utterances`,
  `screen-device-tokens`. Only cross-cutting bullets belong here.
- schema.sql: a column added to a table this repo already created needs its own
  `ALTER ... ADD COLUMN IF NOT EXISTS`, even a table created in the same PR's earlier commit.
- Full frames are decoded only at the OCR'd size and ≤3840x2400 px: a 2.6 MB JPEG decoded to
  gigabytes. One socket feeds at most `MAX_WINDOWS` windows.
- Conversation ids are dashed uuid4 strings; history 404s any other shape (`is_valid_session_id`).
- The fixture is synthetic OCR output (no real capture may be committed: public repo). Re-tune
  thresholds on real frames once XERK-2506 can record them.
- Replay tests wait on the `screen.frames_handled` counter: frames are processed off the
  socket loop, and latest-frame-wins drops frames sent faster than they are handled.
- Alias editing (XERK-2606) is not a small endpoint: ingest stores the *aliased* name in
  `segments.speaker`, so aliases must also be keyed by shown names, and those keys collide.
  A per-request "name taken" check missed raw names in other meetings and lost a race (QA on
  XERK-2507 merged two people for good three ways); it needs one model + an org-level lock.
- Meeting titles come from the transcript (`topic.py`, at delivery), never the window's top band:
  on a Horizon window that band is the remote-desktop toolbar. Don't reintroduce `frame.title`
  storage. Tests: `test_screen_topic.py`, `test_an_untitled_meeting_is_titled_by_topic_once_before_delivery`.
- Frame timing is metered, not modelled (XERK-2722): admin `GET /metrics` `latency_ms` has
  `screen.caption_gap.<platform>` (ts gap between snapshots fed to one meeting's merger) and
  `screen.ocr_caption.<platform>` (caption-crop OCR time, successes only). Percentiles cover the
  last 256 samples process-wide (all windows, ~4 min of one meeting); read them during or right
  after a meeting: a pod restart clears them. Retune merge timing gates from these, not guesses.
  Tests: `test_the_gap_between_snapshots_fed_to_a_meeting_is_metered`,
  `test_a_frame_the_merger_fails_on_is_not_a_gap`.
- Read a past meeting's gaps from its `screen meeting ended: <id> (<platform>, caption gaps
  n= p50= p95= max= over_3s=[ dropped=])` log line in Loki, not `/metrics` (XERK-2724). Deploys restart
  the pod often enough that a meeting's in-memory samples are usually gone by the time anyone
  reads them. `over_3s` counts gaps past `merge._SAID_BETWEEN_MS`: retune that gate only if it
  is more than a few percent of `n`. Test: `test_a_meeting_logs_its_own_caption_gaps_when_it_ends`.
- Real frames for tuning come from the opt-in sampler (`samples.py`, `API_SCREEN_SAMPLE_DIR`,
  XERK-2705), never from git. On k8x point it under the `/data/audio` volume (the only persistent
  mount) and pull with `kubectl cp`; it saves client meeting screenshots, so set it for one
  meeting, then unset it and delete the files. `orgSiteKey` is client input: it is slugged.
  Tests: `test_screen_samples.py`.
  - Set `API_SCREEN_SAMPLE_ORGS` to the org being tuned: unscoped, the operator's daily Teams
    meetings fill the 2000-frame cap before the Zoom meeting starts (XERK-2820).
  - "No captions showing" with captions on: the `screen captions off:` log line counts caption
    crops received (before OCR), OCR failures, and crops yielding no block. 0 received = the
    client found no box; all empty = the parser refused each (sample that org's next meeting).
    Received minus the other two = crops that yielded blocks or whose parse raised (logged).
    Tests: `test_crops_failing_ocr_on_the_socket_are_counted_in_the_captions_off_line`.
- CPU work on frames (pane finding, parsing, merging) goes through `CPU_POOL.run(user, ...)`
  (`meetings.FairPool`, XERK-2737), never `run_in_executor`: jobs queue per user (household,
  owner) and users take turns, so crafted frames from several device tokens slow a live
  meeting by one job per busy user, not by every frame queued.
  - Accepted: latency still grows with the number of busy users; each needs an admin token.
  - Tests: `api/tests/test_screen_fair_pool.py`.
- `screen.cpu_wait` counts wait plus work, and also the event loop's own lag: a 77 s max with
  jobs measured in ms was the api pod starved of CPU (XERK-2798). Its CPU request was 10m with
  no limit; on a saturated node CFS weights it near zero (Prometheus: 0.03-0.07 cores while the
  node sat at 100%). Keep the request in ArgoCD `ai/tenir/deployment.yaml` at what a meeting needs.
- Client log lines (`screen.log`, `client_log.py`, XERK-2875) are client input: one token bucket
  per user, never per device token (mint/flood/revoke bypassed it, QA), and every line logged
  pays for itself, the "lines dropped" notice too (free, `dropped` bought a line per message).
  Validated strict from the raw text (lax took `"1"` for an int). Tests: `test_screen_client_log.py`.
- Client-sent strings (`orgSiteKey`, `orgName`, window titles) go into log lines with `%r`, never
  `%s`: a device token may send newlines and forge log lines (XERK-2879). Tests:
  `test_a_site_key_cannot_forge_a_frame_failed_log_line` and its siblings.
