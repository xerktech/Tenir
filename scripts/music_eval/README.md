# Music ID eval (`scripts/music_eval/`)

Replays a recorded session's **audio** through the shipped Music ID recognizer
(`api.music.shazam.ShazamMusicService` — shazamio recognition + LRCLIB synced
lyrics) to verify, on real music, exactly what the live feature would have shown:
which songs are recognized, where on the session timeline, the play-offset the
lyric box would anchor on, and whether LRCLIB has synced lyrics for them.

Not part of CI: it needs network access to Shazam + LRCLIB and real recorded
audio. Install the recognizer first:

```bash
cd api && pip install -e '.[music]'   # shazamio; ffmpeg recommended on PATH
```

## 1. Export a session's audio from a deployment

Audio is retained as `{household}/{conversation_id}.wav` under the app's
`API_AUDIO_DIR` (`/data/audio` in the container). List recent sessions and copy
their WAVs out of the `Tenir` container (adjust the node/container/DB names to
your deployment):

```bash
# Recent conversations (last 6h) with retained audio:
ssh NAS01-TrueNAS "docker exec Tenir-Postgres psql -U tenir -d tenir -tAc \
  \"select household, id from conversations \
    where started_at > now() - interval '6 hours' and audio_key is not null \
    order by started_at desc\""

# Copy one out (household 'default', id <ID>) to a local WAV:
ssh NAS01-TrueNAS "docker exec Tenir cat /data/audio/default/<ID>.wav" > <ID>.wav
```

## 2. Replay

```bash
python scripts/music_eval/replay.py <ID>.wav [more.wav ...] \
  --scan-seconds 8 --window-seconds 8 --min-confidence 0.5 \
  --json results.json
```

It slides the session's scan window across the audio with the same gating the
live session uses (fixed scan interval, min-confidence gate, track-key dedupe for
"same song → re-sync" vs. "new song") and prints each recognition:

```
=== <ID>.wav (612s) ===
  [  16s] ♪ NEW  Radiohead - Weird Fishes (offset 8s, conf 1.00, 42 synced lines)
  [  32s]   sync Radiohead - Weird Fishes (offset 24s, conf 1.00, 42 synced lines)
  ...
```

`♪ NEW` is a `song` frame (a run opening/replacing); `sync` is a `song.sync`
(the same song continuing, re-anchoring the scroll). `NO synced lyrics` means the
track was recognized but LRCLIB had no LRC — the box would show the title
without a scroll.

## What it validates

- **Recognition** end to end (shazamio signature + Shazam catalog) on real
  mic-captured music, at the real 16 kHz mono window.
- **Sync anchor** (`offset_ms`) the client scrolls from.
- **Lyrics** availability + parse (LRCLIB → the same `SyncedLine`s the box shows).

Tuning knobs (`API_MUSIC_*` in `api/src/api/config.py`) map to the CLI flags, so
a threshold change can be measured here before it ships.
