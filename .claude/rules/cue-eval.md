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
- An agent session can't export transcripts: the auto-mode classifier blocks `kubectl exec` into
  `tenir-postgres-1` ("Production Reads") even with the owner's approval in chat. The owner runs
  the export from the cue-eval-review skill with `!`, into the session scratchpad.
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
