# Cues (XERK-81)

A **cue** is a private, fact-checked note the api derives from the live
conversation. Its one job is accuracy (XERK-118): correct something said in the
conversation that is wrong, or add a fact the model is confident is true — someone
says the Great Wall is visible from space and a correction appears; someone asks
how far away the sun is and the verified distance appears. Cues are *private to the
listener* — they are not part of the conversation, and never sent to anyone else in
the session.

Where they appear:

- **Live** (web + mobile + glasses phone Session page): a bordered card above
  the live transcript, auto-dismissed after 10s.
- **Glasses lens**: a bordered box above the on-lens caption band — the same
  full-width popup strip the double-tap menu uses (XERK-85), showing the cue's
  title over its detail, auto-dismissed after 10s. The interactive menu takes
  precedence: a cue arriving while the menu is open is dropped.

Every live surface counts that dismissal down (XERK-110): across from the title,
in the cue's top-right corner, `10s` → `9s` → … → `0s`. The count is derived from
when the cue went up (`cueSecondsLeft` in client-core, shared by all three
frontends) rather than decremented per tick, so a throttled tab or a
backgrounded app resyncs to the truth instead of drifting away from the release
timer. On the lens — where a row is one string and there is no alignment control
— it is spaced out to the right edge of the popup's title row, and the title is
trimmed to whatever width that leaves. History cues carry no countdown: they are
kept permanently, so there is nothing to count down to.
- **History** (web + mobile + glasses phone History page): an inline, clickable
  box at the point in the transcript where the cue appeared; tapping it opens a
  popup with the detail (not a new page).

Each cue has a **title** (1–3 words) and a **body** (the correction or verified
fact). A correction is just a cue whose body states the right fact — there is no
separate cue type; it rides the same title/body shape as any other cue.

## How it works

1. **Generation is server-side, off the caption path.** On each finalized
   transcript turn, `api/src/api/session.py` considers a cue: cheap gating runs on
   the event loop (skip when cues are off, one is already in flight, or inside the
   fixed rate-limit window), and only past that does it spawn a background task
   that calls the cue model off-loop. A slow or failing model never stalls
   captions — a cue is a best-effort aside.
2. **The model call reuses the STT gateway.** The cue backend
   (`api/src/api/cue/openai.py`) POSTs `/chat/completions` to the *same* LiteLLM
   endpoint + key the STT engine uses (`API_LITELLM_ENDPOINT` /
   `API_LITELLM_API_KEY`) — no new URL/key var. The model alias is `API_LLM_MODEL`
   (default `qwen3-llm`). The model returns a small JSON object
   (`{cue, title, body}`). The prod model (Qwen3) is a *reasoning* model, so the
   call disables thinking (`chat_template_kwargs.enable_thinking = false`, toggle
   `API_CUE_DISABLE_THINKING`) — otherwise it spends the whole token budget
   reasoning and returns an empty `content`, and no cue is ever produced. The first
   JSON object is still extracted defensively, falling back to `reasoning_content`.
3. **Delivery + persistence.** A cue is delivered as a `cue` WebSocket message
   (see `contract/ws-messages.schema.json`) and persisted to the `cues` table
   (`schema.sql`) at `at_ms` — its position on the transcript timeline — so history
   renders it inline. Cues are deliberately excluded from the transcript
   full-text search corpus (they are private context, not conversation).

## Accuracy over volume (XERK-118)

The cue's bar is **accuracy, not volume**. A cue only earns its place if it is
correct and worth trusting; padding the transcript with vague or possibly-wrong
"context" is worse than silence. So the model surfaces a cue only when it is
confident, and prefers correcting a clear error over adding tangential trivia —
when unsure, it stays silent. This reversed XERK-114's earlier "when in doubt,
emit" tuning: the info being real is the whole point.

There is still no per-client aggressiveness toggle. The one fixed setting lives in
`api/src/api/cue/tuning.py`:

- **`min_interval_ms()`** — the minimum gap the session waits between cues
  (`1500ms`). A floor that stops a burst of hits from stacking, not a target; the
  accuracy bar below is what actually governs how often cues appear.
- **`cue_guidance()`** — the bar the chat-model prompt sets for emitting a cue:
  correct a clear factual error, or add a concrete fact the model is confident is
  true; never pad, and stay silent when unsure it is accurate.

The old three-level toggle (Conservative / Balanced / Aggressive) that web + mobile
used to expose and persist per client (XERK-81) is gone; the per-client `cueLevel`
on `session.start` is no longer sent by any client. The glasses client never had
the toggle, so it simply matches the single fixed setting everyone else gets.

## Backends (`API_CUE_BACKEND`)

| Value    | Behaviour                                                              |
|----------|-----------------------------------------------------------------------|
| `off`    | No cues (default). The stripped core stays STT-only.                   |
| `stub`   | Model-free, deterministic generator for CI/dev — no GPU.               |
| `openai` | Real chat model via the LiteLLM gateway (`qwen3-llm`).                 |

The stub is what CI exercises end-to-end (session → WS frame → persistence →
history), so the whole path is covered without a GPU.

## The model

The production cue model is **Qwen3.6-27B-FP8**, served by vLLM and aliased
`qwen3-llm` on the shared LiteLLM gateway. It runs as the `tenir-vllm-cue`
container in the `tenir-gpu` compose stack (docker-ops repo), co-tenant with the
Parakeet STT server on the GPU box. (STT moved Voxtral→Parakeet in XERK-92; the
cue model is unaffected — it is a separate chat route through the same gateway.)

Why this model:

- **World knowledge.** Cues are fact checks and factual lookups (correcting wrong
  claims, distances, entities), so breadth *and* reliability of knowledge matter
  most — a cue is only worth surfacing if it is right (XERK-118). The Qwen3 family
  leads open-weight models on knowledge/instruction-following benchmarks (MMLU-Pro,
  IFEval) at this size.
- **Speed as a GPU co-tenant.** FP8 weights keep latency low; the only other model
  on the card is Parakeet STT (~2.4 GB), so the cue LLM gets the lion's share and
  cue generation is a short, bursty chat call, not a sustained load.
- **Reliable structured output.** vLLM's guided decoding + a JSON-only prompt give
  dependable `{cue, title, body}` objects, which the parser still guards
  defensively for reasoning-model wrapping.

### Running it on the single-host stack

The base `docker-compose.yml` keeps cues `off`. To see them:

```bash
# Model-free demo cues (no extra container):
API_CUE_BACKEND=stub docker compose up --build

# Real model (large; needs its own GPU share alongside Parakeet):
API_CUE_BACKEND=openai docker compose --profile cues up --build
```

The `cues` profile starts the `vllm-cue` container and the gateway routes the
`qwen3-llm` alias to it (`litellm/config.yaml`). Without the profile the route
just 503s and cues stay silent — captions are unaffected.
