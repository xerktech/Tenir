# even — Even G2 glasses client

The phone+glasses client. A Vite + TypeScript Even Hub app that captures mic
audio, streams PCM to the api over a WebSocket, and renders live captions on
the G2 lens. Scaffolded in the spirit of the `asr` template: the mic→STT seam
is the WS client, replacing the template's HTTP STT stub.

Like the web client, the api is a **required, user-editable setting**: you point
Tenir at your own self-hosted instance on the companion page and then sign in
(auth is always required). The chosen server URL and bearer token are persisted
and shared with the on-lens app, so the glasses connect to the same instance.
Everything beyond server + sign-in (session history, system status, user
management) lives in the server-hosted web app, which the companion page links
to. `VITE_API_WS` is only a build-time *seed* for dev / first run — a saved
choice always wins.

## Layout

```
src/
  main.ts              boot order, lens render loop, input + lifecycle
  config.ts            resolves the effective api URL (saved → seed → localhost); configures client-core
  state/settings.ts    required, user-editable server URL: validate + persist (shared with the lens)
  lens/layout.ts       576x288 HUD: status line / caption band
  audio/capture.ts     audioControl + PCM extraction (16kHz s16le mono)
  state/persist.ts     session persistence across background/foreground (Even SDK)
  companion/           slim off-lens page: server setting + sign-in + web-app link
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

The api URL is a **runtime, user-editable setting** (companion → Server), so the
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

- **Single click** — pause / resume the caption stream.
- **Double click** — exit (confirm dialog).

## Notes

- Live text uses `textContainerUpgrade` only (flicker-free); never a rebuild.
- `createStartUpPageContainer` runs exactly once at boot.
- Set the prod api host via `TENIR_API_HOSTS` at pack time (it fills
  `app.json`'s `network` whitelist) — and ensure the api returns CORS headers
  for explicit origins (`API_CORS_ORIGINS`); whitelisting alone does not bypass
  CORS, and a wildcard origin disables credentialed CORS.
- Generated types come from `/contract`; run `make gen-ts` at the repo root to
  refresh `packages/contract/src/messages.ts`.
