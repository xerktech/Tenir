# Cues (XERK-81)

A **cue** is a private context card the api derives from the live conversation.
Someone asks how far away the sun is and the answer appears; someone mentions
their favourite Pokémon is #133 and a note about Nidoran♀ appears. Cues are
*private to the listener* — they are not part of the conversation, and never sent
to anyone else in the session.

Where they appear:

- **Live** (web + mobile): a bordered card band above the live transcript,
  auto-dismissed after 10s.
- **Glasses**: a bordered box above the on-lens caption band, auto-dismissed
  after 10s.
- **History** (web + mobile): an inline, clickable box at the point in the
  transcript where the cue appeared; tapping it opens a popup with the detail
  (not a new page).

Each cue has a **title** (1–3 words) and a **body** (the fact/context).

## How it works

1. **Generation is server-side, off the caption path.** On each finalized
   transcript turn, `api/src/api/session.py` considers a cue: cheap gating runs on
   the event loop (skip when cues are off, one is already in flight, or inside the
   level's rate-limit window), and only past that does it spawn a background task
   that calls the cue model off-loop. A slow or failing model never stalls
   captions — a cue is a best-effort aside.
2. **The model call reuses the STT gateway.** The cue backend
   (`api/src/api/cue/openai.py`) POSTs `/chat/completions` to the *same* LiteLLM
   endpoint + key the STT engine uses (`API_LITELLM_ENDPOINT` /
   `API_LITELLM_API_KEY`) — no new URL/key var. The model alias is `API_LLM_MODEL`
   (default `qwen3-llm`). The model returns a small JSON object
   (`{cue, title, body}`); a reasoning model may wrap it, so the first JSON object
   is extracted defensively.
3. **Delivery + persistence.** A cue is delivered as a `cue` WebSocket message
   (see `contract/ws-messages.schema.json`) and persisted to the `cues` table
   (`schema.sql`) at `at_ms` — its position on the transcript timeline — so history
   renders it inline. Cues are deliberately excluded from the transcript
   full-text search corpus (they are private context, not conversation).

## Aggressiveness — the global toggle

Users choose how eagerly cues appear with a global toggle in the client UI
(Conservative / Balanced / Aggressive):

- **web**: on the Live panel.
- **mobile**: on the Live screen.
- **glasses**: on the companion page.

The choice is persisted per client and sent on `session.start` as `cueLevel`. The
server maps the level to a prompt strictness and a minimum gap between cues
(`api/src/api/cue/levels.py`): conservative spaces cues out and only fires on
unambiguous facts; aggressive lets them come thick and fast.

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
`qwen3-llm` on the shared LiteLLM gateway. It runs as the `tenir-vllm` container
in the `tenir-gpu` compose stack (docker-ops repo), co-tenant with the Voxtral
STT server on the GPU box.

Why this model:

- **World knowledge.** Cues are factual lookups (distances, entities, trivia), so
  breadth of knowledge matters most. The Qwen3 family leads open-weight models on
  knowledge/instruction-following benchmarks (MMLU-Pro, IFEval) at this size.
- **Speed as a GPU co-tenant.** FP8 weights keep latency low while sharing the
  card with Voxtral; cue generation is a short, bursty chat call, not a sustained
  load.
- **Reliable structured output.** vLLM's guided decoding + a JSON-only prompt give
  dependable `{cue, title, body}` objects, which the parser still guards
  defensively for reasoning-model wrapping.

### Running it on the single-host stack

The base `docker-compose.yml` keeps cues `off`. To see them:

```bash
# Model-free demo cues (no extra container):
API_CUE_BACKEND=stub docker compose up --build

# Real model (large; needs its own GPU share alongside Voxtral):
API_CUE_BACKEND=openai docker compose --profile cues up --build
```

The `cues` profile starts the `vllm-cue` container and the gateway routes the
`qwen3-llm` alias to it (`litellm/config.yaml`). Without the profile the route
just 503s and cues stay silent — captions are unaffected.
