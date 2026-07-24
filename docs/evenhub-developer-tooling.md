# Even Hub Developer Tooling — Reference

> Distilled from `even-realities/everything-evenhub` (the official AI skill suite,
> 13 skills) plus the SDK/CLI/simulator packages it documents. This is the
> authoritative "what the platform actually lets us do" reference for building
> `tenir`. Captured 2026-06-17.

## 0. Mental model

```
Your app (HTML + TypeScript, Vite)  ──►  EvenAppBridge (SDK)  ──►  Even companion app (Flutter WebView host)  ──►  G2 glasses (BLE)
```

- An Even Hub app is **a plain web app running inside a Flutter WebView** on the
  phone. No native code, no special framework — Vite + vanilla TS is the norm.
- All glasses I/O goes through the **`@evenrealities/even_hub_sdk`** bridge.
- The phone is always in the loop; the glasses are a BLE-tethered display + mic +
  IMU + touchpad. **No camera, no speaker on G2.**

## 1. Toolchain packages

| Package | Purpose | Notes |
|---|---|---|
| `@evenrealities/even_hub_sdk` | The bridge SDK (v0.0.10) | UI containers, audio, IMU, storage, events |
| `@evenrealities/evenhub-cli` | CLI (`evenhub`, v0.1.11) | `login`, `init`, `qr`, `pack` |
| `@evenrealities/evenhub-simulator` | Desktop simulator (v0.7.1) | renders 576×288, feeds mic audio, HTTP automation API |
| `@evenrealities/pretext` | Pixel-accurate font measurement | matches firmware LVGL layout; line height **27px** |
| `evenhub-templates` (degit) | Starter scaffolds | `minimal`, **`asr`**, `image`, `text-heavy` |

Scaffold paths:
- `/quickstart <name>` → blank Vite+TS+SDK app.
- `/template <name> --asr` → **mic→STT pipeline already wired** (our starting point).

## 2. The display (canvas)

- **576 × 288 px**, origin top-left, 4-bit greyscale (**16 shades of green**, 0–15).
- White = bright green; **black = off = transparent** (real world shows through).
  **No background fill.**
- **Max 12 containers/page**: ≤ **8** text/list + ≤ **4** image.
- **Exactly one** container must have `isEventCapture: 1` (it receives all input).
- No z-index (declaration order = stacking), no CSS/flexbox/DOM, no animations,
  no text alignment, no font size/bold/italic. Single baked-in LVGL font, not
  monospaced. Unsupported glyphs are silently skipped.
- ~**400–500 chars** fill the screen; line height **27px**; list item height **40px**.

## 3. SDK API surface (the parts that matter)

### Init & order
```ts
const bridge = await waitForEvenAppBridge()        // always await this
await bridge.createStartUpPageContainer(container)  // EXACTLY ONCE at startup
// only after success: audioControl / imuControl / rebuild / events
```
`audioControl` and `imuControl` fail unless `createStartUpPageContainer` returned
`success (0)`.

### Rendering
| Method | Use | Cost |
|---|---|---|
| `createStartUpPageContainer(c)` | one-shot initial layout. returns `0=ok,1=invalid,2=oversize,3=oom` | once |
| `rebuildPageContainer(c)` | full redraw (layout/structure/list changes). **flickers** | ~expensive |
| `textContainerUpgrade(c)` | in-place text update, **no flicker**, max **2000 chars** | cheap, fast |
| `updateImageRawData({...})` | push pixels to an image container. **must be serial** | ~0.5–2s/frame |
| `shutDownPageContainer(mode)` | `0`=exit now, `1`=confirm dialog | — |

Container types: `TextContainerProperty`, `ListContainerProperty` +
`ListItemContainerProperty` (firmware-managed scroll, can't update in place — must
rebuild), `ImageContainerProperty` (placeholder until `updateImageRawData`; no
event capture — pair with a transparent full-screen text container for events).

Text content limits: `createStartUpPageContainer` 1000 / `textContainerUpgrade`
**2000** / `rebuildPageContainer` 1000. Use `contentOffset:0, contentLength:0` for
full replacement.

### Audio (the core of a transcription app)
```ts
await bridge.audioControl(true)                  // start mic (needs startup ok first)
const off = bridge.onEvenHubEvent(e => {
  if (e.audioEvent) {
    const pcm = e.audioEvent.audioPcm            // Uint8Array
    // PCM, 16 kHz, signed 16-bit little-endian, MONO
  }
})
await bridge.audioControl(false); off()          // stop + unsubscribe
```
Simulator delivers **100ms chunks** (3200 bytes / 1600 samples) per event.
Requires the `g2-microphone` (glasses mic) or `phone-microphone` permission.

### Input & events — `bridge.onEvenHubEvent(cb)`
Event has one of: `listEvent`, `textEvent`, `sysEvent`, `audioEvent`.
- **Clicks/double-clicks on a TEXT container arrive as `sysEvent`, not `textEvent`.**
  Only scroll gestures fire `textEvent`. (#1 source of bugs.)
- `OsEventTypeList`: `0 CLICK, 1 SCROLL_TOP, 2 SCROLL_BOTTOM, 3 DOUBLE_CLICK,
  4 FOREGROUND_ENTER, 5 FOREGROUND_EXIT, 6 ABNORMAL_EXIT, 7 SYSTEM_EXIT, 8 IMU`.
- **Protobuf zero-omission**: any field equal to 0/false/"" arrives as `undefined`.
  Always `?? 0` (e.g. single click `eventType` is `undefined`; list index 0 is `undefined`).
- Distinguish glasses vs ring via `sysEvent.eventSource` (`1`=right arm, `2`=ring,
  `3`=left arm).
- Canonical exit: on `DOUBLE_CLICK (3)` call `shutDownPageContainer(1)`; do cleanup
  in the `SYSTEM_EXIT (7)` / `ABNORMAL_EXIT (6)` handlers (not before the dialog).

### Device / user / storage
- `getDeviceInfo()` → model/sn/status (battery, isWearing, isCharging, isInCase);
  `onDeviceStatusChanged(cb)` for live updates.
- `getUserInfo()` → uid/name/avatar/country.
- **Persistence: `setLocalStorage(k,v)` / `getLocalStorage(k)`** (strings). Browser
  `localStorage`/IndexedDB do **NOT** survive restarts in this WebView — SDK
  storage is the only reliable option. Chunk large data across keys (~50k chars).

### IMU
`imuControl(true, ImuReportPace.P500)` → `sysEvent.imuData {x,y,z}` filtered by
`eventType === IMU_DATA_REPORT (8)`. (Not available in simulator.)

### Background-state persistence (important for long sessions)
When the phone backgrounds, the host **snapshots JS state and migrates to a
headless WebView**, then restores on resume. If you don't register exporters your
app **resets to initial state** every background→foreground.
```ts
import { setBackgroundState, onBackgroundRestore } from '@evenrealities/even_hub_sdk'
setBackgroundState('session', () => ({ ...mutableState }))   // plain JSON only
onBackgroundRestore('session', (s) => { state = { ...state, ...(s as any) } })
```
Register at module init, before `onEvenHubEvent`. Snapshot copies (not refs), cast
+ `??` fallback on restore, no Maps/Dates/class instances.

## 4. Performance & reliability rules (BLE is the bottleneck)

- **Serialize every bridge call** — `await` each before the next. Concurrent
  render + storage calls can crash the connection.
- **Never send images concurrently**; frames cost ~0.5–2s, no compression/delta —
  design turn-based, no multi-FPS loops.
- Prefer **`textContainerUpgrade`** for anything live (captions, counters) — text
  is far faster than image and flicker-free.
- **Wrap BLE calls in a timeout** (`Promise.race`, few-second cap) — one flaky hop
  can hang ~30s.
- **Debounce `setLocalStorage`** (shares the BLE link); flush on `FOREGROUND_EXIT`.
- `createStartUpPageContainer` **once**; everything else rebuilds/upgrades.
- Always unsubscribe listeners and stop `audioControl`/`imuControl` on exit.

## 5. Build, package, deploy

`app.json` manifest (validated by `evenhub pack`):
```json
{
  "package_id": "com.example.tenir",   // reverse-domain, lowercase, NO hyphens, ≥2 segments
  "edition": "202601",                            // must be exactly this
  "name": "Tenir",                     // ≤ 20 chars
  "version": "0.1.0",                             // x.y.z semver
  "min_app_version": "2.0.0",
  "min_sdk_version": "0.0.10",
  "entrypoint": "index.html",
  "permissions": [],                              // array of objects (NOT a map)
  "supported_languages": ["en"]                   // en,de,fr,es,it,zh,ja,ko
}
```
Permissions: array of `{name, desc, whitelist?}`. Valid names: `network`
(whitelist required), `location`, `g2-microphone`, `phone-microphone`, `album`,
`camera`.

**CORS gotcha:** whitelisting a domain in `app.json` does **not** bypass browser
CORS. The remote API must also send `Access-Control-Allow-Origin`. For dev, use a
Vite proxy; for prod, use an API with CORS or your own proxy (e.g. Cloudflare
Worker). Public CORS proxies are unreliable.

Pipeline:
```bash
npm run build
npx evenhub pack app.json dist -o tenir.ehpk   # -c checks package_id availability
# submit .ehpk to the Even Hub developer portal — or automate it:
#   npm run publish:hub -w tenir-even -- --next-version x.y.z --changelog '...'
# (see even/README.md "Publish to the Even Hub dev portal")
npx evenhub qr --url http://<ip>:5173                      # live test on real glasses (hot reload)
```

## 6. Simulator & testing

```bash
evenhub-simulator -g http://localhost:5173                 # -g = glow (closer to hardware look)
evenhub-simulator <url> --automation-port 9898             # HTTP automation API
```
Automation API (`http://127.0.0.1:9898`): `GET /api/ping`,
`GET /api/screenshot/glasses` (RGBA PNG — alpha>0 = lit pixel, keep RGBA),
`GET /api/screenshot/webview`, `GET /api/console?since_id=N`, `DELETE /api/console`,
`POST /api/input {action: up|down|click|double_click}`. Allow ~4s after launch for
SDK init.

Simulator ≠ hardware: `onDeviceStatusChanged` not emitted, `eventSource` hardcoded
to `1`, `imuData` always null, font rendering is an approximation, image memory
limits not enforced. **Validate list scrolling, IMU, device-status, and memory on
real glasses before shipping.**

## 7. Community resources worth mining

- `nickustinov/even-g2-notes` — Unicode glyph tables, error codes, reference apps.
- `fabioglimb/even-toolkit` (`even-toolkit`) — React components, pixel-art icons,
  `useGlasses` hook, action bar, event mapper, canvas renderer, pagination helpers.
- Figma: Even Realities Software Design Guidelines (public).
- Discord: https://discord.gg/Y4jHMCU4sv

## 8. The `everything-evenhub` skills (installable in Claude Code / Codex)

Install: `/plugin marketplace add even-realities/everything-evenhub` then
`/plugin install everything-evenhub@everything-evenhub`.

| Tier | Skill | Use |
|---|---|---|
| 1 | `quickstart` / `template` / `build-and-deploy` | scaffold & ship |
| 2 | `glasses-ui` | containers/text/lists/images, layout patterns |
| 2 | `handle-input` | gestures, event routing, lifecycle, exit |
| 2 | `device-features` | audio, IMU, device/user info, storage |
| 2 | `test-with-simulator` / `simulator-automation` | run & automate the sim |
| 2 | `font-measurement` | pixel-accurate sizing via `pretext` |
| 2 | `background-state` | survive background/foreground migration |
| 3 | `sdk-reference` / `cli-reference` / `design-guidelines` | lookups |
