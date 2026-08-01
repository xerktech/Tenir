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

## Creative & product ideas

The tiers above fix the pipeline; these use the same data to make the *product*
richer. They are independent of each other and of P1–P9 (though several get
better with the P1 tag), and each would be its own ticket. Ordered roughly by
value-for-effort.

**C1. The session soundtrack.** History already drops a `♪ TITLE — ARTIST`
marker per song (`songs` table, `at_ms`). Roll them up into a per-conversation
"soundtrack" strip — every track that played, in order — and make it exportable:
a text list, or ready-made search links (Spotify / YouTube / Apple Music take
`artist title` query URLs, so no API keys or accounts). "What was that song from
dinner last night?" becomes a solved problem. Web + Android + glasses phone
History in parity.

**C2. Jump to the moment.** Full session audio is retained
(`persistence/audio.py`) and each song row carries `at_ms` on the session
timeline. Make the ♪ marker a deep link that starts history playback at that
offset — and conversely, when replaying audio, surface the song marker as the
playhead crosses it. Cheap (the data is already joined in
`ConversationDetail`), and it turns songs into the natural landmarks people
actually remember conversations by ("it was right after that song came on").

**C3. Ambient now-playing / scrobbling.** Tenir already is an ambient music
recognizer; expose it. A `now_playing` field on the session REST surface plus
an optional outbound webhook on `song` / `song.done` makes the household's
music state available to home automation, and an opt-in Last.fm-style scrobbler
falls out of the same hook. Strictly opt-in and off by default — it exports
listening data from a self-hosted system, so it must be a deliberate choice
(same posture as `music_lyrics_endpoint` for on-prem LRCLIB).

**C4. Karaoke mode.** The lyric window is fixed at one line behind / two ahead
(`LYRIC_LINES_BEFORE`/`AFTER`, `packages/client-core/src/captureSession.ts`).
A per-session toggle that widens the look-ahead and leads the anchor by a
second or two turns the glasses into a prompter — you see the line *before*
you have to sing it. Almost entirely client-side; the sync machinery already
does the hard part. (Pairs with the "singing along" open question: karaoke mode
is the one context where suppressing the user's own singing is unambiguously
right.)

**C5. Lyric translation for foreign songs.** We suppress *transcript*
translation during songs (P3) — but translating the *lyric lines themselves*
is the feature that gate accidentally hints at. On demand (not per-line spam),
run the fetched `SyncedLine` texts through the existing translation backend
once per song and show original + translation stacked in the lyric card.
Language learners get subtitled music for free. Display-only, like the lyrics
themselves — nothing new is persisted, so the licensing posture is unchanged.

**C6. Track card for instrumental / no-lyrics songs.** When LRCLIB has nothing
synced, the card today shows a bare title over `♪ ♪ ♪`. Shazam's response
already carries more than we keep (album, year, artwork URL) — `_parse_match`
drops it. Keep album/year in `MusicMatch` and let the no-lyrics card show a
small track card instead of placeholder notes. Low effort, and it makes the
feature feel finished for the large minority of tracks with no synced lyrics.

**C7. Ambience as session metadata.** "Music was playing" is a strong proxy
for *where you were* (dinner, bar, gym, party). Aggregate song-run coverage
into a per-conversation ambience hint (e.g. `music_ms` alongside duration) and
use it two ways: as a history filter chip ("sessions with music"), and as a
pipeline hint — sessions with heavy music coverage could run a more
conservative VAD or suppress cue generation entirely, the way a human
assistant knows not to interject at a party.

**C8. A free labeled corpus for STT robustness.** A P2-flagged segment is a
rare artifact: real-world music-over-mic audio *with known ground-truth text*
(the lyric lines it matched). Harvested from retained audio, that's a
benchmark set for exactly the failure XERK-182 heuristics guard against —
feed it to `scripts/stt_eval/` to score STT models on music robustness, tune
the hallucination guard against data instead of anecdotes, and regression-test
future model swaps (`docs/stt-model-selection.md`) on the hardest ambient case
we actually encounter. Internal tooling only; nothing ships to clients.

**C9. Listening stats.** Self-hosted means the household owns its data: a
small stats view over the `songs` table — most-heard tracks and artists, by
week or by conversation — is a join away. Pure novelty, but the kind users
show people. (Keep it household-scoped like everything else; it's derived from
rows we already store, so no new privacy surface.)

**C10. Duration-aware scan scheduling.** `MusicMatch.duration_ms` plus the
current offset tells us *when the song will end* — the scan loop ignores this
today and re-checks on a flat cadence (`music_lock_interval_ms`). Scheduling a
scan just after the predicted end catches the next track seconds after it
starts (instead of up to 18 s late), and skipping mid-song re-checks when sync
is corroborated (P4/P5) spends fewer Shazam calls for a snappier box. Pure
`_music_scan_loop` change, tunable with the existing replay harness.

## Suggested sequencing

1. **P1** — contract field + `segments.song_id` + clients dim live sung
   captions (one PR: schema, api, three front ends, tests on the stub path).
2. **P2** — retain lyrics in run state, matcher + `sung` flag + metric;
   thresholds tuned offline against retained recordings.
3. **P3** — search exclusion, translation gating, history collapse.
4. **P4** — lock corroboration from the P2 matcher.
5. Re-evaluate P5/P6 once 1–4 are live; P7–P9 stay parked.

The C-ideas slot in independently whenever wanted — C1, C2, C6 and C10 are
cheap and need nothing from the tiers; C8 wants P2's flag first; C4/C5/C7 are
product calls to make deliberately.

## Open questions

- **Singing along.** While a track is locked, a user singing along transcribes
  as the song and gets suppressed — correct for transcript quality, but it
  erases "we sang happy birthday". Default: treat as song; revisit if it stings.
- **Match thresholds.** Token-overlap ratio, minimum token count, and sync
  slack all need offline tuning before P2 ships; the eval harness exists.
- **Partials.** P1 should stamp partials too (cheap, same code path), but P2
  matching on partials is likely wasted work — finals only, and let the live UI
  dim by `songId` instead.
