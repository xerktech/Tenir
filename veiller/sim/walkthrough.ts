/**
 * A guided tour of every Tenir feature, on simulated glasses.
 *
 * Boots the built miniapp against {@link FakeTenirServer}, then walks the whole
 * surface — sign-in, capture, captions, cues, translation runs, songs, the
 * Continue/Exit menu, reconnects, token expiry, backgrounding, history, sign-out
 * — printing the lens after each step. Run it and read the output the way you
 * would look through the glasses:
 *
 *   bun run sim/walkthrough.ts
 *   bun run sim/walkthrough.ts --step 7      # one step only
 *
 * `VEILLER_REPO` points at a Veiller checkout (default ../Veiller) for the
 * simulator itself.
 */

import {resolve} from "node:path"

import {FakeTenirServer} from "./fake-server"
import {simulatorModule, type SimulatorInstance} from "./sim-path"

const {Simulator, delay} = await simulatorModule()

// Defaults to this checkout's build output; point TENIR_BUNDLE at a released
// zip to walk the artifact users actually installed.
const BUNDLE = process.env.TENIR_BUNDLE ?? resolve(import.meta.dir, "..")

interface Step {
  title: string
  run: (ctx: Ctx) => Promise<void>
}

interface Ctx {
  sim: SimulatorInstance
  server: FakeTenirServer
  /** Print the lens under a caption. */
  show: (caption: string) => Promise<void>
  /** Assert, but keep walking — the point is to see everything, not stop early. */
  check: (label: string, ok: boolean, detail?: string) => void
}

const findings: string[] = []

function banner(text: string): void {
  console.log(`\n[1m${"═".repeat(78)}\n${text}\n${"═".repeat(78)}[0m`)
}

async function main(): Promise<void> {
  const only = stepFilter()
  const server = new FakeTenirServer().start()
  const sim = new Simulator({bundle: BUNDLE})
  await sim.start()

  const ctx: Ctx = {
    sim,
    server,
    show: async (caption) => {
      await sim.settle()
      console.log(`\n  — ${caption} —`)
      console.log(
        sim
          .lens()
          .split("\n")
          .map((l) => `  ${l}`)
          .join("\n"),
      )
    },
    check: (label, ok, detail) => {
      if (ok) {
        console.log(`  [32m✓[0m ${label}`)
      } else {
        console.log(`  [31m✗ ${label}[0m${detail ? ` — ${detail}` : ""}`)
        findings.push(`${label}${detail ? ` — ${detail}` : ""}`)
      }
    },
  }

  for (const [i, step] of STEPS.entries()) {
    if (only !== null && only !== i + 1) continue
    banner(`STEP ${i + 1}. ${step.title}`)
    try {
      await step.run(ctx)
    } catch (err) {
      console.log(`  [31m✗ step threw: ${err instanceof Error ? err.message : String(err)}[0m`)
      findings.push(`step ${i + 1} "${step.title}" threw: ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  banner(findings.length ? `${findings.length} finding(s)` : "No findings")
  for (const f of findings) console.log(`  • ${f}`)

  await sim.stop()
  server.stop()
  process.exit(findings.length ? 1 : 0)
}

function stepFilter(): number | null {
  const idx = process.argv.indexOf("--step")
  if (idx === -1) return null
  return Number(process.argv[idx + 1])
}

/** Sign in through the phone page, the way a user would. */
async function signIn(ctx: Ctx): Promise<void> {
  ctx.sim.phone.open()
  const result = await ctx.sim.phone.request<{ok: boolean; username?: string; error?: string}>(
    "tenir:login",
    {serverUrl: ctx.server.serverUrl, username: "sim", password: "hunter2"},
  )
  if (!result.ok) throw new Error(`login failed: ${result.error}`)
}

/** Start capture from the lens and wait for the socket to come up. */
async function startCapture(ctx: Ctx): Promise<void> {
  ctx.sim.tap()
  await ctx.sim.waitFor(() => ctx.server.current !== null, 3000, "no session opened on the server")
  await ctx.sim.settle()
}

const STEPS: Step[] = [
  {
    title: "Cold boot, signed out — the lens explains what to do",
    run: async (ctx) => {
      await ctx.show("lens at boot")
      ctx.check(
        "shows the sign-in prompt",
        ctx.sim.lensText().some((t) => /sign in/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.check(
        "subscribes to touch before anything else",
        ctx.sim.host.activeSubscriptions().includes("touch_event"),
        ctx.sim.host.activeSubscriptions().join(",") || "(none)",
      )
      ctx.check(
        "does not hold the mic while idle",
        !ctx.sim.host.activeSubscriptions().includes("audio_chunk"),
      )
    },
  },

  {
    title: "Taps do nothing until signed in",
    run: async (ctx) => {
      ctx.sim.tap()
      ctx.sim.doubleTap()
      await ctx.sim.settle()
      ctx.check("no session started", ctx.server.current === null)
      await ctx.show("lens after taps")
    },
  },

  {
    title: "Sign in from the phone page",
    run: async (ctx) => {
      await signIn(ctx)
      await ctx.sim.settle()
      await ctx.show("lens after sign-in")
      ctx.check(
        "lens goes to the idle prompt",
        ctx.sim.lensText().some((t) => /tap/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.check("clock is showing", ctx.sim.lensText().some((t) => /^\d{1,2}:\d{2}/.test(t.trim())))
      const auth = ctx.sim.phone.last<{signedIn: boolean; username: string; serverUrl: string}>("tenir:auth")
      ctx.check("phone page learns it is signed in", auth?.signedIn === true, JSON.stringify(auth))
      ctx.check(
        "server URL is stored for next boot",
        typeof ctx.sim.host.storageSnapshot()["tenir.serverUrl"] === "string",
        JSON.stringify(ctx.sim.host.storageSnapshot()),
      )
    },
  },

  {
    title: "A tap starts capture; the mic goes live",
    run: async (ctx) => {
      await startCapture(ctx)
      await ctx.show("lens while listening")
      ctx.check("socket session opened", ctx.server.current !== null)
      ctx.check(
        "mic subscribed",
        ctx.sim.host.activeSubscriptions().includes("audio_chunk"),
        ctx.sim.host.activeSubscriptions().join(","),
      )
      for (let i = 0; i < 5; i++) ctx.sim.speak({ms: 100})
      await delay(150)
      ctx.check(
        "PCM reaches the server",
        (ctx.server.current?.audioBytes ?? 0) > 0,
        `${ctx.server.current?.audioBytes ?? 0} bytes`,
      )
    },
  },

  {
    title: "Captions: a partial firms up into a final",
    run: async (ctx) => {
      ctx.server.partial("the quick brown")
      await ctx.sim.settle()
      await ctx.show("partial on the lens")
      ctx.check(
        "partial is shown",
        ctx.sim.lensText().some((t) => t.includes("quick brown")),
        JSON.stringify(ctx.sim.lensText()),
      )

      ctx.server.final("s1", "The quick brown fox jumps over the lazy dog.")
      ctx.server.partial("and then")
      await ctx.sim.settle()
      await ctx.show("final plus a new partial")
      ctx.check(
        "final is shown",
        ctx.sim.lensText().some((t) => t.includes("lazy dog")),
        JSON.stringify(ctx.sim.lensText()),
      )
      const live = ctx.sim.phone.last<{segments: {text: string}[]}>("tenir:live")
      ctx.check("phone mirror has the turn", (live?.segments ?? []).some((s) => s.text.includes("lazy dog")))
    },
  },

  {
    title: "A long transcript scrolls rather than overflowing",
    run: async (ctx) => {
      for (let i = 0; i < 8; i++) {
        ctx.server.final(`long-${i}`, `Turn number ${i}: ${"words ".repeat(12).trim()}.`)
      }
      await ctx.sim.settle()
      await ctx.show("lens with a long transcript")
      const caption = ctx.sim.lensText().find((t) => t.includes("Turn number"))
      ctx.check("newest turn is visible", caption?.includes("Turn number 7") === true, caption)
      ctx.check(
        "caption band stays inside the display",
        ctx.sim.glasses.lens().every((el) => el.box.y + el.box.h <= ctx.sim.glasses.model.scene.height),
      )
    },
  },

  {
    title: "A cue pops up, counts down, and can be held open",
    run: async (ctx) => {
      ctx.server.cue("c1", "Distance", "The sun is about 150 million km from Earth.", "Wikipedia")
      await ctx.sim.settle()
      await ctx.show("cue in the box")
      ctx.check(
        "cue title and body render",
        ctx.sim.lensText().some((t) => t.includes("Distance")) &&
          ctx.sim.lensText().some((t) => t.includes("150 million")),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.check(
        "cue is drawn as a bordered box",
        ctx.sim.glasses.lens().some((el) => Boolean((el.style as {border?: number})?.border)),
      )
      const secondsLeft = () => {
        const m = /\b(\d+)s\b/.exec(ctx.sim.lensText().join(" "))
        return m ? Number(m[1]) : null
      }
      const atOpen = secondsLeft()
      await delay(2200)
      await ctx.sim.settle()
      await ctx.show("cue a couple of seconds later")
      const ticked = secondsLeft()
      ctx.check("countdown ticks down on its own", atOpen !== null && ticked !== null && ticked < atOpen, `${atOpen}s → ${ticked}s`)

      ctx.sim.tap()
      await ctx.sim.settle()
      await ctx.show("cue after a tap (countdown should have reset)")
      ctx.check("cue still up after the hold tap", ctx.sim.lensText().some((t) => t.includes("Distance")))
      ctx.check(
        "a tap buys the cue more time (XERK-129)",
        (secondsLeft() ?? 0) > (ticked ?? 99),
        `${ticked}s → ${secondsLeft()}s`,
      )
    },
  },

  {
    title: "The cue auto-dismisses and a queued cue takes its place",
    run: async (ctx) => {
      ctx.server.cue("c2", "Queued", "This one waits its turn.")
      await ctx.sim.settle()
      ctx.check("second cue does not steal the box", ctx.sim.lensText().some((t) => t.includes("Distance")))
      await ctx.sim.waitForLens("Queued", 15_000)
      await ctx.show("queued cue promoted")
      ctx.check("queued cue is now showing", ctx.sim.lensText().some((t) => t.includes("Queued")))
      await ctx.sim.waitFor(
        () => !ctx.sim.lensText().some((t) => t.includes("Queued")),
        15_000,
        "queued cue never auto-dismissed",
      )
      await ctx.show("box free again")
    },
  },

  {
    title: "Translation run: turns stack in one box, then it clears",
    run: async (ctx) => {
      ctx.server.final("es1", "Hola, ¿cómo estás?", "es")
      ctx.server.translation("es1", "Hello, how are you?", "es")
      await ctx.sim.settle()
      await ctx.show("translation box opens")
      ctx.check(
        "translation title names the language",
        ctx.sim.lensText().some((t) => /Translating/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.check("first turn is shown", ctx.sim.lensText().some((t) => t.includes("how are you")))

      ctx.server.final("es2", "Me llamo Ana.", "es")
      ctx.server.translation("es2", "My name is Ana.", "es")
      await ctx.sim.settle()
      await ctx.show("second turn stacks into the same box")
      ctx.check("both turns visible", ctx.sim.lensText().some((t) => t.includes("My name is Ana")))

      ctx.server.translationDone()
      await ctx.sim.settle()
      await ctx.show("translation.done clears the box")
      ctx.check(
        "box dismissed",
        !ctx.sim.lensText().some((t) => /Translating/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )
    },
  },

  {
    title: "Song box: lyrics scroll, re-sync, and end",
    run: async (ctx) => {
      ctx.server.song("song1", "Blue Monday", "New Order", [
        {atMs: 0, text: "How does it feel"},
        {atMs: 1000, text: "To treat me like you do"},
        {atMs: 2000, text: "When you've laid your hands upon me"},
        {atMs: 3000, text: "And told me who you are"},
      ])
      await ctx.sim.settle()
      await ctx.show("song box opens at the first line")
      ctx.check(
        "title shows song and artist",
        ctx.sim.lensText().some((t) => /Blue Monday/i.test(t) && /New Order/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.check("first lyric is up", ctx.sim.lensText().some((t) => t.includes("How does it feel")))

      ctx.server.songSync("song1", 2000)
      await ctx.sim.settle()
      await ctx.show("after a sync anchor 2s into the track")
      ctx.check(
        "scroll jumped to the synced line",
        ctx.sim.lensText().some((t) => t.includes("laid your hands")),
        JSON.stringify(ctx.sim.lensText()),
      )

      ctx.server.songDone("song1")
      await ctx.sim.settle()
      await ctx.show("song.done frees the box")
      ctx.check("song box gone", !ctx.sim.lensText().some((t) => /Blue Monday/i.test(t)))
    },
  },

  {
    title: "Box precedence: a song outranks a live translation run",
    run: async (ctx) => {
      ctx.server.final("es3", "Buenos días.", "es")
      ctx.server.translation("es3", "Good morning.", "es")
      await ctx.sim.settle()
      ctx.check("translation has the box", ctx.sim.lensText().some((t) => /Translating/i.test(t)))

      ctx.server.song("song2", "Ceremony", "New Order", [{atMs: 0, text: "This is why events unnerve me"}])
      await ctx.sim.settle()
      await ctx.show("song takes the box from the translation")
      ctx.check(
        "song wins",
        ctx.sim.lensText().some((t) => /Ceremony/i.test(t)) &&
          !ctx.sim.lensText().some((t) => /Translating/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )

      ctx.server.songDone("song2")
      await ctx.sim.settle()
      await ctx.show("song ends — the held translation comes back")
      ctx.check(
        "translation retakes the box",
        ctx.sim.lensText().some((t) => /Translating/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.server.translationDone()
      await ctx.sim.settle()
    },
  },

  {
    title: "The Continue / Exit menu",
    run: async (ctx) => {
      ctx.sim.doubleTap()
      await ctx.sim.settle()
      await ctx.show("menu open, Continue highlighted")
      ctx.check(
        "both choices are offered",
        ctx.sim.lensText().some((t) => /Continue/i.test(t)) &&
          ctx.sim.lensText().some((t) => /Exit/i.test(t)),
        JSON.stringify(ctx.sim.lensText()),
      )

      ctx.sim.swipeDown()
      await ctx.sim.settle()
      await ctx.show("swipe down moves the highlight to Exit")

      ctx.sim.swipeUp()
      await ctx.sim.settle()
      await ctx.show("swipe up moves it back to Continue")

      ctx.sim.doubleTap()
      await ctx.sim.settle()
      await ctx.show("a second double tap dismisses the menu")
      ctx.check("menu closed", !ctx.sim.lensText().some((t) => /Exit session/i.test(t)))
      ctx.check("still recording", ctx.server.current !== null)
    },
  },

  {
    title: "A brushed temple must not end a session (XERK-85)",
    run: async (ctx) => {
      const before = ctx.server.current
      ctx.sim.tap()
      ctx.sim.tap()
      await ctx.sim.settle()
      ctx.check("single taps do not stop capture", ctx.server.current === before && before !== null)
      await ctx.show("lens after stray taps")
    },
  },

  {
    title: "Reconnect: the socket drops and the session resumes",
    run: async (ctx) => {
      const firstId = ctx.server.current?.sessionId
      ctx.server.dropSocket()
      await ctx.sim.settle()
      await ctx.show("lens right after the drop")
      await ctx.sim.waitFor(() => ctx.server.current !== null, 8000, "client never reconnected")
      await ctx.sim.settle()
      await ctx.show("lens after reconnect")
      ctx.check("same session resumed", ctx.server.current?.sessionId === firstId, `${firstId} → ${ctx.server.current?.sessionId}`)
      ctx.check("resume flag set on the wire", ctx.server.current?.resumed === true)
    },
  },

  {
    title: "Backgrounding the phone keeps the HUD alive",
    run: async (ctx) => {
      ctx.sim.background()
      await delay(1200)
      await ctx.show("lens while the phone is in a pocket")
      ctx.check("still recording", ctx.server.current !== null)
      const revBefore = ctx.sim.glasses.currentRevision()
      await delay(1500)
      ctx.check("HUD keeps repainting in the background", ctx.sim.glasses.currentRevision() > revBefore)
      ctx.sim.foreground()
      await ctx.sim.settle()
      await ctx.show("back in the foreground")
    },
  },

  {
    title: "Exit from the menu stops the session and wipes the lens",
    run: async (ctx) => {
      ctx.sim.doubleTap()
      await ctx.sim.settle()
      ctx.sim.swipeDown()
      await ctx.sim.settle()
      ctx.sim.tap()
      await ctx.sim.settle()
      await ctx.show("lens after Exit")
      ctx.check("capture stopped", ctx.server.current === null)
      ctx.check(
        "transcript is gone",
        !ctx.sim.lensText().some((t) => t.includes("Turn number")),
        JSON.stringify(ctx.sim.lensText()),
      )
      ctx.check("back to the idle prompt", ctx.sim.lensText().some((t) => /tap/i.test(t)))
      ctx.check(
        "mic released",
        !ctx.sim.host.activeSubscriptions().includes("audio_chunk"),
        ctx.sim.host.activeSubscriptions().join(","),
      )
    },
  },

  {
    title: "History: the phone page lists, opens, and deletes conversations",
    run: async (ctx) => {
      ctx.server.conversations = [
        {
          id: "conv1",
          status: "final",
          micSource: "g2-microphone",
          sourceLang: "en",
          startedAt: new Date().toISOString(),
          endedAt: new Date().toISOString(),
          durationMs: 60_000,
          segmentCount: 2,
          hasAudio: false,
          segments: [
            {segmentId: "s1", text: "First turn", startMs: 0, endMs: 500, lang: "en"},
            {segmentId: "s2", text: "Second turn", startMs: 600, endMs: 900, lang: "en"},
          ],
          cues: [],
        },
      ]
      const list = await ctx.sim.phone.request<{ok: boolean; data?: unknown[]; error?: string}>(
        "tenir:fetch",
        {method: "GET", path: "/conversations?limit=50&offset=0"},
      )
      ctx.check("list returns the conversation", list.ok && (list.data as unknown[])?.length === 1, JSON.stringify(list))

      const detail = await ctx.sim.phone.request<{ok: boolean; data?: {segments?: unknown[]}}>("tenir:fetch", {
        method: "GET",
        path: "/conversations/conv1",
      })
      ctx.check("detail returns segments", (detail.data?.segments ?? []).length === 2, JSON.stringify(detail))

      const del = await ctx.sim.phone.request<{ok: boolean}>("tenir:fetch", {
        method: "DELETE",
        path: "/conversations/conv1",
      })
      ctx.check("delete succeeds", del.ok === true, JSON.stringify(del))
      ctx.check("conversation is gone server-side", ctx.server.conversations.length === 0)
    },
  },

  {
    title: "An expired token heals itself with a silent re-login",
    run: async (ctx) => {
      await startCapture(ctx)
      const tokensBefore = ctx.server.tokens.length
      // The stored token is revoked mid-session; the cached credentials are
      // still good, so the wearer should never notice.
      ctx.server.revokeTokens()
      ctx.server.error("unauthorized", "token expired")
      await ctx.sim.waitFor(
        () => ctx.server.tokens.length > 0,
        5000,
        "no silent re-login happened after the unauthorized error",
      )
      await ctx.sim.waitFor(() => ctx.server.current !== null, 5000, "capture never resumed")
      await ctx.sim.settle()
      await ctx.show("lens after the token was revoked and re-minted")
      ctx.check("a fresh token was minted", ctx.server.tokens.length >= 1, `${tokensBefore} → ${ctx.server.tokens.length}`)
      ctx.check(
        "the wearer stays signed in",
        ctx.sim.phone.last<{signedIn: boolean}>("tenir:auth")?.signedIn !== false,
        JSON.stringify(ctx.sim.phone.last("tenir:auth")),
      )
      ctx.check("capture is still running", ctx.server.current !== null)
      ctx.check("lens is not asking for a sign-in", !ctx.sim.lensText().some((t) => /sign in/i.test(t)))
    },
  },

  {
    title: "When the cached credentials stop working, the wearer is signed out",
    run: async (ctx) => {
      ctx.server.rejectLogins = true
      ctx.server.revokeTokens()
      ctx.server.error("unauthorized", "token expired")
      await ctx.sim.waitFor(
        () => ctx.sim.lensText().some((t) => /sign in/i.test(t)),
        8000,
        `lens never fell back to the sign-in prompt (showing: ${JSON.stringify(ctx.sim.lensText())})`,
      )
      await ctx.sim.settle()
      await ctx.show("lens once re-login failed too")
      ctx.check(
        "phone page is told we were signed out",
        ctx.sim.phone.last<{signedIn: boolean}>("tenir:auth")?.signedIn === false,
        JSON.stringify(ctx.sim.phone.last("tenir:auth")),
      )
      ctx.check("capture stopped", ctx.server.current === null)
      ctx.check(
        "mic released",
        !ctx.sim.host.activeSubscriptions().includes("audio_chunk"),
        ctx.sim.host.activeSubscriptions().join(","),
      )
      ctx.server.rejectLogins = false
    },
  },

  {
    title: "Sign out from the phone page",
    run: async (ctx) => {
      await signIn(ctx)
      await ctx.sim.settle()
      const out = await ctx.sim.phone.request<{ok: boolean}>("tenir:logout", {})
      await ctx.sim.settle()
      await ctx.show("lens after sign-out")
      ctx.check("logout acknowledged", out.ok === true)
      ctx.check("lens is back to the sign-in prompt", ctx.sim.lensText().some((t) => /sign in/i.test(t)))
      ctx.check(
        "cached credentials cleared",
        ctx.sim.host.storageSnapshot()["tenir.credentials"] === undefined,
        JSON.stringify(ctx.sim.host.storageSnapshot()),
      )
    },
  },
]

await main()
