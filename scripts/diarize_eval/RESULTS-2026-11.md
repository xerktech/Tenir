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
- Non-English (FLEURS test es_419 / pt_br laid out as LibriSpeech, built with p1's flags; FLEURS
  has no speaker ids, so each "speaker" is a bucket of same-gender clips and only precision and
  recall mean much here):

  | session | policy | precision | recall |
  |---|---|---|---|
  | Spanish | keep every separated piece | 0.721 | 0.622 |
  | Spanish | no separation | 0.810 | 0.594 |
  | Spanish | **gate −0.5** | **0.809** | **0.586** |
  | Portuguese | keep every separated piece | 0.637 | 0.550 |
  | Portuguese | no separation | 0.733 | 0.533 |
  | Portuguese | **gate −0.5** | **0.748** | **0.480** |

  - Precision rises in both, as in English. But Whisper is less sure of real non-English
    separated speech: pieces between −0.8 and −0.5 had 60 % (es) and 43 % (pt) of their words
    right, against 39 % for every English piece under −0.5. So the gate costs more recall
    there (pt falls below no separation).
  - A per-language threshold is not worth it on this: the sessions are synthetic and about 30
    separated pieces each. Retune on real Spanish/Portuguese overlap before splitting it.
- MossFormer2 (ClearerVoice `MossFormer2_SS_16K`) in place of sepformer-whamr16k: s3 captions
  were identical. Not run on p1: on this host's CPU it took 283 s for 4 s of audio (about 70x
  slower than real time, under load), so p1's 43 separated pieces were out of reach. A GPU run
  is the next step if precision has to go higher than the gate gives.


## Per-language gate on real Spanish overlap (XERK-3069)

Question: the −0.5 gate cost Spanish/Portuguese recall on FLEURS; should the threshold depend
on the decode's detected language? Measured on the Nov 14 dinner (3 recordings, 22.7 min,
Spanish + English, 6+ people). There is no real Portuguese: its `pt` turns are misdetected
Spanish, so Portuguese stays measured on FLEURS only. Local CPU diarizer (same code and models
as the deployed one), real Whisper large-v3-turbo.

**Decision: keep one threshold (−0.5).** On real voices the shipped gate scores best of every
policy measured; any lower Spanish threshold lowers precision and recall both.

Real overlap (`diarize_pieces.py` on the recordings; no word truth):

- 993 pieces, 350 separated decodes. The gate drops 262 (75 %): 109 of 148 tagged `es`,
  137 of 184 tagged `en`. The room is hard even unseparated: 60 of 92 one-speaker stretches decode
  under −0.5.
- Detected language on a separated stream is unreliable: 82 Spanish raw spans gave a stream
  tagged another language (71 `en`, 4 `pt`, 2 `id`, one each `pl`/`tr`/`ru`/`fr`/`it`); the
  raw-span decode shares 0–12 % of their words.
- Words of a separated decode also heard in its turn's raw decode (`replay` `sep_heard_in_raw`; a lower bound:
  the raw decode misses the quieter voice):

  | separated tag / raw span | −0.3…−0.5 | −0.5…−0.8 | < −0.8 |
  |---|---|---|---|
  | `es` / `es` | 65 % (23) | 46 % (44) | 33 % (40) |
  | `en` / `en` | 75 % (16) | 38 % (22) | 20 % (33) |
  | `en` / `es` | 5 % (3) | 9 % (13) | 5 % (53) |

- Read by hand, 30 random Spanish-span pieces between −0.8 and −0.5: of the 21 tagged `es`,
  about 16 are right or mostly right; of the 9 tagged otherwise, 7 are invented.

Real-voice mixtures (`diarize_gate.py mixes` → `diarize_sepmix.py`): the recordings'
one-speaker turns at avg_logprob ≥ −0.6, chained per speaker to ~4 s and summed in pairs
(±4 dB, ≤ 0.5 s offset), 6 seeds, 54 mixtures, 80 separated decodes with text. The reference is
each turn's own decode, not a hand transcript. Mostly Spanish over Spanish (10 % es+en).
`diarize_pieces.py` on these sessions separated only 8 pieces (Sortformer seldom hears two
new voices in a minute), so SepFormer runs directly on each mixture, as the server calls it.
Replay models the shipped fallback: a turn none of whose pieces emits is captioned whole,
unlabelled (`_finalize_pieces` returns False); here the whole turn is the mixture.

| policy (`replay --lang-gate es=T`) | precision | recall | whole-turn captions |
|---|---|---|---|
| no separation (mixed decode) | 0.593 | 0.246 | 0 |
| keep every separated piece | 0.289 | 0.192 | 2 |
| gate −0.4 | 0.591 | 0.251 | 45 |
| **gate −0.5 — shipped** | **0.598** | **0.256** | 44 |
| gate −0.5, `es` at −0.6 | 0.583 | 0.244 | 42 |
| gate −0.5, `es` at −0.8 | 0.512 | 0.218 | 31 |
| gate −0.5, `es` at −1.0 | 0.486 | 0.206 | 22 |
| gate −0.5, `es` ungated | 0.447 | 0.205 | 14 |
| gate −0.5, else the piece's mixed span | 0.598 | 0.256 | 2 |

- Separated decodes tagged `es` were 43 % right between −0.8 and −0.5 and 40 % under −0.8;
  tagged `en` 32 % and 3 %; every other tag ≤ 7 % (a word both streams carry counts once).
  FLEURS (60 % es) and English p1 (39 %) agree: at a given avg_logprob, Spanish separated
  speech is right more often than English.
- But under 50 % right is still worse than the whole-turn caption it displaces: in 44 of 54
  mixtures the shipped gate drops both streams and captions the mixture, and every lower
  Spanish threshold swaps some of those for separated lines that are mostly wrong.
- The FLEURS recall loss came from synthetic sessions; on real voices the gate's whole-turn
  fallback already brings recall to the no-separation level. Captioning a gated piece's own
  mixed span instead scores the same.
- SepFormer (whamr16k) does poorly on these voices: keeping every separated piece is worse
  than no separation on both precision and recall (follow-up XERK-3071).
- Limits: references are Whisper's own solo decodes (accuracy unknown, avg_logprob ≥ −0.6);
  ~6 voices from one dinner; mixtures fully overlapped, which is harder than the real
  recordings' mix of edges and long overlaps; attribution is not measured. The whole-turn
  fallback is modelled from the mixture's decode; a real VAD turn may span more audio.
  `diarize_pieces.py` records the whole-turn decode for future real runs; the Nov 14 runs
  above predate it, so their counts carry no caption scoring.

## MossFormer2 in place of SepFormer on real voices (XERK-3071)

**Decision: keep SepFormer (`speechbrain/sepformer-whamr16k`).** MossFormer2 (ClearerVoice
`MossFormer2_SS_16K`) separates real dinner voices far better, but behind the shipped −0.5 gate
it scores no better, and no gate setting beats SepFormer's shipped precision.

Same recipe as XERK-3069, rebuilt from the Nov 14 recordings: `diarize_pieces.py` with the
deployed GPU diarizer and Whisper large-v3-turbo (1010 pieces, 379 separated; XERK-3069 had
993 / 350), `mixes --ref-logprob -0.6`, seeds 1–6: 49 mixtures in 12 non-empty dirs, 98
separated decodes per separator. Both separators ran on CPU over the same mixtures
(`diarize_sepmix.py --separator … --out-name …`), scored with `replay --truth`.

| policy | SepFormer P / R | MossFormer2 P / R |
|---|---|---|
| no separation (mixed decode) | 0.571 / 0.200 | 0.571 / 0.200 |
| keep every separated piece | 0.301 / 0.209 | 0.440 / 0.278 |
| **gate −0.5 — shipped** | **0.606 / 0.205** | 0.566 / 0.215 |
| gate −0.6 | 0.595 / 0.204 | 0.577 / 0.230 |
| gate −0.7 | 0.556 / 0.196 | 0.592 / 0.245 |
| gate −0.8 | 0.471 / 0.210 | 0.575 / 0.257 |

- Kept whole, MossFormer2's streams are much better than SepFormer's (+0.14 precision,
  +0.07 recall) and beat no separation on recall. More of its streams are tagged Spanish (49
  vs 33), and its Spanish decodes at ≥ −0.5 had 72 % of words right (SepFormer 59 %).
- Behind the shipped gate it lets more pieces through: +0.01 recall for −0.04 precision.
- MossFormer2 at −0.7 is the best trade (0.592 / 0.245), but a paired bootstrap over the 12
  dirs gives it vs SepFormer-shipped: precision −0.078…+0.052, recall −0.001…+0.075 (95 %).
  That's no clear win on 49 mixtures.
- Not measured: LibriSpeech p1 with MossFormer2, and its GPU latency in the diarizer. A
  swap would also need a ClearerVoice loader in `diarizer/server.py` (it loads speechbrain
  only), `clearvoice` in the image, and the gate retuned for it on p1 + s1/s3.
- Speed: on CPU, MossFormer2 was ~8–11x slower than real time, and SepFormer ~4x faster
  than MossFormer2.
- Limits are XERK-3069's: Whisper's own solo decodes as reference, one dinner, fully
  overlapped mixtures.
