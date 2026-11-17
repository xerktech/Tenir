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
  - Separated pieces Whisper is unsure of are dropped (XERK-3006, below); p1 precision 0.798.
  - Sortformer tracks at most 4 speakers.

## After QA fixes (planner tiling, absorb/join across overlaps)

The table above predates three planner fixes from QA (a solo piece swallowing a later overlap
in the same turn). Re-run on the final code:

| session | recall | attributed | attributed, overlapped | precision |
|---|---|---|---|---|
| s1 | 0.918 | 0.880 | 0.845 | 0.950 |
| p1 | 0.652 | 0.624 | 0.624 | 0.790 |

s1 recall dips 2 points against the no-diarizer baseline (0.939): words at piece edges that a
whole-turn decode keeps. p1 (parallel conversations) gains on every count: correctly
credited words 0.545 → 0.624, against 0.546 recall and nothing credited without the diarizer.

## Real recordings (hand read, not scored)

- The incremental driver reproduces NeMo's whole-file streaming pass on a real recording
  (max abs diff 1.3e-6); turn-end provisional labels agree with the final ones on 98–100 % of frames.
- A 3-min excerpt of the 11-min dinner: the English TV-show thread and the Spanish politics
  thread come out as separate captions under different speakers where they overlap, which the
  stored unlabelled transcript had collapsed to one voice. Separated pieces also produced lines
  no one may have said — the p1 precision cost, seen live.

## More than 4 speakers: voice fingerprints (XERK-3017)

Sortformer has 4 slots. Each conversation now keeps a TitaNet fingerprint per person and re-points
slots (diarizer/fingerprint.py).

Feasibility (6 and 8 speakers, whole-file Sortformer): the extra people fold into the 4 slots (m6:
three true speakers share slot 3). Nearest-fingerprint on 2 s solo clips picks the right person
99 % of the time. But same- and different-speaker cosines overlap on single clips (m6 same p10 0.19
vs different p90 0.21), so the decision uses ≥ 2 s pieces and running means.

Frame-level accuracy, single-speaker frames, one label ↔ one true speaker (6 s turns, no STT).
Reproduce with `diarize_fp_tune.py` (sweep, or one setting with `--match/--new/--min`). Sessions
(`build_session.py ... --overlap 0.2` unless noted): m6 `--speakers 6 --minutes 4 --seed 6`,
m8 `--speakers 8 --minutes 5 --seed 8`, m6b `--speakers 6 --minutes 5 --seed 16`,
m8b `--speakers 8 --minutes 6 --seed 18`; held out h4/h5/h6/h8/h10 `--speakers N --minutes 5
--seed 31..35`; s1/s2/p1 as in the table at the top.

Tuned jointly (match 0.55 / new 0.30 / min 2.0 s, the best setting with no session below 4-slot):

| session | speakers | 4-slot | fingerprints | labels used |
|---|---|---|---|---|
| s1 | 4 | 0.809 | 0.843 | 4 |
| s2 | 4 | 0.858 | 0.883 | 4 |
| p1 | 4 (parallel) | 0.444 | 0.444 | 4 |
| m6 | 6 | 0.672 | 0.840 | 8 |
| m6b | 6 | 0.794 | 0.890 | 6 |
| m8 | 8 | 0.610 | 0.923 | 9 |
| m8b | 8 | 0.579 | 0.807 | 9 |

Held out (fresh seeds, never tuned on):

| session | speakers | 4-slot | fingerprints | labels used |
|---|---|---|---|---|
| h4 | 4 | 0.937 | 0.937 | 4 |
| h5 | 5 | 0.727 | 0.899 | 7 |
| h6 | 6 | 0.619 | 0.905 | 7 |
| h8 | 8 | 0.531 | 0.786 | 7 |
| h10 | 10 | 0.663 | 0.806 | 8 |

End to end (real Whisper, the shipped transcriber, CPU diarizer):

| session | recall | attributed | precision |
|---|---|---|---|
| h6, 4-slot | 0.929 | 0.576 | 0.935 |
| h6, fingerprints | 0.929 | **0.895** | 0.935 |
| h4, fingerprints | 0.926 | 0.922 | 0.933 |

- No cost to 4-person tables. With 5–10 people, attribution rises 14–31 points.
- Label counts can be off by one or two: a person split over two numbers, or two quiet people
  merged at 8–10 speakers.
- No help when more than 4 talk within the same few seconds (p1-style parallel talk is still
  Sortformer-bound).

## Gating separated captions (XERK-3006)

One diarized run per session recorded, for every piece of every multi-speaker turn, the
separated decode, the mixed-span decode, Whisper's verbose_json `avg_logprob` and the
separated stream's energy/activity correlation; gates were then replayed offline through the
same piece logic (de-dup included). Replaying "keep every separated piece" reproduces the
recorded finals exactly, repeat runs gave identical numbers, and a live run with the gate on
(XERK-3017 code, fingerprints on) scored p1 exactly as its replay did: 0.798 / 0.641 / 0.571.

| p1 policy | precision | recall | attributed |
|---|---|---|---|
| keep every separated piece (XERK-2958) | 0.744 | 0.674 | 0.599 |
| no separation (overlap decoded as the mix) | 0.792 | 0.591 | 0.446 |
| stream correlation ≥ 0.4, else the mix (pre-XERK-3017 run) | 0.783 | 0.609 | 0.469 |
| avg_logprob ≥ −0.4, else dropped | 0.801 | 0.633 | 0.561 |
| **avg_logprob ≥ −0.5, else dropped — shipped** | **0.798** | **0.641** | **0.571** |
| avg_logprob ≥ −0.5, else the mix | 0.796 | 0.659 | 0.553 |
| avg_logprob ≥ −0.6, else dropped | 0.784 | 0.654 | 0.581 |

- s1 (0.935 / 0.899 / 0.793) and s3 (0.907 / 0.897 / 0.680) are unchanged by every logprob gate (s1 separates nothing at the 1.5 s floor; s3's
  two separated pieces decode at −0.25 and −0.17).
- On p1, separated decodes under −0.5 had 36 of 93 words right (39 %); above, 314 of 377 (83 %).
- Stream correlation does not tell invented from real: every threshold trades attribution for
  precision along the line to "no separation". Not shipped.
- Falling back to the mix for a gated piece scores the same precision with lower attribution,
  and costs a second decode. Dropped instead.
- MossFormer2 (ClearerVoice `MossFormer2_SS_16K`) in place of sepformer-whamr16k: s3 captions
  were identical. Not run on p1: on this host's CPU it took 283 s for 4 s of audio (about 70x
  slower than real time, under load), so p1's 43 separated pieces were out of reach. A GPU run
  is the next step if precision has to go higher than the gate gives.

