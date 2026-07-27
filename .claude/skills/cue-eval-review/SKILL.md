---
name: cue-eval-review
description: Review a recorded Tenir deployment session's cue quality and evaluate cue-pipeline changes with the replay/judge harness. Use when the user asks to review a recording's cues, investigate cue failures, or verify a prompt/threshold change against real conversation data.
---

# Reviewing and evaluating cue quality

This is the methodology behind the 2026-07 cue improvement cycles (PRs #80,
#81, #86; full narrative in `scripts/cue_eval/RESULTS-2026-07.md`). It has two
halves that feed each other: a **hand review** of a real recorded session
(finds the failure classes) and a **replay eval** of candidate fixes against
frozen data (proves a change helps before it ships). The hand review is the
primary quality signal; judged numbers are secondary and comparative.

## 1. Export the data

Postgres runs in the `Tenir-Postgres` container; the host has no local `psql`.

```bash
# Transcript segments (replay input format)
docker exec Tenir-Postgres psql -U tenir -d tenir -tAc \
  "select json_agg(row_to_json(t)) from (select segment_id, conversation_id, \
   text, start_ms, end_ms, lang, speaker_id, speaker_label from segments \
   order by conversation_id, start_ms) t" > segments.json

# Cues the session actually surfaced
docker exec Tenir-Postgres psql -U tenir -d tenir -tAc \
  "select json_agg(row_to_json(t)) from (select cue_id, conversation_id, \
   title, body, at_ms, source from cues order by conversation_id, at_ms) t" \
  > cues.json
```

Work in the session scratchpad, not the repo. Deployment transcripts are the
user's private family conversations: quote them in analysis for the user, but
never paste transcript content into shipped code, prompts, tests, or docs.

## 2. Hand-review the recorded session

Interleave the production cues into the transcript by `at_ms` and read the
whole session in order. For every cue ask: was it correct, was it new
information, was it about the topic *at that moment*, and had it (or its
substance) been shown before? Bucket every bad cue into a named failure class
— the fix targets classes, not individual cues. Classes seen so far:

- **Cross-generation confabulation** — specs/facts from a model the LLM knows
  pasted under a newer sibling's name (e.g. Fold 7 facts as "Fold 8").
- **False corrections** — cue "corrects" a speaker who was right (regional
  titles, renames, post-cutoff releases, date arithmetic the model can't do).
- **Paraphrase duplicates** — same substance re-surfaced under a new title.
  Measure `cue_substance_similarity` for each suspected pair; calibrated
  bands: near-verbatim rewords 0.57–0.87, retitled paraphrases 0.30–0.38,
  closest genuinely-distinct pairs 0.26–0.27 (backstop threshold 0.35).
- **Stale-topic cues** — cue about a topic the conversation left several
  turns ago (8-turn window + fast topic switches).
- **Mishears resolved wrongly** — garbled STT or a bilingual speaker's other
  language treated as a real entity in a third language.

Count each class. If asked only to review, deliver the classes with examples
and counts and stop — don't fix until asked.

Beware when grepping for failure markers: the model emits non-breaking and
narrow spaces inside names (`Fold 8`). Normalize with
`unicodedata.normalize("NFKC", text)` before regex probes, and treat probes as
a floor — the hand read is authoritative.

## 3. Replay-evaluate a candidate fix

Harness: `scripts/cue_eval/` (README there has full usage). It replays
exported conversations through the **installed** `api` package's
`OpenAICueGenerator._build_payload` with the real session gating (8-turn
window, one attempt in flight ≈ 2.5 s, 1.5 s min interval, both dedup
backstops), then LLM-judges every cue.

```bash
cd api && pip install -e '.[dev]'    # replay imports the installed package
python scripts/cue_eval/replay.py segments.json \
  --endpoint http://10.10.10.22:9402/v1 --model <model> \
  --reasoning-effort medium --out results.json
python scripts/cue_eval/judge.py results.json segments.json \
  --endpoint http://10.10.10.22:9402/v1 --model <model> \
  --reasoning-effort low --max-tokens 500
python scripts/cue_eval/report.py results.judged.json
```

Rules that keep the comparison honest:

- **Freeze the eval set.** Fix the list of conversation IDs before iterating
  and pass it via `--conversations` for subset runs. Keep at least one
  control conversation the change should NOT affect (e.g. the
  ambient-movie-audio session) and one out-of-domain one.
- **Re-baseline on the current export.** Row counts drift between exports;
  never compare a new run against numbers from an old export. Re-run the
  shipped prompt on today's data first, then compare candidates to that.
- **Iterate subset → full.** Try a prompt variant on the 3–4 conversations
  exhibiting the target failure class; only when it moves those, run the full
  frozen sweep. Hand-read the emitted cues of every iteration — the scorecard
  can't see a new failure class it has no axis for (v10 fixed duplicates but
  introduced date-arithmetic false corrections; only reading caught it).
- **Match production decoding.** Same temperature (0), same reasoning effort
  (medium — LiteLLM injects it in production), same endpoint family.
- **Expect stochastic effects.** Even at temp 0 the avoid-list makes runs
  path-dependent: cue counts vary ±6 run-to-run, and prompt fixes for
  model-belief failures (confabulation) typically *halve* the class per run
  rather than eliminate it. Run twice before claiming a fix or a regression,
  and report "roughly halved", not "fixed", when that's what the data shows.
- The judge shares the generator's weights — use its numbers **only to
  compare runs on identical data**, never as ground-truth accuracy.
  Spot-check every cue it flags wrong or duplicate.

Editing prompt source mid-run does not affect a replay already running
(Python imports are resolved at process start), but re-`pip install -e` isn't
needed for source edits either — the editable install sees them on the *next*
process. GPU concurrency is capped (`OLLAMA_NUM_PARALLEL=4`); don't raise
harness parallelism past it.

## 4. Calibrate thresholds on real data, not intuition

For any numeric gate (e.g. the substance-dedup threshold), simulate the gate
offline over the real production cues at several candidate values, list which
pairs each value would drop, and hand-classify those pairs as
duplicate/distinct. Pick the value that separates the classes with margin,
and record the measured bands in a comment next to the constant (see
`_CUE_SUBSTANCE_DUP_THRESHOLD` in `api/src/api/session.py`). Keep
`scripts/cue_eval/replay.py`'s copies of session constants in sync.

## 5. Ship and document

- Fixes follow repo convention: branch, tests with the change (pin new prompt
  rules with substring assertions in `api/tests/test_cue_backends.py`; cover
  behavior changes with real measured examples in tests), full `pytest` in
  `api/` green at ≥85% coverage, PR, watch CI.
- Append a dated section to `scripts/cue_eval/RESULTS-2026-07.md` (or its
  successor): failure classes found, what was changed, the iteration
  narrative including failed attempts, a before/after scorecard table, and —
  honestly — the residuals that survived and the planned next step for them.
