# Music / lyrics usefulness — using song data beyond the cue box (XERK-193)

Music ID (XERK-184…191) gives us something rare mid-session: **ground truth**.
While a song run is locked we know the track, we know every lyric line with
millisecond timing, and we know where in the song we are right now. Today that
knowledge drives exactly one thing — the scrolling lyric card — while the rest
of the pipeline behaves as if it doesn't exist: the STT transcribes the song
badly, the garbage is sent to clients *underneath the accurate lyrics*, persisted
to Postgres, indexed for search, and (for foreign-language songs) even shipped to
the translation LLM.

This doc brainstorms what else that data can do, grounded in the current
architecture, and proposes a sequencing. It is analysis, not a spec — each tier
should become its own ticket(s).

## What we know during a song run (and where it lives)

- **The run flag.** `Session._music_active`, `_music_run_id`, `_music_track_key`
  (`api/src/api/session.py`) — live in the same object that drains caption
  results, so "was this segment produced during a song?" is one comparison away.
- **The timing bridge.** The anchor pair (`atMs` on the session timeline,
  `offsetMs` into the song) sent in `song` / `song.sync` maps any session
  timestamp to a song position. Caption finals carry session-timeline
  `startMs`/`endMs`; lyric lines carry song-relative `atMs`. The bridge lets us
  ask, for any final, *which lyric lines were playing while it was spoken*.
- **The lyrics themselves.** `MusicService.lyrics()` returns the full synced LRC
  line list — which the server currently **forgets** right after converting it to
  the outbound `Song` frame (`_open_music_run`). Retaining it in the run state is
  the enabling change for almost everything below.
- **What does not exist today:** no per-segment music flag, no filtering or
  annotation of captions while `_music_active`, no music/speech classifier, no
  source separation. The only music↔caption interaction is cue suppression, and
  the only content-heuristic suppression precedent is the XERK-182
  hallucinated-final guard (`api/src/api/stt/streaming.py`,
  `stage.stt.final_recovery_suppressed`).

## Tier 0 — the foundation: tag captions produced during a song

**P1. Stamp every caption emitted during a song run with the run's `songId`.**

Add an optional `songId` to `caption.partial` / `caption.final` in
`contract/ws-messages.schema.json` (schema-first, `make gen`) and a nullable
`song_id` column on `segments`. The stamp is applied in the one place a final is
known, unsent, unpersisted, and adjacent to the music state:
`Session._drain_results`.

This changes no behaviour by itself, which is exactly why it goes first: it is
the substrate every other proposal (suppression, search hygiene, UI
de-emphasis, history grouping) keys off, it is fully exercised by the stub
backend in CI, and it ships client parity trivially (web + Android + glasses
all read the same contract field).

## Tier 1 — high value, near term

**P2. Lyric-match suppression: transcribe only *spoken* words during a song.**

The ticket's "block out song audio" — done safely. Never gate the microphone;
instead, reclassify text we can *prove* is the song:

1. Retain the `SyncedLine` list in the run state (today it's discarded).
2. When a final lands in `_drain_results` during a run, map its span to song
   time via the anchor, select the lyric lines within that span (± a few
   seconds of sync slack), normalize both sides (same discipline as
   `agreement._norm`), and score token overlap.
3. A strong match ⇒ the STT was transcribing the song ⇒ mark the segment
   `sung` (and let clients hide it) rather than presenting it as speech. No
   match ⇒ someone talked over the music ⇒ keep it, untouched.

Prefer **persist-with-flag over drop**: store the segment with `sung = true`
and filter at display/search time. That is non-destructive (thresholds can be
retuned later against retained audio), and it mirrors how XERK-182 was
validated. Emit a `music.captions_suppressed` metric beside the existing music
metrics.

The position window is what makes this precise: speech that merely *quotes* a
lyric ("I love the line 'hello darkness'") only matches if it coincides with
that exact moment of the song — vanishingly unlikely — so false suppression of
real speech stays rare. When LRCLIB has no synced lyrics for the track, P2
simply doesn't apply (tag-by-time from P1 still does; blanket time-based
suppression is too aggressive because people talk over music).

Validation is already in place: retained session WAVs + `scripts/music_eval/`
+ `scripts/stt_eval/` can replay real recordings through both the recognizer
and the STT stack offline. The tuning metric that matters most is **precision
on speech** — spoken words wrongly suppressed are the one unacceptable failure.

**P3. Downstream hygiene keyed off the tag.**

- **Live UI:** hide or dim captions carrying the active `songId` — the direct
  fix for "accurate lyrics with completely wrong transcription below", shipped
  in parity on web, Android, and the lens.
- **Search:** exclude `sung` segments from the FTS query path. Searching your
  own conversations for "love" should not return every pop song that played in
  the kitchen.
- **Translation:** skip `_consider_translation` for song-tagged segments. A
  foreign-language song currently burns an LLM call per sung turn — and since
  precedence is translation > song, its own translation popup can evict the
  lyric card it's competing with. (Worth verifying as a live bug regardless of
  this ticket.)
- **History:** collapse sung segments under the existing inline `♪ TITLE —
  ARTIST` marker (expandable), instead of interleaving garbage with real
  conversation.
- **Future features:** when summaries / cues-over-history / RAG return, the
  flag keeps song text out of their inputs for free.

**P4. Use STT↔lyric agreement to strengthen the music lock itself.**

Quiet passages routinely miss 1–3 consecutive Shazam scans mid-song — that's
why `music_hold_ms` is 45 s. But if recent finals are matching upcoming lyric
lines (the P2 matcher, reused), the song is *demonstrably still playing*: treat
that as a free positive re-lock signal — reset `_music_misses` and the hold
clock without a network call. Fewer Shazam calls (rate-limit hygiene), less
flap at song boundaries, and it can corroborate the XERK-187 takeover debounce.

## Tier 2 — worthwhile, needs more design

**P5. The transcriber as a sync microphone.**

Between lock re-checks (18 s cadence) drift correction comes only from Shazam
offsets. A final that matches lyric line *N* pins the song position at that
moment independently — the server could emit a `song.sync` anchored on it, or
use it to bound drift and *raise* `music_lock_interval_ms` (fewer Shazam
calls). Needs guardrails: only nudge on high-confidence matches, cap the
correction size, never fight a fresh Shazam anchor.

**P6. Karaoke correction: show canonical lyrics instead of the mis-transcription.**

Where P2 hides sung text, P6 *replaces* it with the true lyric line — the
best-looking transcript possible. The catch is deliberate policy: lyrics are
**not stored** (licensing; see `docs/music-id.md` and the `songs` schema
comment), and persisting corrected segment text is persisting lyrics with extra
steps. If pursued, make it **live-display-only** (clients already hold the full
line list) and keep storage on the P2 flag. Honestly, P2 + the lyric card
already show the user the right words — P6 may be redundant.

**P7. Lyric-biased decoding.**

Feed the upcoming lyric window to the STT as hotwords / a decoding prompt.
Neither the Parakeet nor Nemotron servers expose biasing today, and P2/P6 cover
the visible symptom without it. Parked unless the model servers grow support.

## Tier 3 — heavy, revisit only on demand

**P8. Source separation** (demucs-class vocal/accompaniment split) to improve
accuracy of *speech spoken over music* — the one thing no proposal above
touches (P2 removes sung garbage; it doesn't help the STT hear a person over a
chorus). Real GPU cost, added latency, a new container. Only worth it if
speech-over-music accuracy surfaces as an actual complaint.

**P9. Reference-track cancellation.** We know the exact track and offset, so in
principle we could subtract the reference audio from the mic signal. Obtaining
reference audio is a licensing and practicality dead end; listed for
completeness.

## Suggested sequencing

1. **P1** — contract field + `segments.song_id` + clients dim live sung
   captions (one PR: schema, api, three front ends, tests on the stub path).
2. **P2** — retain lyrics in run state, matcher + `sung` flag + metric;
   thresholds tuned offline against retained recordings.
3. **P3** — search exclusion, translation gating, history collapse.
4. **P4** — lock corroboration from the P2 matcher.
5. Re-evaluate P5/P6 once 1–4 are live; P7–P9 stay parked.

## Open questions

- **Singing along.** While a track is locked, a user singing along transcribes
  as the song and gets suppressed — correct for transcript quality, but it
  erases "we sang happy birthday". Default: treat as song; revisit if it stings.
- **Match thresholds.** Token-overlap ratio, minimum token count, and sync
  slack all need offline tuning before P2 ships; the eval harness exists.
- **Partials.** P1 should stamp partials too (cheap, same code path), but P2
  matching on partials is likely wasted work — finals only, and let the live UI
  dim by `songId` instead.
