# Speaker tracking results — Nov 2026 (XERK-2958)

All runs: the installed api's `StreamingTranscriber` → real Whisper large-v3-turbo (`tenir-stt`) →
diarizer on CPU (same code as the image). Sessions from `build_session.py` on LibriSpeech
dev-clean; ~3 min each. Numbers are word shares (see README "Reading the score").

## Sessions

| id | build flags | overlap |
|---|---|---|
| s1 | `--speakers 4 --seed 1` | turn edges, 30 % of turns |
| s2 | `--speakers 4 --seed 2` | turn edges, few |
| s3 | `--speakers 3 --seed 3 --overlap 0.5` | turn edges, half of turns |
| p1 | `--speakers 4 --seed 4 --overlap 0.1 --parallel` | two conversations at once: 87 % of speech |

The real recordings that prompted the ticket (3 dinner conversations, Spanish + English) score
78 % of speech as overlapped — p1 is the synthetic stand-in for them.

## Sortformer streaming setup (frame level, no STT)

Single-speaker frames given the right speaker:

| setup | s1 | s2 | s3 | CPU time (s1) |
|---|---|---|---|---|
| 1.04 s (chunk 6, rc 7) | 0.857 | 0.927 | 0.978 | 284 s |
| 10 s (chunk 124, rc 1) — **shipped** | 0.936 | 0.927 | 0.981 | 16 s |
| 30 s (chunk 340, rc 40) | 0.780 | | | 9 s |
| 3 s (chunk 31, rc 7) | 0.749 | | | 48 s |

## End to end (shipped: 10 s setup, separation only on overlap ≥ 1.5 s)

| session | baseline recall | recall | **attributed** | attributed, overlapped | precision (baseline) |
|---|---|---|---|---|---|
| s1 | 0.939 | 0.943 | **0.884** | 0.851 | 0.942 (0.948) |
| s2 | 0.962 | 0.954 | **0.929** | 0.927 | |
| s3 | 0.790 | 0.790 | **0.764** | 0.681 | 0.882 (0.899) |
| p1 | 0.546 | 0.648 | **0.545** | 0.545 | 0.783 (0.920) |

- Labels are consistent: every true speaker's words sit mostly under one label, and labels map
  one-to-one in arrival order (s1: 1→1 … 4→4).
- Separation trade-off, measured three ways (none / every overlap ≥ 0.48 s / only ≥ 1.5 s):
  - turn-edge overlap (s1, s3): no attribution gain, precision 0.942 → 0.926 and 0.882 → 0.821
    when separating every overlap. Whisper already hears both voices there; separating only
    adds invented words. Hence the 1.5 s floor.
  - parallel conversations (p1): recall 0.611 → 0.648, attributed 0.471 → 0.545, precision
    0.874 → 0.783. The only place separation recovers words the mix loses.
- Known limits:
  - Sustained 4-way talk is hard for one mic: about half the words come through credited correctly.
  - p1's precision means about 1 word in 5 is garbled or invented there.
  - Sortformer tracks at most 4 speakers.

## After QA fixes (planner tiling, absorb/join across overlaps)

The table above predates three planner fixes from QA (a solo piece swallowing a later overlap
in the same turn). Re-run on the final code:

| session | recall | attributed | attributed, overlapped | precision |
|---|---|---|---|---|
| s1 | 0.918 | 0.880 | 0.845 | 0.950 |

Recall dips 2 points against the no-diarizer baseline (0.939): words at piece edges that a
whole-turn decode keeps. Attribution and precision hold.

## Real recordings (hand read, not scored)

- The incremental driver reproduces NeMo's whole-file streaming pass on a real recording
  (max abs diff 1.3e-6); turn-end provisional labels agree with the final ones on 98–100 % of frames.
- A 3-min excerpt of the 11-min dinner: the English TV-show thread and the Spanish politics
  thread come out as separate captions under different speakers where they overlap, which the
  stored unlabelled transcript had collapsed to one voice. Separated pieces also produced lines
  no one may have said — the p1 precision cost, seen live.
