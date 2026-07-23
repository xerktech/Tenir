# even — Even G2 glasses client

The phone+glasses client. A Vite + TypeScript Even Hub app that captures mic
audio, streams PCM to the api over a WebSocket, and renders live captions on
the G2 lens. Scaffolded in the spirit of the `asr` template: the mic→STT seam
is the WS client, replacing the template's HTTP STT stub.

It is a **single page** on purpose (XERK-82): the lens renders through the SDK
bridge while the phone side of the same WebView shows the login page and, once
signed in, the server-hosted **Tenir web UI embedded full-bleed** — the phone
companion *is* the web UI. (Navigating the WebView to a separate page would
unload the lens app, so nothing here ever navigates.)

Like the web client, the api is a **required, user-editable setting**: the
login page collects your self-hosted server address plus the household
username + password, styled to match the web UI's login. The server URL, the
bearer token, **and the credentials** are persisted in the *device* store —
the SDK's `setLocalStorage`/`getLocalStorage`, the only storage that survives
app restarts in this host (browser `localStorage` does not) — so everything is
entered once, ever: later launches sign in from cache, and an expired token
re-logs-in silently. `VITE_API_WS` is only a build-time *seed* for dev / first
run — a saved choice always wins.

## Layout

```
src/
  main.ts              bridge/dev boot + wiring: lens surface, login page, transcript strip
  lens/controller.ts   session state machine: click start/stop, listening/clock ticker (XERK-85)
  config.ts            initConfig(storage): effective api URL (saved → seed → localhost) + device token store
  phone/login.ts       phone-side login page + embedded web UI (token handed over via #token= fragment)
  phone/transcript.ts  phone-side live transcript strip mirroring the running session (XERK-85)
  state/storage.ts     KeyValueStorage: BridgeStorage (device, survives restarts) / BrowserStorage (dev)
  state/settings.ts    required, user-editable server URL: validate + persist (shared with the lens)
  state/credentials.ts cached username/password + silent re-login when the token expires
  lens/layout.ts       576x288 HUD: status line / clock / caption band, fit-to-band trimming
  audio/capture.ts     audioControl + PCM extraction (16kHz s16le mono)
  state/persist.ts     session persistence across background/foreground (Even SDK)
app.json               Even Hub manifest (permissions, languages)
```

The api REST + WS client and auth live in the shared `@tenir/client-core`
package; the generated WS types in `@tenir/contract`.

## Develop

Install the workspace once from the **repo root** (`npm install`), then:

```bash
VITE_API_WS=ws://localhost:8080/ws npm run dev --workspace tenir-even   # :5173
```

Run the api alongside (`docker-compose.yml`, or the api directly).

### In the simulator

```bash
npx @evenrealities/evenhub-simulator -g http://localhost:5173
```

### On real glasses (hot reload)

```bash
npx evenhub qr --url http://<your-lan-ip>:5173
```

## Build & package

```bash
npm run build                       # tsc typecheck + vite build -> dist/

# Pack for the simulator (whitelist defaults to localhost):
npm run pack                        # gen app.packed.json (localhost) + evenhub pack -> .ehpk

# Pack for a real deployment — the Even Hub host ENFORCES the network whitelist, so
# the packed build must list its actual api host(s) or the lens WSS/HTTPS
# connection is blocked and the caption loop never connects:
TENIR_API_HOSTS=api.example.com npm run pack
# (comma-separate multiple hosts). `npm run appjson` writes app.packed.json alone.
```

The committed `app.json` keeps the localhost dev whitelist; `scripts/gen-app-json.mjs`
writes a deploy-ready `app.packed.json` (gitignored) from it + `TENIR_API_HOSTS`,
and `pack` packs that.

### BYO self-hosting (arbitrary user servers)

The api URL is a **runtime, user-editable setting** (phone login → Server), so the
wearer points the app at *their own* server — a host we can't enumerate at pack
time. Because the Even Hub host enforces the whitelist, the user's URL must fall
inside it. To allow any user-supplied host, pack with a wildcard:

```bash
TENIR_API_HOSTS='*' npm run pack            # https://* + wss://*  (any host)
TENIR_API_HOSTS='*.example.com' npm run pack # restrict to one domain's subdomains
```

A wildcard may be combined with explicit hosts (comma-separated). `evenhub pack`
accepts a wildcard manifest, but the **Even Hub host runtime and the submission /
QA review are separate gates** — confirm a wildcard build is honoured on real
glasses (and passes review) before relying on it for production.

## Lens controls

- **Single tap** — start a new session when idle. While one records, single
  taps do NOTHING (a brushed temple must not end a recording).
- **Double tap (recording)** — a bordered popup box (its own container, added
  via `rebuildPageContainer`) with **Continue** (default, top) / **Exit
  session**, drawn over the caption band: the rows the box covers are masked
  (exactly what an opaque popup would hide) while the rows around it keep
  flowing. Swipe to move the highlight, single tap to confirm, another double
  tap dismisses (same as Continue). Exit session stops the session — the api
  finalizes and stores it — and the lens idles at "tap to start". Should the
  popup-page rebuild ever fail on the host, the menu falls back into the
  caption band itself, so the wearer is never stranded inside a session.
- **Double tap (idle / signed out)** — exit the app (confirm dialog).

No VISIBLE container ever captures input (the OS plays its scroll animation
on whatever container captures a scroll gesture — it hit the session text
first, then the clock): every page carries an invisible full-band "touch"
overlay (content: one space) at the caption band's geometry, which captures
all gestures — the bounce animation moves content nobody can see. Gestures
arrive on the `sysEvent` and `textEvent` channels; both feed one handler,
deduped per gesture type.

Once signed in, the top-right corner shows the current time (12-hour) — on
the idle "ready" page and while recording alike. While a session records, the
status line (top left) reads `listening` with moving dots, and the caption
band keeps only the tail of the transcript that fits on screen — old
text falls off the top, and with nothing overflowing there is nothing to
scroll. The phone page mirrors the running session's transcript in real time
in a strip above the embedded web UI.

## Notes

- Live text uses `textContainerUpgrade` only (flicker-free); never a rebuild.
- `createStartUpPageContainer` runs exactly once at boot — and FIRST, before
  any storage round-trip, so a slow BLE hop can't leave the lens blank.
- BLE is fragile (XERK-82): every bridge call is timeout-bounded
  (`withBleTimeout`), and all lens text writes go through the serialized
  `LensTextWriter` — concurrent bridge calls can crash the connection, which
  presents as the app closing itself.
- Set the prod api host via `TENIR_API_HOSTS` at pack time (it fills
  `app.json`'s `network` whitelist) — and ensure the api returns CORS headers
  for explicit origins (`API_CORS_ORIGINS`); whitelisting alone does not bypass
  CORS, and a wildcard origin disables credentialed CORS.
- Generated types come from `/contract`; run `make gen-ts` at the repo root to
  refresh `packages/contract/src/messages.ts`.
