# Cue attempt gating vs cost (XERK-2411, 2026-10-29)

Question: you pay per cue attempt and about 94% of attempts decline. How much does attempting
less often save, and what does it cost in cues and time to cue?

## Method

- Model: Claude Sonnet 5.5 (`anthropic/claude-sonnet-5-5`) via LiteLLM, with the cached system
  block from the Claude cue change.
- Set: the 4 value-bar segments (2.05 conversation-hours, 1,192 finals), the same set as the
  Haiku-vs-Sonnet round in RESULTS-2026-10.md.
- Replay: `replay.py --grounded --realtime`, one shared warm evidence cache, no fact check.
- Gates (`replay.py --attempt-interval-ms` / `--gate`):
  - every final: one attempt in flight, as before this change (the anchor).
  - sentence end: only a final whose text ends a sentence (`.?!…` and CJK/Arabic/Devanagari
    stops, after closing quotes). Other finals still join the 8-turn window.
  - 10 s / 20 s floor: at least that long between attempt starts.
- Cost: LiteLLM's own per-call price (`x-litellm-response-cost`, the figure `/spend/logs`
  stores), summed per run and divided by conversation-hours.
- Time to cue: from the end of the oldest final no earlier attempt had seen to when the cue is
  shown (gate wait + retrieval + call).
- Grading: `blind_judge.py`, Claude subagent reviewers, GOOD/MARGINAL/BAD value grade.
  Differences of ±3 GOOD cues are batch noise; only compare within a batch.

## Results

Batch 3 graded the anchor, sentence end and the plain 10 s floor together (164 cues, 4 packs).
Batch 4 graded the anchor against the shipped floor, which also attempts when the floor lapses
(107 cues, 3 packs). Compare GOOD counts within a batch only.

| gate | attempts | $/hour | cues | GOOD (anchor, same batch) | MARGINAL | BAD | wrong | time to cue mean / p90 |
|---|---|---|---|---|---|---|---|---|
| every final (anchor) | 1,120 | $2.59 | 61 | — | — | — | — | 7.7 / 14.2 s |
| sentence end | 796 | $1.73 | 53 | 40 (42) | 9 | 4 | 0 | 13.6 / 27.5 s |
| 10 s floor, next final only | 500 | $1.09 | 50 | 40 (42) | 7 | 3 | 0 | 14.0 / 18.9 s |
| **10 s floor + lapse attempt (shipped)** | 711 | **$1.49** | 46 | **35 (42)** | 8 | 3 | 2 | 12.6 / 16.4 s |
| 20 s floor, next final only (batch 1) | 308 | $0.63 | 29 | 23 (42) | 6 | 0 | 0 | 23.5 / 28.2 s |

- Cost falls in proportion to attempts: about $0.0042-0.0047 per cached call in every run.
- The anchor's $2.59/h matches the ~$2.3/h estimated from the uncached run.
- Every cheaper gate gives up some GOOD cues: 2 for sentence end and the plain floor (noise),
  7 for the shipped floor (past noise; one replay run, so part of it is run-to-run variance).
  Expect roughly 5-17% fewer good cues for 42% less cost.
- The plain floor (attempt only on the next final past it) is cheaper but strands a turn
  followed by silence. Driving the real session with production timings (28,697 finals,
  170 conversations, 4.7 s calls), 106 finals never reached the model before the
  conversation ended, and some waited up to 216 s. With the lapse attempt none are missed,
  no turn waits past about 10 s (or one call, if longer), and attempts stay at least 10 s
  apart. That costs 211 more attempts on the eval set.
- Sentence end's tail is long: a run of unpunctuated finals holds the window until a sentence
  ends, and a conversation that ends mid-sentence never gets that tail attempted.
- The shipped run's 2 wrong cues are stale or mistaken facts (a wrestling period length, a
  product flavour), not gating effects. BAD cues elsewhere are mostly sermon reactions.
- A 20 s floor halves the GOOD cues. Too sparse.

Earlier batches:
- Batch 1 (4 runs): anchor 42, sentence end 42, plain floor 38, 20 s floor 23 GOOD.
  - Its sentence-end run came from a `replay.py` whose catch-up window missed mid-sentence
    finals that the session includes (fixed; the table uses the corrected rerun).
- Batch 2 (anchor + corrected sentence end): 47 vs 41 GOOD.

## Decision

Shipped: a 10 s floor between attempt starts, plus one attempt when it lapses with turns no
call has seen (`api.cue.tuning.ATTEMPT_INTERVAL_MS`, `Session._consider_cue` /
`_cue_floor_lapsed`). It needs no STT punctuation, so it behaves the same in every language,
and it bounds every turn's wait at about the floor plus one call. `replay.py` models it by
default (`--attempt-interval-ms 0` for the old gating).

The owner first chose sentence end on batch 1. Once the corrected numbers came in, the
question was re-asked with the floor recommended. It timed out, so the floor shipped. The
lapse attempt came later, from QA, and costs GOOD cues as well as money. That trade is in
the PR for the reviewer to accept or reject.

## Not measured

- A second replay of each gate. Grading reruns move GOOD counts by ±3, so one run per gate
  is enough only for differences above that.
- Floors between 10 s and 20 s, or 10 s combined with sentence end.
- A pause gate: a final that closed on silence rather than at the 8 s cap. The session isn't
  told why the STT closed a turn.
- The fact check (off in every run, as in the Haiku/Sonnet round).
