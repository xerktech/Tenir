/**
 * A stand-in Tenir server for simulator runs.
 *
 * Speaks the real wire contract — `POST /auth/login`, `GET /auth/me`, the
 * `/conversations` history endpoints, and the `/ws` captions socket carrying
 * binary PCM up and JSON control/result frames down — but every server-side
 * event is driven by the test instead of by speech recognition. That is the
 * point: it makes cues, translations, songs, token expiry and socket drops
 * happen on command, so the whole feature surface can be walked in seconds
 * without a GPU, a household, or somebody talking into a microphone.
 *
 * Only behaviour the client can observe is modelled. Anything the client never
 * reads (durations, word timings, audio retention) is omitted.
 */

import type {ServerWebSocket} from "bun"

export interface FakeServerOptions {
  username?: string
  password?: string
  /** Reject the very first `/auth/me` so boot exercises the silent re-login path. */
  expireInitialToken?: boolean
  /** Close every socket with 1008, as the api does for a bad/expired token. */
  rejectSockets?: boolean
  port?: number
}

interface Session {
  ws: ServerWebSocket<unknown>
  sessionId: string
  /** Bytes of PCM the client has streamed — proof the mic path is live. */
  audioBytes: number
  resumed: boolean
  ended: boolean
}

export class FakeTenirServer {
  private server: ReturnType<typeof Bun.serve> | null = null
  private readonly opts: Required<Omit<FakeServerOptions, "port">> & {port: number}
  private meCalls = 0
  private nextSession = 1

  /** Live sockets, newest last. Most drivers only ever want `current`. */
  readonly sessions: Session[] = []
  /** Tokens the server has minted; the newest is the valid one. */
  readonly tokens: string[] = []
  /** Conversations returned by the history endpoints. Mutable from tests. */
  conversations: Record<string, unknown>[] = []
  /** Paths the client hit, for asserting on the phone page's REST usage. */
  readonly requests: string[] = []
  /** Flip to make `/auth/login` reject — the "cached credentials no longer work" case. */
  rejectLogins = false

  constructor(opts: FakeServerOptions = {}) {
    this.opts = {
      username: opts.username ?? "sim",
      password: opts.password ?? "hunter2",
      expireInitialToken: opts.expireInitialToken ?? false,
      rejectSockets: opts.rejectSockets ?? false,
      port: opts.port ?? 0,
    }
  }

  get port(): number {
    if (!this.server) throw new Error("FakeTenirServer is not started")
    const {port} = this.server
    if (typeof port !== "number") throw new Error("FakeTenirServer has no port")
    return port
  }

  /** What a user would type into the miniapp's server field. */
  get serverUrl(): string {
    return `ws://127.0.0.1:${this.port}/ws`
  }

  /** The newest live session, or null when the client isn't connected. */
  get current(): Session | null {
    const live = this.sessions.filter((s) => !s.ended)
    return live.length ? live[live.length - 1] : null
  }

  start(): this {
    const self = this
    this.server = Bun.serve({
      port: this.opts.port,
      async fetch(req, server) {
        const url = new URL(req.url)
        self.requests.push(`${req.method} ${url.pathname}`)

        if (url.pathname === "/ws") {
          const token = url.searchParams.get("token")
          if (self.opts.rejectSockets || !token || !self.tokens.includes(token)) {
            // The api rejects an unauthenticated socket with a policy close
            // rather than an HTTP error, so upgrade first and close after.
            if (server.upgrade(req, {data: {reject: true}})) return undefined
            return new Response("unauthorized", {status: 401})
          }
          if (server.upgrade(req, {data: {reject: false}})) return undefined
          return new Response("expected websocket", {status: 400})
        }

        return self.rest(req, url)
      },
      websocket: {
        open(ws) {
          if ((ws.data as {reject?: boolean})?.reject) {
            ws.close(1008, "unauthorized")
            return
          }
        },
        message(ws, message) {
          self.onSocketMessage(ws as ServerWebSocket<unknown>, message)
        },
        close(ws) {
          const session = self.sessions.find((s) => s.ws === ws)
          if (session) session.ended = true
        },
      },
    })
    return this
  }

  stop(): void {
    this.server?.stop(true)
    this.server = null
  }

  // ===========================================================================
  // REST
  // ===========================================================================

  private async rest(req: Request, url: URL): Promise<Response> {
    const authed = () => {
      const header = req.headers.get("authorization") ?? ""
      const token = header.replace(/^Bearer\s+/i, "")
      return Boolean(token && this.tokens.includes(token))
    }
    // The audio endpoint is reached by plain navigation — an `<audio src>` or
    // the OS download sheet — neither of which can set a header, so the real
    // api accepts the token as a query param on this one route (XERK-237).
    const authedByQuery = () => {
      const token = url.searchParams.get("token") ?? ""
      return Boolean(token && this.tokens.includes(token))
    }

    if (url.pathname === "/auth/login" && req.method === "POST") {
      const body = (await req.json()) as {username?: string; password?: string}
      if (this.rejectLogins || body.username !== this.opts.username || body.password !== this.opts.password) {
        return Response.json({detail: "invalid credentials"}, {status: 401})
      }
      const token = this.mintToken()
      return Response.json({token})
    }

    if (url.pathname === "/auth/me") {
      this.meCalls += 1
      // The "stored token has expired" case: the first probe fails, the silent
      // re-login that follows succeeds.
      if (this.opts.expireInitialToken && this.meCalls === 1) {
        return Response.json({detail: "token expired"}, {status: 401})
      }
      if (!authed()) return Response.json({detail: "unauthorized"}, {status: 401})
      return Response.json({
        userId: "u1",
        username: this.opts.username,
        household: "home",
        role: "member",
      })
    }

    // Retained audio: token in the query, a real (if tiny) WAV in the body, so
    // the page's `<audio>` element gets something it can actually load.
    const audio = /^\/conversations\/([^/]+)\/audio$/.exec(url.pathname)
    if (audio) {
      if (!authed() && !authedByQuery()) {
        return Response.json({detail: "unauthorized"}, {status: 401})
      }
      const found = this.conversations.find((c) => c.id === audio[1])
      if (!found) return Response.json({detail: "not found"}, {status: 404})
      return new Response(silentWav().buffer as ArrayBuffer, {
        status: 200,
        headers: {"content-type": "audio/wav"},
      })
    }

    if (!authed()) return Response.json({detail: "unauthorized"}, {status: 401})

    if (url.pathname === "/conversations" && req.method === "GET") {
      return Response.json(this.conversations)
    }
    const detail = /^\/conversations\/([^/]+)$/.exec(url.pathname)
    if (detail) {
      const id = detail[1]
      if (req.method === "DELETE") {
        this.conversations = this.conversations.filter((c) => c.id !== id)
        return new Response(null, {status: 204})
      }
      const found = this.conversations.find((c) => c.id === id)
      if (!found) return Response.json({detail: "not found"}, {status: 404})
      return Response.json(found)
    }

    return Response.json({detail: "not found"}, {status: 404})
  }

  private mintToken(): string {
    const token = `tok-${this.tokens.length + 1}`
    this.tokens.push(token)
    return token
  }

  /** Invalidate every minted token, as a server-side revocation would. */
  revokeTokens(): void {
    this.tokens.length = 0
  }

  // ===========================================================================
  // WebSocket
  // ===========================================================================

  private onSocketMessage(ws: ServerWebSocket<unknown>, message: string | Buffer): void {
    if (typeof message !== "string") {
      const session = this.sessions.find((s) => s.ws === ws)
      if (session) session.audioBytes += message.byteLength
      return
    }
    let msg: {type?: string; sessionId?: string}
    try {
      msg = JSON.parse(message)
    } catch {
      return
    }
    if (msg.type === "session.start") {
      const resumed = Boolean(msg.sessionId)
      const sessionId = msg.sessionId ?? `sess-${this.nextSession++}`
      this.sessions.push({ws, sessionId, audioBytes: 0, resumed, ended: false})
      ws.send(JSON.stringify({type: "session.ready", sessionId, resumed}))
      return
    }
    if (msg.type === "session.end") {
      const session = this.sessions.find((s) => s.ws === ws)
      if (session) session.ended = true
      return
    }
    if (msg.type === "ping") {
      ws.send(JSON.stringify({type: "pong", t: (msg as {t?: number}).t}))
    }
  }

  // ===========================================================================
  // Driving the conversation — what a test calls instead of speaking
  // ===========================================================================

  private push(payload: Record<string, unknown>): void {
    const session = this.current
    if (!session) throw new Error("FakeTenirServer: no live session to push to")
    session.ws.send(JSON.stringify(payload))
  }

  partial(text: string): void {
    this.push({type: "caption.partial", text})
  }

  final(segmentId: string, text: string, lang?: string): void {
    this.push({
      type: "caption.final",
      segmentId,
      text,
      startMs: 0,
      endMs: 1000,
      ...(lang ? {lang} : {}),
    })
  }

  cue(cueId: string, title: string, body: string, source?: string): void {
    this.push({type: "cue", cueId, title, body, atMs: 0, ...(source ? {source} : {})})
  }

  translation(segmentId: string, text: string, sourceLang?: string): void {
    this.push({type: "translation", segmentId, text, ...(sourceLang ? {sourceLang} : {})})
  }

  translationDone(): void {
    this.push({type: "translation.done"})
  }

  song(songId: string, title: string, artist: string, lines: {atMs: number; text: string}[]): void {
    this.push({type: "song", songId, title, artist, atMs: 0, offsetMs: 0, lines})
  }

  songSync(songId: string, offsetMs: number): void {
    this.push({type: "song.sync", songId, atMs: 0, offsetMs})
  }

  songDone(songId: string): void {
    this.push({type: "song.done", songId})
  }

  error(code: string, message: string, fatal = false): void {
    this.push({type: "error", code, message, fatal})
  }

  /** Drop the socket the way a flaky network would (client should reconnect). */
  dropSocket(code = 1006): void {
    const session = this.current
    if (!session) return
    session.ended = true
    session.ws.close(code, "dropped")
  }
}

/**
 * A minimal, valid 16 kHz mono s16le WAV: a 44-byte header over a few
 * milliseconds of silence. Real enough that a browser's `<audio>` element
 * loads and reports a duration, small enough to keep the harness quick.
 */
function silentWav(samples = 800): Uint8Array {
  const dataBytes = samples * 2
  const buf = new ArrayBuffer(44 + dataBytes)
  const view = new DataView(buf)
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i))
  }
  ascii(0, "RIFF")
  view.setUint32(4, 36 + dataBytes, true)
  ascii(8, "WAVE")
  ascii(12, "fmt ")
  view.setUint32(16, 16, true) // PCM chunk size
  view.setUint16(20, 1, true) // PCM
  view.setUint16(22, 1, true) // mono
  view.setUint32(24, 16000, true) // sample rate
  view.setUint32(28, 16000 * 2, true) // byte rate
  view.setUint16(32, 2, true) // block align
  view.setUint16(34, 16, true) // bits per sample
  ascii(36, "data")
  view.setUint32(40, dataBytes, true)
  return new Uint8Array(buf)
}
