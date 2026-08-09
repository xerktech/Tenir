/**
 * The same tour as `walkthrough.ts`, but for the phone page — driven through a
 * real browser.
 *
 * `walkthrough.ts` exercises the background context and the lens with a
 * headless stand-in for the WebView. That covers the state machine but not the
 * page itself: its DOM, its `veiller.request` round-trips, its rendering of
 * live captions, cue cards, songs, and history. This script serves the built UI
 * bundle with the phone's real host environment injected (see the simulator's
 * ui-host), opens it in Chromium, and clicks through it like a person would —
 * with the glasses lens visible alongside, since the two must agree.
 *
 *   bun run sim/phone-tour.ts
 *   bun run sim/phone-tour.ts --headed        watch it happen
 *   bun run sim/phone-tour.ts --shots ./out   keep the screenshots
 *
 * Needs a Chromium: `bunx playwright-core install chromium`, or set
 * CHROMIUM_PATH. `VEILLER_REPO` locates the simulator, as in walkthrough.ts.
 */

import {mkdirSync} from "node:fs"
import {resolve} from "node:path"

import {chromium, type Browser, type Page} from "playwright-core"

import {FakeTenirServer} from "./fake-server"
import {simulatorModule, type SimulatorInstance} from "./sim-path"

const {Simulator, startPanel} = (await simulatorModule()) as Awaited<ReturnType<typeof simulatorModule>> & {
  startPanel: (sim: SimulatorInstance, port?: number) => {url: string; stop: () => void}
}

const BUNDLE = process.env.TENIR_BUNDLE ?? resolve(import.meta.dir, "..")
const HEADED = process.argv.includes("--headed")
const SHOTS = argValue("--shots")
const PANEL_PORT = Number(argValue("--port") ?? 8770)

function argValue(flag: string): string | undefined {
  const i = process.argv.indexOf(flag)
  return i === -1 ? undefined : process.argv[i + 1]
}

function chromiumPath(): string | undefined {
  return process.env.CHROMIUM_PATH
}

const findings: string[] = []
let stepIndex = 0

async function main(): Promise<void> {
  if (SHOTS) mkdirSync(SHOTS, {recursive: true})

  const server = new FakeTenirServer().start()
  const sim = new Simulator({bundle: BUNDLE})
  await sim.start()
  const panel = startPanel(sim, PANEL_PORT)

  let browser: Browser
  try {
    browser = await chromium.launch({
      headless: !HEADED,
      ...(chromiumPath() ? {executablePath: chromiumPath()} : {}),
    })
  } catch (err) {
    console.error(
      "Could not launch Chromium. Install one with `bunx playwright-core install chromium`,\n" +
        "or point CHROMIUM_PATH at an existing binary.\n" +
        (err instanceof Error ? err.message : String(err)),
    )
    panel.stop()
    await sim.stop()
    server.stop()
    process.exit(2)
  }

  const page = await browser.newPage({viewport: {width: 420, height: 900}})
  page.setDefaultTimeout(8000)
  const consoleErrors: string[] = []
  page.on("pageerror", (e) => consoleErrors.push(`uncaught: ${e.message}`))
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text())
  })
  // Chromium's console text for a failed subresource omits the URL, which makes
  // the finding unactionable. Record the response instead.
  const failedRequests: string[] = []
  page.on("response", (r) => {
    if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.url()}`)
  })

  const ctx = {sim, server, page, panel}

  await page.goto(`${panel.url}/app/`, {waitUntil: "networkidle"})
  await page.waitForTimeout(800)

  for (const step of STEPS) {
    stepIndex += 1
    console.log(`\n${"═".repeat(72)}\nSTEP ${stepIndex}. ${step.title}\n${"═".repeat(72)}`)
    try {
      await step.run(ctx)
    } catch (err) {
      const msg = err instanceof Error ? err.message.split("\n")[0] : String(err)
      console.log(`  ✗ step threw — ${msg}`)
      findings.push(`step ${stepIndex} "${step.title}" threw: ${msg}`)
    }
    if (SHOTS) {
      await page.screenshot({
        path: `${SHOTS}/${String(stepIndex).padStart(2, "0")}-${step.title.replace(/\W+/g, "-").toLowerCase()}.png`,
        fullPage: true,
      })
    }
  }

  // A browser always asks for /favicon.ico and a bundle never ships one; that
  // 404 is the browser's, not the miniapp's. Everything else is a real gap.
  const realFailures = failedRequests.filter((r) => !/\/favicon\.ico$/.test(r))
  if (failedRequests.length) {
    console.log(`\n  Failed requests:`)
    for (const r of failedRequests) console.log(`    ${r}`)
  }
  for (const r of realFailures) findings.push(`page requested a missing resource: ${r}`)

  const realConsole = consoleErrors.filter((e) => !/Failed to load resource/i.test(e))
  if (realConsole.length) {
    console.log(`\n  Browser console errors:`)
    for (const e of realConsole) console.log(`    ${e}`)
    findings.push(`${realConsole.length} browser console error(s): ${realConsole[0]}`)
  }

  console.log(`\n${"═".repeat(72)}\n${findings.length ? `${findings.length} finding(s)` : "No findings"}\n${"═".repeat(72)}`)
  for (const f of findings) console.log(`  • ${f}`)

  await browser.close()
  panel.stop()
  await sim.stop()
  server.stop()
  process.exit(findings.length ? 1 : 0)
}

interface Ctx {
  sim: SimulatorInstance
  server: FakeTenirServer
  page: Page
  panel: {url: string}
}

interface Step {
  title: string
  run: (ctx: Ctx) => Promise<void>
}

function check(label: string, ok: boolean, detail?: string): void {
  if (ok) {
    console.log(`  ✓ ${label}`)
    return
  }
  console.log(`  ✗ ${label}${detail ? ` — ${detail}` : ""}`)
  findings.push(`${label}${detail ? ` — ${detail}` : ""}`)
}

async function bodyText(page: Page): Promise<string> {
  return (await page.locator("body").innerText()).replace(/\n{3,}/g, "\n\n")
}

async function showBoth(ctx: Ctx, caption: string): Promise<void> {
  await ctx.sim.settle()
  console.log(`\n  — ${caption} — phone —`)
  console.log(
    (await bodyText(ctx.page))
      .split("\n")
      .map((l) => `    ${l}`)
      .join("\n"),
  )
  console.log(`  — ${caption} — lens —`)
  console.log(
    ctx.sim
      .lens()
      .split("\n")
      .map((l) => `    ${l}`)
      .join("\n"),
  )
}

const STEPS: Step[] = [
  {
    title: "The page opens on the login form",
    run: async (ctx) => {
      await showBoth(ctx, "first paint")
      check("server field is present", await ctx.page.locator("#server-url").isVisible())
      check("login button is present", await ctx.page.locator("#login-submit").isVisible())
      check("session controls are hidden until sign-in", !(await ctx.page.locator("#session-start").isVisible()))
    },
  },

  {
    title: "A bad password is reported, not swallowed",
    run: async (ctx) => {
      await ctx.page.fill("#server-url", `http://127.0.0.1:${ctx.server.port}`)
      await ctx.page.fill("#username", "sim")
      await ctx.page.fill("#password", "wrong")
      await ctx.page.click("#login-submit")
      await ctx.page.waitForTimeout(800)
      const text = await bodyText(ctx.page)
      check("an error is shown", /incorrect|invalid|password/i.test(text), text.slice(0, 200))
      check("still on the login form", await ctx.page.locator("#login-submit").isVisible())
    },
  },

  {
    title: "Signing in reveals the session screen — and the lens agrees",
    run: async (ctx) => {
      await ctx.page.fill("#password", "hunter2")
      await ctx.page.click("#login-submit")
      await ctx.page.waitForSelector("#session-start", {state: "visible"})
      await showBoth(ctx, "signed in")
      check("Start button is offered", await ctx.page.locator("#session-start").isVisible())
      check("lens left the sign-in prompt", !ctx.sim.lensText().some((t) => /sign in/i.test(t)))
    },
  },

  {
    title: "Start from the phone; the glasses follow",
    run: async (ctx) => {
      await ctx.page.click("#session-start")
      await ctx.sim.waitFor(() => ctx.server.current !== null, 5000, "no session opened")
      await showBoth(ctx, "capturing")
      check("Stop replaces Start", await ctx.page.locator("#session-stop").isVisible())
      check("lens shows it is listening", ctx.sim.lensText().some((t) => /listening/i.test(t)))
      check("mic is subscribed", ctx.sim.host.activeSubscriptions().includes("audio_chunk"))
    },
  },

  {
    title: "Captions land on both surfaces",
    run: async (ctx) => {
      ctx.server.partial("the quick brown")
      await ctx.page.waitForTimeout(300)
      ctx.server.final("s1", "The quick brown fox jumps over the lazy dog.")
      ctx.server.final("s2", "And here is a second turn.")
      await ctx.page.waitForTimeout(600)
      await showBoth(ctx, "two turns in")
      const text = await bodyText(ctx.page)
      check("phone shows the first turn", text.includes("lazy dog"), text.slice(0, 300))
      check("phone shows the second turn", text.includes("second turn"))
      check("lens shows the newest turn", ctx.sim.lensText().some((t) => t.includes("second turn")))
    },
  },

  {
    title: "A cue card renders with its source attribution",
    run: async (ctx) => {
      ctx.server.cue("c1", "Distance", "The sun is about 150 million km from Earth.", "Wikipedia")
      await ctx.page.waitForTimeout(600)
      await showBoth(ctx, "cue up")
      const text = await bodyText(ctx.page)
      check("phone shows the cue title", text.includes("Distance"), text.slice(0, 300))
      check("phone shows the cue body", text.includes("150 million"))
      check("phone shows the source", text.includes("Wikipedia"))
      check("lens shows the same cue", ctx.sim.lensText().some((t) => t.includes("Distance")))
    },
  },

  {
    title: "A recognised song shows its lyrics on both surfaces",
    run: async (ctx) => {
      ctx.server.song("g1", "Blue Monday", "New Order", [
        {atMs: 0, text: "How does it feel"},
        {atMs: 1000, text: "To treat me like you do"},
      ])
      await ctx.page.waitForTimeout(600)
      await showBoth(ctx, "song playing")
      const text = await bodyText(ctx.page)
      check("phone shows title and artist", /Blue Monday/.test(text) && /New Order/.test(text), text.slice(0, 300))
      check("phone shows a lyric", text.includes("How does it feel"))
      check("lens shows the song too", ctx.sim.lensText().some((t) => /Blue Monday/.test(t)))
      ctx.server.songDone("g1")
      await ctx.page.waitForTimeout(400)
    },
  },

  {
    title: "A translated turn is paired with its original",
    run: async (ctx) => {
      ctx.server.final("es1", "Hola, buenos días.", "es")
      ctx.server.translation("es1", "Hello, good morning.", "es")
      await ctx.page.waitForTimeout(600)
      await showBoth(ctx, "translated turn")
      const text = await bodyText(ctx.page)
      check("original is shown", text.includes("buenos días"), text.slice(0, 400))
      check("translation is shown alongside it", text.includes("good morning"))
      ctx.server.translationDone()
      await ctx.page.waitForTimeout(400)
    },
  },

  {
    title: "Stop from the phone ends the session and wipes the lens",
    run: async (ctx) => {
      await ctx.page.click("#session-stop")
      await ctx.sim.waitFor(() => ctx.server.current === null, 5000, "session never ended")
      await showBoth(ctx, "stopped")
      check("Start comes back", await ctx.page.locator("#session-start").isVisible())
      check("lens transcript is cleared", !ctx.sim.lensText().some((t) => t.includes("lazy dog")))
      check("mic released", !ctx.sim.host.activeSubscriptions().includes("audio_chunk"))
    },
  },

  {
    title: "History lists, opens, and deletes a conversation",
    run: async (ctx) => {
      ctx.server.conversations = [
        {
          id: "conv1",
          status: "final",
          micSource: "g2-microphone",
          sourceLang: "en",
          startedAt: new Date("2026-01-02T15:04:05Z").toISOString(),
          endedAt: new Date("2026-01-02T15:05:10Z").toISOString(),
          durationMs: 65_000,
          segmentCount: 2,
          hasAudio: false,
          segments: [
            {segmentId: "s1", text: "First turn of the conversation", startMs: 0, endMs: 500, lang: "en"},
            {segmentId: "s2", text: "Second turn", startMs: 600, endMs: 900, lang: "en"},
          ],
          cues: [{cueId: "c1", title: "Distance", body: "About 150 million km.", atMs: 300}],
        },
      ]
      await ctx.page.click("#nav-history")
      await ctx.page.waitForSelector(".history-item", {state: "visible"})
      check("the conversation is listed", (await ctx.page.locator(".history-item").count()) === 1)

      await ctx.page.locator(".history-item .history-open").first().click()
      await ctx.page.waitForSelector("#history-detail", {state: "visible"})
      const detail = await bodyText(ctx.page)
      check("detail shows the turns", detail.includes("First turn of the conversation"), detail.slice(0, 400))
      check("detail shows the embedded cue", detail.includes("Distance"), detail.slice(0, 400))

      // A stored cue opens the detail POPUP, as upstream does — not an inline
      // expansion (XERK-237).
      await ctx.page.locator("#history-transcript .cue-inline").first().click()
      await ctx.page.waitForSelector("#history-cue-popup", {state: "visible"})
      check(
        "the cue popup shows the cue body",
        (await ctx.page.locator("#history-cue-popup-body").innerText()).includes("150 million"),
      )
      await ctx.page.locator("#history-cue-popup-close").click()
      check("the popup closes again", !(await ctx.page.locator("#history-cue-popup").isVisible()))

      // Delete is deliberately two-step (arm, then confirm).
      await ctx.page.click("#history-delete")
      await ctx.page.waitForTimeout(200)
      await ctx.page.click("#history-delete")
      await ctx.page.waitForTimeout(800)
      check("conversation deleted server-side", ctx.server.conversations.length === 0)
    },
  },

  {
    title: "A conversation with retained audio gets a working player",
    run: async (ctx) => {
      // XERK-237: upstream's history detail plays and downloads the retained
      // clip. The WebView can't fetch the authenticated endpoint cross-origin,
      // but `<audio src>` is plain media navigation and the api takes the token
      // as a query param — so the player has to actually load something.
      ctx.server.conversations = [
        {
          id: "conv-audio",
          status: "final",
          micSource: "g2-microphone",
          sourceLang: "en",
          startedAt: new Date("2026-01-03T09:00:00Z").toISOString(),
          endedAt: new Date("2026-01-03T09:00:30Z").toISOString(),
          durationMs: 30_000,
          segmentCount: 1,
          hasAudio: true,
          segments: [{segmentId: "s1", text: "A recorded turn", startMs: 0, endMs: 400, lang: "en"}],
          cues: [],
        },
      ]
      await ctx.page.click("#nav-history")
      await ctx.page.waitForSelector(".history-item", {state: "visible"})
      await ctx.page.locator(".history-item .history-open").first().click()
      await ctx.page.waitForSelector("#history-audio", {state: "visible"})
      check("the player is shown for a conversation with audio", await ctx.page.locator("#history-audio").isVisible())

      const src = await ctx.page.locator("#history-audio-el").getAttribute("src")
      check("the clip URL carries the bearer token", Boolean(src && /\/audio\?token=/.test(src)), String(src))

      // The element must actually load it — a 401 or a bad URL would leave
      // readyState at 0 and the duration NaN.
      const ok = await ctx.page.evaluate(async () => {
        const el = document.getElementById("history-audio-el") as HTMLAudioElement
        if (!el) return {loaded: false, duration: 0}
        await new Promise<void>((resolve) => {
          if (el.readyState >= 1) return resolve()
          el.addEventListener("loadedmetadata", () => resolve(), {once: true})
          el.addEventListener("error", () => resolve(), {once: true})
          setTimeout(resolve, 4000)
        })
        return {loaded: el.readyState >= 1, duration: el.duration}
      })
      check("the browser loads the clip's metadata", ok.loaded, JSON.stringify(ok))

      // Actually press the button — a save that quietly reports failure looks
      // exactly like a broken one to the wearer.
      await ctx.page.click("#history-audio-link")
      await ctx.page.waitForTimeout(500)
      const toast = await ctx.page.evaluate(() => {
        const el = document.getElementById("app-toast")
        return {shown: el?.classList.contains("show") ?? false, text: el?.textContent ?? ""}
      })
      check("saving the clip does not report a failure", !toast.shown, JSON.stringify(toast))

      await ctx.page.click("#history-back")
      const stillSrc = await ctx.page.locator("#history-audio-el").getAttribute("src")
      check("leaving the detail releases the clip", !stillSrc, String(stillSrc))
      ctx.server.conversations = []
    },
  },

  {
    title: "The host's scheme reaches the page, and both palettes render",
    run: async (ctx) => {
      // XERK-237: upstream follows `prefers-color-scheme`; here the host says
      // which it is. The page shipped dark-only, so there are two things to
      // check — that the host's choice ARRIVES, and that each palette renders.
      const bg = () => ctx.page.evaluate(() => getComputedStyle(document.body).backgroundColor)
      const themeAttr = () =>
        ctx.page.evaluate(() => document.documentElement.getAttribute("data-theme"))

      // The simulator's host reports "dark" in its CONNECT_ACK and injects no
      // `window.MentraOS`, so a `data-theme` on the page can only have come
      // down the `tenir:color-scheme` channel from the background — i.e. this
      // asserts the delivery path, not just the stylesheet. (The background
      // half — session.colorScheme and onColorSchemeChange — is covered by
      // TenirController.test.ts, which the simulator can't drive: it hardcodes
      // dark and exposes no way to push a change.)
      check("the host's scheme reaches the page", (await themeAttr()) === "dark", String(await themeAttr()))
      const darkBg = await bg()

      // From here on this is a STYLESHEET check: force each palette and look
      // at what it paints.
      await ctx.page.evaluate(() => document.documentElement.setAttribute("data-theme", "light"))
      const lightBg = await bg()
      check("light and dark paint different backgrounds", darkBg !== lightBg, `${darkBg} vs ${lightBg}`)
      check("the light palette is actually light", /^rgb\((2\d\d|1\d\d), /.test(lightBg), lightBg)

      // Text has to survive the swap: a token that only exists in the dark
      // block would leave the light page unreadable.
      const contrast = await ctx.page.evaluate(() => {
        const lum = (c: string) => {
          const [r, g, b] = (c.match(/\d+/g) ?? ["0", "0", "0"]).map(Number).map((v) => {
            const s = v / 255
            return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
          })
          return 0.2126 * r + 0.7152 * g + 0.0722 * b
        }
        const style = getComputedStyle(document.body)
        const [a, b] = [lum(style.color), lum(style.backgroundColor)].sort((x, y) => y - x)
        return (a + 0.05) / (b + 0.05)
      })
      check("body text stays legible in light mode", contrast >= 4.5, `contrast ${contrast.toFixed(2)}:1`)

      await ctx.page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"))
      check("dark comes back", (await bg()) === darkBg, `${await bg()} vs ${darkBg}`)
    },
  },

  {
    title: "Sign out returns the phone and the lens to the sign-in prompt",
    run: async (ctx) => {
      await ctx.page.click("#nav-session").catch(() => {})
      await ctx.page.waitForTimeout(200)
      await ctx.page.click("#sign-out")
      await ctx.page.waitForSelector("#login-submit", {state: "visible"})
      await showBoth(ctx, "signed out")
      check("login form is back", await ctx.page.locator("#server-url").isVisible())
      check("lens asks for a sign-in", ctx.sim.lensText().some((t) => /sign in/i.test(t)))
    },
  },
]

await main()
