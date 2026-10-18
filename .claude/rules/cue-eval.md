---
paths:
  - "scripts/cue_eval/**"
  - "api/src/api/cue/**"
---

# Cue replay evals

## Standing it up

- Turma hosts reach the cluster services directly, no port-forward needed:
  `litellm.ai.svc.cluster.local:4000/v1`, `kiwix.cloud.svc.cluster.local:8080`,
  `searxng.ai.svc.cluster.local:8080`. Point `--kiwix`/`--searxng` there to match prod grounding.
- Exporting transcripts with `kubectl exec` into `tenir-postgres-1` may or may not pass the
  auto-mode classifier; when it is refused, the owner runs the export with `!`.
- `pip install -e api` fails on the host's system Python (PEP 668); use a venv in the scratchpad.

## Thinking cap

- The 512-token cap is the LiteLLM route's `extra_body.thinking_token_budget`. A request-level
  `thinking_token_budget` overrides it (verified: 64 → 78 reasoning tokens, 2000 → 2014), so a
  cap sweep is `replay.py --extra '{"thinking_token_budget": N}'` against the alias. No
  redeploy is needed. Tenir itself sends none, so production gets the route's value.

## Frozen set and anchor

- Frozen 6-conversation set (id prefixes): `0aff8ae8`, `2837af01`, `bd758f83`, plus the
  `87e2acbe` (movie), `1cf67366` (TV) and `ed1d330d` (garbled) controls. About 800 attempts
  per realtime run, ~1 h at cap 512.
- No earlier blind-judged run survives a session. Use the production cues of those
  conversations as the anchor: they stay in the `cues` table, so every future batch can rebuild
  the same anchor. Set `seg_index` to the last segment whose `end_ms` ≤ the cue's `at_ms`.
- Warm `--evidence-cache` over every 8-turn window before the first run, then refetch the
  empty ones until their count stops falling. On the 2026-10 export that plateaus at 145/917.

## Self-check (`--verify`)

- To measure a verifier, run it after the fact over one replay's cues (same window, cached
  evidence). Do not compare two separate replays: dropping a cue changes the avoid-list, so
  the two runs emit different cues. Then count how many of its drops the blind judges marked
  wrong.
- At cap 512 on INT4 the shipped `VERIFY_SYSTEM` mostly drops good cues: 2 of its 23 drops
  were wrong, at a cost of 16 perfect cues (RESULTS-2026-10.md). Don't ship it as-is.

## Grading prompt changes (Oct 2026)

- Grade blind and cross-family: pool every run's cues (the shipped prompt included as the anchor)
  shuffled and unlabelled into the transcript, and have Claude graders mark each GOOD, MARGINAL
  or BAD with a class. Tell graders that near-duplicates come from different systems.
- Re-grade the anchor in every batch: its good count moved 22-27 between batches, so a
  difference of ±3 good cues is noise.
- A prompt run on the 4-segment Oct set (~1,140 attempts) takes ~2 h at the cue server's
  concurrency of 1. A `replay.py` run prints nothing until it ends (no `-u`, no progress line).
- Count good AND bad cues. A value bar that halves the bad cues also loses good ones, and a
  blanket "no advice" rule suppresses answers to questions the speakers actually asked.

## Kiwix (XERK-2066)

- kiwix-serve runs searches serially and never cancels an abandoned one, so Tenir allows one
  `/search` in flight per event loop (`_KIWIX_SEARCHING` in `cue/retrieval/live.py`).
- Keep that slot per loop, not one process-wide `asyncio.Lock`: replay runs a loop per worker
  thread, and a late search parked in a finished thread's loop held a global lock forever.
- A cold search (fresh pod, NFS-backed ZIMs) takes seconds to tens of seconds; time any Kiwix
  measurement twice, so the second run shows warm-cache figures.
- `/search` ignores `books.filter.flavour`, and both Wikipedia ZIMs are named `wikipedia_en_all`.
