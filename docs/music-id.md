# Music ID — song recognition + synced lyrics (XERK-184)

When a song is playing in the room, Tenir recognizes the track and shows its
**time-synced lyrics** in the same box a cue uses — titled `ARTIST - SONG NAME`,
the lyrics **auto-scrolling** as the song plays. It is the fourth "derived aside"
built on the cue/translation pattern (an `off`/`stub`/real backend, a WebSocket
contract message, off-loop work that never disturbs captions, persistence +
history, parity across web + Android + the glasses lens), with two differences
that make it new:

1. **It is audio-driven, not transcript-driven.** Cues and translations run off
   finalized transcript turns; recognition needs the raw mic audio. So the
   session taps `on_audio` into a bounded rolling window and runs a **scan loop**
   of its own, not off the caption pump.
2. **It auto-scrolls in real time.** The client advances the lyric window from a
   server-provided anchor off its own clock — a primitive cues never needed.

Where it appears:

- **Live** (web + Android + the glasses phone Session page): a bordered card in
  the cue box's slot, title `ARTIST — SONG NAME` over a four-line window of the
  lyrics — one already-sung line, the current line (highlighted), and two
  upcoming — scrolling as the song plays. A live song **owns the box**: the cue
  band is hidden while it shows, exactly as cues stand aside for a translation
  run.
- **Glasses lens**: the same full-width popup box the cue/translation use, its
  body the scrolling lyric window (a documented platform exception: monochrome,
  its own line count).
- **History** (web + Android + glasses phone): each recognized song renders
  inline on the transcript timeline as a quiet `♪ ARTIST — TITLE` marker at the
  point it played. The lyrics themselves are **not** stored — they are
  re-fetchable and licensing-sensitive — so history keeps only the identity.

## How it works

1. **Recognition is audio-driven and off the caption path.** `on_audio` copies
   each PCM chunk into a bounded ~8s rolling window
   (`api/src/api/session.py`). A background **scan loop** periodically packages
   that window as a WAV and calls the music service; a slow or failing call
   never touches captions. The cadence adapts — while searching it backs off on
   repeated misses (`music_scan_interval_ms`, doubling up to
   `music_scan_max_interval_ms` for rate-limit hygiene), and while a song is
   locked it re-checks less often (`music_lock_interval_ms`), because the
   client's local clock carries the scroll between checks.
2. **The service recognizes, then fetches lyrics** (`api/src/api/music/`).
   `MusicService.identify(wav)` returns the track plus the **play-offset into the
   song at the end of the window** (`MusicMatch.offset_ms`) — the sync anchor.
   `MusicService.lyrics(match)` returns the track's **time-synced LRC** lines.
   Recognition is *slow and variable* (Shazam can spend up to its hard timeout),
   so before emitting the anchor the session **advances the offset by the wall
   time the identify + lyric-fetch consumed** (`_synced_offset_ms`, XERK-188):
   `offset_ms` is the play position at the window's end, and the client stamps its
   anchor on arrival, so without this the scroll would start seconds behind the
   music and each re-sync would land a *different* lag — big, jumpy corrections.
   Compensating leaves only the small, roughly-constant WS delivery delay.
3. **The run state machine** mirrors the translation run. A confident match
   opens a **song run**: the api sends one `song` frame carrying the full synced
   lyrics and an anchor (`atMs` = the session-timeline position, `offsetMs` = the
   play position there). While the same track keeps matching, it re-identifies
   and sends `song.sync` (a fresh anchor, no lyrics) to correct drift. The run
   ends with `song.done` in one of three ways: it **plays to its end** (song-end
   prep, XERK-192), it **stops matching** past the hold window (`music_hold_ms`),
   or a **different song takes over**. A takeover is **debounced** (XERK-187): a
   different track must match twice in a row to replace a locked one, because
   crossfaded/DJ-blended windows flap between the outgoing and incoming track and
   would otherwise reset the box several times per blend. A live song run
   **suppresses cues** (gated in `session.py` beside the translation flag). Song
   and translation are otherwise **independent streams** (XERK-194): music
   recognition is not gated on a translation run, so both can be live at once and
   the web/mobile/phone surfaces render both. Only the glasses share one popup box
   for them, and there precedence is **lyrics > translation > cue** — arbitrated on
   the lens (`even/src/lens/controller.ts`), not the server.

   **Song-end prep (XERK-192).** The hold alone left the box lingering on the last
   lyric long after a song *finished* — the hold is deliberately long (it must
   survive quiet mid-song passages that miss scans), so a track that simply ended
   sat on screen until it expired. But the run already carries the full synced
   lyrics and (usually) the track duration, so the session *knows* when the song
   is over: on open (and each `song.sync`) it fixes an **end position** — the
   reported duration, else the last lyric line plus `music_end_tail_ms` — and
   schedules a precise `song.done` for that moment off the same anchor the scroll
   uses. The hold now only covers a song **cut short**; a song **played to the end**
   dismisses right when it ends. As that end nears (within `music_end_prep_ms`) the
   scan also tightens from the slow locked cadence back to the search cadence, so
   the **next** track is recognized and shown promptly rather than after one more
   slow re-check — "either a new song, or the lyrics disappear quickly."
4. **The scroll is client-driven** (`packages/client-core/src/captureSession.ts`).
   From the anchor, the client computes the song position at any moment as
   `offsetMs + (now - anchorAt)` and the current line as the last one whose
   (song-time) `atMs` has been reached (`currentLyricIndex`), then renders a
   window around it (`lyricWindow`). Because the position is *derived* from the
   anchor rather than counted per tick, a throttled tab or a backgrounded app
   resyncs to the truth on the next tick instead of drifting — the same
   discipline as the cue countdown (`cueSecondsLeft`). Periodic `song.sync`
   frames re-anchor it — and because the api latency-compensates each anchor
   (point 2), those re-syncs are small nudges rather than multi-second jumps.
5. **Delivery + persistence.** `song` / `song.sync` / `song.done` are WebSocket
   messages (`contract/ws-messages.schema.json`); the song identity is persisted
   to the `songs` table (`schema.sql`) at `at_ms` so history renders it inline.

## Backends (`API_MUSIC_BACKEND`)

| Value    | Behaviour                                                             |
|----------|----------------------------------------------------------------------|
| `off`    | No music ID (default). The stripped core stays STT-only.             |
| `stub`   | Model-free, deterministic recognizer + canned synced lyrics for CI/dev — no network. |
| `shazam` | Real recognition via **shazamio** (Shazam's global catalog) + synced lyrics from **LRCLIB**. |

The stub is what CI exercises end-to-end (scan → `song`/`song.sync`/`song.done`
→ persistence → history), so the whole path is covered without a network call.

## Recognition: shazamio (global) + LRCLIB (lyrics)

Global ambient recognition needs a global fingerprint catalog, which no
self-hosted engine has — so recognition uses **shazamio**, a free,
reverse-engineered client for Shazam's own backend. It matches against Shazam's
global catalog (best-in-class ambient accuracy), needs no account and costs
nothing per call, and runs **in-process** (async + a Rust fingerprinter) — there
is no extra container. `shazamio` is an **optional** api dependency (`pip install
-e 'api[music]'`), lazily imported, so CI (the stub) never needs it. ffmpeg on
PATH is recommended for decoding; the 16 kHz mono WAV window works without it.

Caveats, and how they are handled:

- **Unofficial / ToS-gray.** shazamio impersonates the Shazam mobile client
  against Apple's private endpoints. It can break if Shazam changes their
  backend, and has no SLA. The `off`/`stub`/`shazam` factory keeps a paid
  provider (AudD, ACRCloud) a **one-module drop-in** behind the same seam if it
  ever does.
- **Rate limits.** Scanning is conservative and backs off while searching, to
  avoid hammering one IP.
- **Best-effort.** Any recognition or lyric-lookup failure degrades to "no song
  shown"; captions are never affected.

**Lyrics** come from **LRCLIB** (lrclib.net) — free, open (MIT), key-less, ~3M
**time-synced** LRC lyrics — keyed by the recognized artist/title (+ duration
when known), cached per track. Point `music_lyrics_endpoint` at a self-hosted
LRCLIB to keep lyric lookups on-premises. A track with only plain (untimed)
lyrics, or none, shows the title without a scroll.

### Running it on the single-host stack

```bash
# Model-free demo (no network; deterministic canned song + lyrics):
API_MUSIC_BACKEND=stub docker compose up --build

# Real recognition (needs the [music] extra installed in the api image and
# outbound access to Shazam + LRCLIB):
API_MUSIC_BACKEND=shazam docker compose up --build
```

Tuning lives in `api/src/api/config.py` (`API_MUSIC_*`): `music_scan_interval_ms`,
`music_scan_max_interval_ms`, `music_lock_interval_ms`, `music_hold_ms`,
`music_min_confidence`, `music_identify_timeout_ms`, `music_window_seconds`,
`music_end_tail_ms`, `music_end_prep_ms`, `music_lyrics_endpoint`. The defaults
were tuned against real recorded sessions
in XERK-187: the hold spans two failed lock-cadence re-checks (quiet passages
routinely miss 1–3 consecutive scans mid-song), the search backoff decays to a
minute-scale ceiling because Shazam rate-limits aggressive scanning from one IP,
and each recognition call carries a hard timeout because shazamio's retrying
HTTP stack was observed spending 10+ minutes on one call on a flaky upstream.

## Where this can go next

[`docs/music-lyrics-usefulness.md`](music-lyrics-usefulness.md) (XERK-193)
brainstorms how the song data — the run flag, the sync anchor, the synced lyric
lines — can improve the rest of the pipeline (tagging and suppressing sung
captions, search/translation hygiene, lyric-corroborated lock keeping) and what
products it enables (session soundtracks, replay landmarks, karaoke mode,
now-playing hooks).

## Verifying against real music

`scripts/music_eval/` replays a **recorded session's audio** through the shipped
recognizer and reports what the live box would have shown — recognized songs,
their timeline position, the sync offset, and LRCLIB lyric availability. It needs
the `[music]` extra, network, and a stored WAV; see
[`scripts/music_eval/README.md`](../scripts/music_eval/README.md). Ambient
recognition and the Shazam/LRCLIB network calls are validated this way (manually,
like the STT model servers), not in CI.
