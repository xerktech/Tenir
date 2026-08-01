# STT eval results — 2026-07

First run of the harness against the live deployment. Follows the honesty
rules in the README: numbers below are **divergence vs stored production
output** (no hand-corrected references exist yet), so they validate the
harness and characterize the deployment — they are not yet absolute accuracy.
No transcript content appears here; the per-segment evidence lives in the
session scratchpad review sheet.

## Setup

| | |
|---|---|
| Eval set | 74 conversations / 3011 segments with retained audio (2026-07-27 export) |
| Frozen set going forward | **73 conversations / 2465 segments** — one live/unfinished session excluded (51 min of transcript, 2 s of audio on disk) |
| Endpoint | `http://10.10.10.22:9401`, `nvidia/parakeet-tdt-0.6b-v3` (the deployed model) |
| Settings | production-shaped: language auto, word timestamps on, serialized requests |
| Scoring | jiwer; NFKC + lower-case + punctuation stripped, per README |

## Baseline scorecard (deployed Parakeet replayed on its own output)

| slice | convs | words | divergence |
|---|---|---|---|
| all scored segments | 74 | 42 470 | 7.80 % |
| clean conversations (<10 % conv divergence) | 44 | 33 104 | **1.00 %** |
| problem conversations (≥10 %) | 30 | 9 366 | 31.86 % |

The 1.00 % on clean conversations is the harness validating itself: replaying
the deployed model over well-aligned audio reproduces the stored transcript
almost exactly (residual is boundary word-splits and TDT tie-breaks). The
divergence lives almost entirely in three failure classes, none of which is
model inaccuracy:

1. **Audio/transcript timeline damage.** The 1-hour conversation
   (`3e8e18a7`) diverges 62 % in its first third and 0.0–0.1 % in the rest:
   the early retained audio does not correspond to the early transcript
   (resume/chunk-loss shape). One conversation's transcript span exceeds its
   audio by 11 s; another's audio exceeds its span by 15 s. **Product
   implication beyond eval:** History audio playback is silently misaligned
   with the transcript for these conversations. Deserves its own
   investigation/ticket.
2. **Ambient media/music audio.** Sessions dominated by TV or singing
   (e.g. `763fd1d5`, 83 %; `bbead742`, 92 %): fixed-boundary slices of music
   often decode to empty where the live turn-buffer decode produced lyrics.
   These are the control conversations the cue eval also uses — keep them in
   the frozen set, but score them separately.
3. **Segment-boundary spill.** Stored `start_ms`/`end_ms` (caption timing)
   can sit ~a word off the live turn-buffer boundaries, so neighboring words
   leak into or drop out of a slice. Shows up as first/last-word errors on
   otherwise-identical text. `--pad-ms` exists to probe this.

## Speed — the actionable finding

Server-side decode time equals client wall time (network is negligible), and
it is **flat (~670 ms) regardless of clip length from 0–30 s**:

| mode | p50 / decode | p95 | RTFx |
|---|---|---|---|
| word timestamps ON (what production finals request) | 675 ms | 740 ms | 10 |
| word timestamps OFF (3-conversation subset, same segments) | **122 ms** | — | 64 |

Identical normalized text in both modes on the subset (0/79 segments differ).
**~550 ms of every production final decode — 5.5× — is NeMo word-timestamp
computation**, not transcription. Every final caption a user sees waits on it
(`want_words=True` in `api/src/api/stt/streaming.py`). If per-word caption
timing can come from the Nemotron partial stream (or be computed async, or be
dropped), finals get ~5× faster for free, on the model we already run. This is
the cheapest latency win found so far — cheaper than any model migration.

## Next steps

1. Hand-correct references for the frozen set, worst-divergence first
   (`review.tsv`), to convert divergence numbers into authoritative WER.
2. File/fix the audio-timeline damage class (also a History-playback bug).
3. Prototype finals without synchronous word timestamps and measure the
   user-visible caption latency change.
4. Candidate-model comparison on the same frozen set + refs (e.g.
   `parakeet-unified-en-0.6b` if an English-only mode is on the table;
   Nemotron streaming via its published `ws://10.10.10.22:9403` endpoint
   needs a WS client mode in the harness first).

## 2026-08-01 — Turn-close windows retuned (XERK-175)

Long turns were making translations (and cues) land late: both run only on
FINALIZED turns, so a word spoken early in a turn waits out the rest of the
turn before the pipeline can even see it. Measured over the whole July
deployment (105 conversations / ~7.8 h of retained audio) by replaying every
WAV through the real `StreamingTranscriber` VAD offline (`segment_sim.py`,
added with this cycle):

| config (silence/cap) | speech turns | closed on cap | len p50 | wait-for-final mean | p90 | max |
|---|---|---|---|---|---|---|
| **700/12000 (shipped before)** | 5475 | 20 % | 4.7 s | 4.69 s | 9.6 s | 12 s |
| 700/8000 | 6425 | 33 % | 4.8 s | 3.43 s | 6.6 s | 8 s |
| 700/6000 | 7464 | 44 % | 5.0 s | 2.70 s | 5.1 s | 6 s |
| 500/12000 | 7841 | 8 % | 3.0 s | 3.78 s | 8.4 s | 12 s |
| **500/8000 (shipped now)** | 8490 | 16 % | 3.1 s | 2.96 s | 6.1 s | 8 s |

(wait-for-final = per 100 ms speech chunk, time until its turn's final; the
stored-segment export cross-checks the baseline: 27.6 % of stored July turns
sat at the 12 s cap, 33.7 % of translated ones.)

The stand-out finding is the silence window, not the cap: many real pauses
fall between 500 and 700 ms, so 500 ms *more than halves* forced cap-closes
(20 % → 8 % at the old cap) — turns end at actual pauses instead of arbitrary
mid-speech chops. The tighter 8 s cap then bounds the worst case (music /
wall-to-wall speech) without re-inflating forced closes past the old rate.

Quality spot-check (re-transcribed candidate boundaries on the production
Parakeet, re-translated non-English spans on the production `gpt-oss:120b`,
hand-read): silence-closed turns at 500/8000 read as complete clauses;
the 1-hour bilingual conversation captured slightly *more* normalized words
than the 700/12000 replay (4699 vs 4349) with p50 turn length halved
(6.9 s → 3.8 s); the Spanish music session (no pauses ever — every turn caps)
simply delivers its translations every 8 s instead of every 12 s, per-chunk
translation quality comparable on read. Final-decode cost per turn is flat
(~80–170 ms regardless of length), so ~55 % more finals is noise for the GPU.

Residuals / next steps for turn latency:

1. The translation model call itself measured 1.8–4.6 s per turn on the
   deployed `gpt-oss:120b` — now the dominant term for translation delivery.
   A smaller/faster translation model or streaming delivery is the next lever.
2. Shorter turns mean `cue_context_segments = 8` spans ~25 s instead of ~37 s
   of conversation; if stale-context cues appear, re-run the cue eval with a
   larger window rather than re-inflating turns.
3. The multilingual test session (`159fe63c`) retained no audio
   (`audio_key` NULL) and could not be replayed — worth understanding why
   before the next eval cycle.
