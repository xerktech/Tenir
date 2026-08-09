# QA guide

How to exercise Tenir — every component, front to back — without a pair of G2s,
a phone, a GPU, or anyone talking. Written during the first full QA pass
(XERK-236) so the next one starts where that one finished instead of
rediscovering the setup.

`CLAUDE.md` sets the standard: "it compiles" and "the tests pass" are not
verification. This file is about the rungs above that, plus the backlog of what
the first pass found and did **not** fix.

---

## TL;DR — a full pass from a cold checkout

```bash
# 0. one-time toolchain (see "Toolchain" for what's already on the usual box)
npm install                                   # workspaces: packages/*, even, mobile, web
uv venv --python 3.12 .venv && VIRTUAL_ENV=.venv uv pip install -e './api[dev]'
(cd veiller && bun install --frozen-lockfile)

# 1. the cheap gates
(cd api && ../.venv/bin/pytest -q)            # 465 tests, 85 % coverage gate
npm run typecheck && npm run test && npm run build
(cd veiller && bun run typecheck && bun test && bun run build)
node --test .github/scripts/tests/*.test.js   # release-pipeline scripts

# 2. a REAL running stack
cp .env.example .env                          # then set the secrets below
docker compose -f docker-compose.yml -f docker-compose.qa.yml up -d
curl localhost:18080/health

# 3. drive it
TENIR_BASE=http://127.0.0.1:18080 TENIR_USERNAME=<admin> TENIR_PASSWORD=<pw> \
  python scripts/functional_test.py
open http://127.0.0.1:18080                   # the real web UI, same origin

# 4. tear down — leaves nothing behind (named volumes, own project)
docker compose -f docker-compose.yml -f docker-compose.qa.yml down -v
```

---

## Standing up a stack

### The root compose file is the *deployed* shape

`docker-compose.yml` describes the host this project deploys to: bind mounts
under `C:/docker/tenir/...` and a local GPU running the Parakeet image. Those
paths are now `${TENIR_DATA_DIR:-C:/docker/tenir}`, so on Linux/macOS you can
point them somewhere real — but the GPU service still won't start without one.

### Use the QA overlay

```bash
docker compose -f docker-compose.yml -f docker-compose.qa.yml up -d
```

| | root file | with the overlay |
|---|---|---|
| project name | `tenir` | `tenir-qa` — can never adopt or `down -v` a real stack |
| storage | host bind mounts | named volumes, removed by `down -v` |
| STT | GPU `parakeet` service | `replicas: 0`; api runs `API_STT_BACKEND=stub` |
| ports | 8080 / 5432 / 4000 | **127.0.0.1**:18080 / 15432 / 14000 |

Two traps worth knowing:

- **`up -d`, never `up --build`.** `replicas: 0` stops the GPU service
  *starting*, not being *built* — `--build` will pull the multi-GB NGC NeMo base
  on a box with no GPU. Rebuild one service instead:
  `docker compose -f docker-compose.yml -f docker-compose.qa.yml build app`.
- **The `!override` tags on the port and volume lists are load-bearing.**
  Compose *merges* list values, so without them the QA stack also publishes 8080
  and collides with a real one.

### Running several QA stacks at once

This box is shared. Agents running the same overlay share its containers *and*
its volumes, and a `down -v` from any one of them wipes everyone's data — that
happened repeatedly during the first pass. Give each run its own project and
ports:

```bash
TENIR_QA_APP_PORT=18090 TENIR_QA_PG_PORT=15440 TENIR_QA_LITELLM_PORT=14010 \
  docker compose -p tenir-qa-<yours> -f docker-compose.yml -f docker-compose.qa.yml up -d
# …
docker compose -p tenir-qa-<yours> -f docker-compose.yml -f docker-compose.qa.yml down -v
```

Check `ss -ltn` first. Only stop containers whose name starts with your own
project prefix — this host also runs unrelated production containers.

### Env you must set

`.env` is git-ignored (and now `.dockerignore`d). The api **refuses to boot**
without a real `API_AUTH_SECRET` — non-blank and at least 32 characters — and
you need a bootstrap admin or there is no way to log in:

```bash
API_AUTH_SECRET=$(openssl rand -hex 32)
API_AUTH_ADMIN_USERNAME=qaadmin
API_AUTH_ADMIN_PASSWORD=<pw>
API_AUTH_ADMIN_HOUSEHOLD=default
LITELLM_MASTER_KEY=sk-tenir-qa
```

### Turning features on

Cues, translation and music are **off** in the base stack. Each has a model-free
`stub` backend — that is what you want, because it lights up the whole UI
surface with no GPU and no network:

```bash
API_CUE_BACKEND=stub API_TRANSLATION_BACKEND=stub API_MUSIC_BACKEND=stub \
  docker compose -f docker-compose.yml -f docker-compose.qa.yml up -d
```

**`API_STT_BACKEND=stub` produces captions from *silent* PCM**, so you can drive
a genuine end-to-end capture session with no microphone at all. That is the
single most useful fact in this document. The stub emits
`[stub segment — real STT lands in Phase 1]` about every 2 s.

### Running the api without docker

Faster than a compose cycle when iterating on backend behaviour:

```bash
API_AUTH_SECRET=$(openssl rand -hex 32) API_AUTH_ADMIN_USERNAME=a \
API_AUTH_ADMIN_PASSWORD=b API_PERSISTENCE_BACKEND=memory \
API_AUDIO_BACKEND=disk API_AUDIO_DIR=/tmp/qa-audio \
  .venv/bin/uvicorn api.main:app --port 8099
```

Defaults are model-free and in-memory, so it boots anywhere. It will **not**
serve the web UI — that is baked into the image at `/srv/web`.

---

## Driving each component

### API (`api/`)

- Suite: `cd api && ../.venv/bin/pytest -q`. 85 % line coverage is enforced;
  never lower it.
- End to end: `scripts/functional_test.py`. `TENIR_BASE` points it anywhere.
- The WS surface is `ws://<host>/ws?token=<bearer>`: `session.start`, then raw
  PCM as **binary** frames (16 kHz s16le mono), then `caption.partial` /
  `caption.final` come back. `contract/ws-messages.schema.json` is the source of
  truth — note `micSource` is `g2-microphone` | `phone-microphone` (not
  "glasses-…"), and a wrong enum returns a generic `could not parse message`
  with no field name.
- **`session.end` finalizes asynchronously** (audio flush + store write are
  offloaded to threads). A REST read issued the instant the socket closes will
  legitimately still see `status: "live"` — poll for `ready`. In a `TestClient`
  websocket, send a `ping` after `session.end` and wait for the `pong`: `ping`
  is handled even with no session, so the pong proves the loop got past the end.
- `active_sessions` on `/health` drops to 0 *before* `close()` finishes, so it
  is not a finalization signal.
- **Multi-household is not reachable through the API.** The users API only
  creates users in the caller's own household. To get a second household either
  INSERT into `households`+`users` directly, or (memory backend) mint tokens:
  `api.auth.tokens.issue_token(Principal(user_id=…, household=…, role="admin"),
  secret=<API_AUTH_SECRET>, ttl_seconds=…)`. Auth only checks the signature, so
  any household is forgeable if you hold the secret — the intended
  single-appliance model.
- After any hard kill, `select * from conversations where status='live'` should
  be empty on the next boot — the api sweeps them at startup now.

### Web UI (`web/`)

- The built SPA is served by the api on the same origin, so
  `http://127.0.0.1:18080` **is** the real app. Prefer it over `vite dev`: it is
  what ships.
- `VITE_API_HTTP=http://127.0.0.1:18080 npm run dev --workspace @tenir/web` for HMR.
- **Getting a browser**: `bunx playwright-core install chromium` from `veiller/`
  installs it, but the headless shell is missing system libs and there is no
  sudo on this box. Fetch them without root:
  ```bash
  mkdir libs && cd libs
  apt-get download libnspr4 libnss3 libasound2t64
  for d in *.deb; do dpkg-deb -x "$d" root; done
  export LD_LIBRARY_PATH=$PWD/root/usr/lib/x86_64-linux-gnu
  ```
  Then launch the full `chrome-linux64/chrome`, not the headless shell.
- Mic: launch with `--use-fake-ui-for-media-stream
  --use-fake-device-for-media-stream`. For *denied*, swap in
  `--deny-permission-prompts`; omit the fake device entirely for `NotFoundError`.
  To prove the mic is released, `addInitScript` a `getUserMedia` wrapper that
  stashes streams and read `track.readyState`.
- `page.goto(url)` where only the **hash** differs does not reload, and
  `adoptTokenFromUrl` runs at module load — `goto('about:blank')` first or the
  fragment test silently no-ops and looks like a pass.
- `context().setOffline(true)` does **not** kill an established WebSocket; the
  only reliable drop is stopping the app container.
- There is no REST route to create conversations — they only come from WS
  sessions. Seed hostile data straight into Postgres
  (`docker exec -i <pg> psql -U tenir -d tenir < seed.sql`).

### Even G2 glasses client (`even/`)

- `VITE_API_WS=ws://127.0.0.1:18080/ws npm run dev --workspace tenir-even`, then
  `npx @evenrealities/evenhub-simulator -g http://localhost:5173`.
- No hardware needed for the logic: only `EvenAppBridge` needs stubbing.
  `even/tests/controller.test.ts` has the shape — swap in the *real* `ApiClient`
  and you have a full loop against a live api.
- Audio goes in as `emit({ audioEvent: { audioPcm: <Uint8Array> } })`.
- `handleGesture` dedupes the same gesture within 200 ms; sleep ≥300 ms between
  synthetic gestures.
- Lens output: pass your own write fn to `LensTextWriter`. The cue/song/
  translation **body arrives inside the `rebuildPageContainer` payload**, not
  through `writer.set` — capture rebuild payloads too or the body looks empty.
- jsdom's WebSocket drops a frame sent in the same tick as `close()`; node's
  undici and the `ws` package do not. Never file a "frame lost on close" bug
  without re-running it on a second stack.

### Veiller miniapp (`veiller/`)

Self-contained: Bun, vendored SDK tarballs, its own lockfile — **not** an npm
workspace member, so root-level `npm run test` does not cover it.

The simulator harnesses in `veiller/sim/` walk the whole miniapp off-hardware
(`walkthrough.ts` = the lens over 20 steps, `phone-tour.ts` = the phone page in a
browser, `fake-server.ts` = a scriptable Tenir server). Both exit non-zero on a
finding.

```bash
export VEILLER_REPO=~/git/Veiller     # see the gotcha below
cd veiller && bun run build           # the harnesses run dist/, not src/
bun run sim/walkthrough.ts            # ~90 s; --step N for one step
bun run sim/phone-tour.ts --shots ./out
TENIR_BUNDLE=./tenir-veiller-vX.zip bun run sim/walkthrough.ts   # a released bundle
```

> **Gotcha:** they need the Veiller monorepo's `sdk/miniapp-simulator`, and
> `~/git/Veiller` may sit on a branch that predates it — the failure is
> `Could not find the Veiller miniapp simulator`. Fetch upstream `main`, or
> clone it somewhere scratch, and point `VEILLER_REPO` there.

`phone-tour.ts --shots` captures the frame *after* each step's teardown, so some
screenshots show no card at all. Read the step's assertions, not the picture.

### Android app (`mobile/`)

```bash
export JAVA_HOME=~/tools/jdk-17.0.20+8 ANDROID_SDK_ROOT=~/Android/Sdk ANDROID_HOME=~/Android/Sdk
export PATH=$JAVA_HOME/bin:$ANDROID_SDK_ROOT/platform-tools:$PATH
cd mobile/android && ./gradlew :app:assembleRelease --no-daemon   # the CI artifact, ~2 min
npx react-native start --host 127.0.0.1 --port 8081 &             # from mobile/, for debug
./gradlew :app:assembleDebug --no-daemon
emulator -avd turma228 -no-window -no-audio -no-snapshot -wipe-data -gpu swiftshader_indirect &
adb wait-for-device && adb install -r app/build/outputs/apk/debug/app-debug.apk
adb reverse tcp:8081 tcp:8081
adb shell am start -n com.tenir/.MainActivity
```

- From inside the emulator the host stack is `http://10.0.2.2:<port>`, never
  `localhost`. **Always type the scheme** in the Server field.
- `-no-audio` does not break the mic — `AudioRecord` initialises and the stub STT
  emits captions from the silence.
- **Dismiss the keyboard before tapping any button** (tap a neutral area, not
  `KEYCODE_BACK` — back exits the app). See the open item about
  `keyboardShouldPersistTaps` below; until it's fixed, the first tap is eaten.
- Use `uiautomator dump` for coordinates, not screenshot arithmetic.
- The arm-then-confirm delete expires in 4 s — issue both taps back to back.
- Reading the token (debug builds): `adb shell run-as com.tenir sqlite3
  /data/data/com.tenir/databases/RKStorage "select * from catalystLocalStorage"`.
- LogBox sits on the tab bar in debug builds and eats taps; dismiss it first.

---

## Techniques worth reusing

- **Time bombs.** `api/tests/test_cue_rss.py` pinned a fixture to a fixed
  `2026-07-24` date while the ingest pass prunes at `keep_days=14`, so it went
  red on `main` on 2026-08-08 with no code change. Any fixture that code
  compares against *now* must be stamped relative to now. Sweep with a faked
  clock:
  ```bash
  uv pip install libfaketime
  LD_PRELOAD=<…>/libfaketime.so.1 FAKETIME="@2028-06-15 08:00:00" \
    DONT_FAKE_MONOTONIC=1 pytest
  TZ=Pacific/Kiritimati npm run test   # and TZ=Pacific/Honolulu
  ```
  Both suites are clean under 2026-12/2027/2028 and UTC±14 as of this pass.
- **Mutation testing beats coverage.** Every escape found in this pass was in a
  file with high line coverage. Mutate a behaviour the tests claim to protect and
  check the suite goes red. Do it in a scratch copy (`cp -r` + symlink
  `node_modules`), or with an in-memory vitest transform plugin — never edit the
  repo.
- **Container logs** via Portainer (`~/.claude/bin/portainer-logs <node> <container>`).
- **`docker compose --dry-run up --build`** shows what a command *would* do
  before you pay for it.

---

## Open items — found by the first pass, not fixed

Triaged out of XERK-236 deliberately (the pass fixed every critical/high and the
security-relevant mediums). Each was reproduced; none is speculative.

### Feature-shaped gaps

- **History is hard-capped at 50 conversations with no pagination**, on web,
  Android, the glasses phone page and the miniapp. `api/src/api/history.py`
  supports `limit`/`offset`; no client passes them. The 51st-oldest session is
  unreachable and undeletable, which contradicts the README's "browse, search,
  replay, export and delete".
- **Transcript/audio export is not implemented on any front end.**
  `GET /conversations/{id}/export` exists and no client calls it. The README
  claims it.
- **No change-password anywhere** — no API route, no UI. An admin can only
  delete and recreate a user.
- **Conversation detail is not a route** on web: Back exits it, refresh loses
  it, it cannot be linked. `web/src/lib/route.ts` only routes tabs.

### Security follow-ups

- **The `#token=` handoff is still an unauthenticated write into a signed-out
  browser.** XERK-236 stopped it replacing an existing session; closing it fully
  needs a one-time, server-issued handoff code instead of a raw token in a URL.
- **The account password is stored in plaintext** on the glasses and the
  miniapp (`even/src/state/credentials.ts`,
  `veiller/.../TenirController.ts`), and the bearer token sits in plain
  AsyncStorage on Android despite `mobile/src/secureStorage.ts` claiming
  "EncryptedSharedPreferences-class storage". Deliberate today — it powers the
  silent re-login — but the comment is wrong and the stored secret is the
  *account* password, not a scoped refresh token.
- **The api image runs as root.** Dropping the uid needs an entrypoint that
  fixes ownership of the existing root-owned `API_AUDIO_DIR` bind mount before
  dropping privileges, plus a release note — see the note in `api/Dockerfile`.
- **`LITELLM_MASTER_KEY` has a shipped default with no boot refusal**, unlike
  `API_AUTH_SECRET`.
- **No security headers on the served SPA** — no CSP, `X-Content-Type-Options`,
  `X-Frame-Options`/`frame-ancestors` or `Referrer-Policy`.
- **`cors_origins` defaults to `*`** and compose never overrides it.
- **A deleted user's token is revoked, but there is no deny-list**, so a stolen
  token stays valid for its lifetime while the account exists.

### Correctness / UX

- **Android back button exits the app from every screen** — no `BackHandler`
  anywhere in `mobile/src`.
- **Every button inside a mobile `Screen` needs two taps while the keyboard is
  open** — `mobile/src/ui/components.tsx` `<ScrollView>` lacks
  `keyboardShouldPersistTaps`.
- **Landscape on Android shows zero captions** — the Live stage is clipped
  behind the tab bar.
- **A stale `tenir.sessionId` makes a fresh recording join an old
  conversation** after a crash; it has no expiry.
- **False biometric disclosure** on the mobile sign-in screen
  (`packages/client-core/src/disclosures.ts`) claims voiceprints are stored;
  there is no such code, and it points at a Privacy tab deleted in `6791d4a`.
  `mobile/README.md` and `docs/design-language.md` still describe that tab.
- **Long unbroken text breaks the web layout** — no `overflow-wrap` anywhere in
  `web/src/styles.css`.
- **A web search with no hits renders "No conversations yet"**, the empty-state
  for having recorded nothing.
- **Toasts kill each other** — `web/src/lib/toast.tsx` never clears the pending
  timeout, so a toast raised soon after another lives ~100 ms. Errors get missed.
- **Repeating a search does nothing** and there is no refresh affordance
  (`useAsync(..., [search])` with an unchanged term).
- **A dropped connection mid-recording looks identical to a healthy one** on
  web; audio in the gap is silently dropped.
- **422 validation errors render as `[object Object]`** — `detail` is typed as
  `string` but FastAPI returns a list.
- **A live cue's transcript anchor doesn't follow the bounded segment window**
  in `even/` and `veiller/` (the past-cue anchors do).
- **`ApiClient.dispatch` throws on a JSON `null` text frame.**
- **Two clients on one session id: the first is silently orphaned.**
- **A veiller "Web app" button emits `https://http://…`**
  (`veiller/src/ui/main.ts`).
- **A 401 on the miniapp's proxied REST path never triggers the silent
  re-login**, though the WS path heals itself.
- **The server URL is persisted before the login is attempted**, so a typo
  sticks as the boot server.
- **Audio-store failure is invisible to the client**: the session completes
  normally and the WAV is silently never stored.

### Test-suite gaps

- **`veiller/sim/**`, every `*.test.ts`, `build.ts` and `scripts/pack.mjs` are
  typechecked by nothing** (`veiller/tsconfig.json` excludes them), and the
  simulator harnesses run in no CI job despite `sim/README.md` calling them CI
  checks.
- **No lint anywhere in the JS/TS workspaces** — no ESLint config, no `lint`
  script. The `eslint-disable react-hooks/exhaustive-deps` in
  `web/src/lib/hooks.ts` is decorative.
- **Mobile has no behavioural screen tests**: several suites assert regexes
  against JSX *source text*, so `{hasContent ? (` → `{false ? (` in `Live.tsx`
  (no captions ever render) passes 163/163. Web has real render tests; mobile
  should too.
- Confirmed escapes worth pinning: web logout never clicked
  (`App.test.tsx` only asserts the button exists); web search term never
  asserted; `even/src/lens/layout.ts` `CAPTION_LINES` can exceed the screen;
  veiller's queued-cue-after-song promotion can be deleted with all 115 tests
  and all 20 walkthrough steps still green.
- **`web/tests/App.test.tsx` mocks the entire `@tenir/client-core` module**, so
  the real REST client, 422 handling and sliding renewal are unexercised at the
  web layer.
- **`even/tests/selectableText.test.ts` and `textSize.test.ts` resolve paths
  from `process.cwd()`**, so they fail unless vitest runs from `even/`.

---

## Don't QA against production

The live hosts are listed in the workspace `~/git/CLAUDE.md` (TrueNAS, backups,
the GPU/model box, UniFi, Home Assistant). Read-only commands and dry runs only
against those unless told otherwise.

The dev box also runs unrelated real containers (`Tenir-Ollama-Cue`,
`watchtower`, `autoheal`, `portainer_agent`, `cAdvisor`). Scope every docker
command to your own QA project, never `docker system prune`, and tear down what
you stood up.

---

## Toolchain notes

Already installed on the usual box: Node 22, Python 3.14 (+ `uv`), Bun, Docker
29 / Compose v5, `gh`, Terraform, the Android SDK with AVD `turma228`, JDK 17.

- The api targets Python **3.11+** and CI pins 3.11. The system Python 3.14
  cannot `python -m venv` here (`ensurepip` fails); use `uv venv --python 3.12`.
- **`make` may not be installed** even though `make gen` is the documented
  contract workflow. Run the two commands from the `Makefile` directly. CI's
  `contract-drift` job regenerates and diffs, so drift is caught regardless.
- The GPU Parakeet server (`10.10.10.22:9401`) was **down** throughout the first
  pass, and this box has no NVIDIA toolkit — so the real STT path, real cues,
  real translation and real music ID went unexercised. Everything was driven on
  the `stub` backends. Plan for that.
