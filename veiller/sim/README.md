# Simulator harnesses

Two ways to walk the Tenir miniapp end to end without a phone, a pair of G2s, or
anyone talking — both driven by the Veiller miniapp simulator, which runs the
built bundle against the app's real host and display pipeline.

| File | What it covers |
|---|---|
| `walkthrough.ts` | The glasses. 20 steps over sign-in, capture, captions, cues, translation runs, songs, the Continue/Exit menu, reconnect, token expiry, backgrounding, history, sign-out — printing the lens after each one. |
| `phone-tour.ts` | The phone page, in a real browser: the DOM, the `veiller.request` round-trips, and the live cards — with the lens shown alongside, since the two must agree. |
| `fake-server.ts` | A Tenir server that speaks the real REST + `/ws` contract but takes its cues from the test, so a translation run or a revoked token happens on command. |

## Running them

The simulator lives in the Veiller monorepo. Keep a Veiller checkout beside this
one, or point `VEILLER_REPO` at it.

```bash
bun run build                       # the harnesses run dist/, not src/

bun run sim/walkthrough.ts
bun run sim/walkthrough.ts --step 7 # one step

bun run sim/phone-tour.ts
bun run sim/phone-tour.ts --headed  # watch the browser
bun run sim/phone-tour.ts --shots ./out
```

Both exit non-zero on a finding, so they work as CI checks.

`phone-tour.ts` needs a Chromium: `bunx playwright-core install chromium`, or
set `CHROMIUM_PATH` to one you already have.

To walk a **released** bundle instead of this checkout's build — which is what
users actually installed — point `TENIR_BUNDLE` at the zip:

```bash
gh release download v0.6.5 --pattern '*veiller*.zip'
TENIR_BUNDLE=./tenir-veiller-v0.6.4.zip bun run sim/walkthrough.ts
```

## Seeing it rather than reading it

The simulator's own control panel shows the lens live next to the phone page,
with buttons for the temple bar and the mic:

```bash
bun --cwd ../../Veiller run simulate ~/git/Tenir/veiller
# → http://localhost:8770
```
