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
