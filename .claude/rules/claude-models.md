---
paths:
  - "api/src/api/llm.py"
  - "api/src/api/cue/openai.py"
  - "api/src/api/cue/factcheck.py"
  - "api/src/api/translate/openai.py"
  - "api/tests/test_llm.py"
  - "scripts/cue_eval/**"
---

# Claude models through LiteLLM

- Every chat payload goes through `api.llm.adapt` before it is sent. A new LLM call site that
  skips it fails with a 400 on Claude.
  - Claude rejects `temperature: 0.0` (only the default is allowed).
  - `chat_template_kwargs` is Qwen's thinking switch and is meaningless to Claude.
- Claude is detected by "claude" in the LiteLLM alias. An alias without it gets the Qwen shape.
- Keep per-call text (evidence, the already-surfaced cues) out of the cached first system
  block. Anything that varies there turns every call into a cache write at 1.25x the input price.
  - The fixed cue prompt is about 2,800 tokens, cached from the second call on (Sonnet 5.5's
    minimum is 512). That cuts each call's cost by about 55%. Tests: `test_llm.py`.
- Cue calls leave Claude's thinking at the model default.
  - Sonnet 5.5 at its default gave the best cues. It averaged ~56 output tokens per call, since
    most windows decline at once, but a window that yields a cue can reason 200-500 tokens.
  - `reasoning_effort=low` made it decline nearly every window.
  - Haiku 5.5 thinks by default (about 160 tokens per call).
  - Replay data: `scripts/cue_eval/RESULTS-2026-10.md`.
- `replay.py` (cue calls and `--verify`) goes through `adapt`, so Claude needs no `--extra`.
  `cue_replay_prompt.py` is Qwen-only: it re-adds `chat_template_kwargs` and string-splits the
  system prompt.
- Thinking off (Qwen `enable_thinking: false`) maps to Claude's own switch, per model:
  - Sonnet 5.5: `between_tools`. It ignores `disabled` through LiteLLM.
  - Haiku 5.5: `disabled`. It 400s on `between_tools`.
  - Without it, Haiku spends the fact check's 150-token budget reasoning and returns nothing.
  - Opus 5.5 can't turn thinking off at all. Measure before pointing the api at it.
- Sonnet may answer, then write "Wait — corrected:" and a second JSON object. The fact check
  reads the last object (`factcheck._json`).
- Cost: input tokens are over 90% of a call. You pay per attempt, and about 94% of attempts
  decline, so cost per cue is about 17x the per-call price. LiteLLM spend logs
  (`/spend/logs`, admin key `LITELLM_MASTER_KEY` in bws) carry LiteLLM's computed cost per call.
- Cue attempts start at least 10 s apart for that reason (`ATTEMPT_INTERVAL_MS`, XERK-2411).
  Cost scales with attempts, so lowering it raises cost in proportion.
