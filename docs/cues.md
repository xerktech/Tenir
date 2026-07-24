# Cues (XERK-81)

A **cue** is a private context card the api derives from the live conversation.
Someone asks how far away the sun is and the answer appears; someone mentions
their favourite Pokémon is #133 and a note about Nidoran♀ appears. Cues are
*private to the listener* — they are not part of the conversation, and never sent
to anyone else in the session.

Where they appear:

- **Live** (web + mobile + glasses phone Session page): a bordered card above
  the live transcript, auto-dismissed after 10s.
- **Glasses lens**: a bordered box above the on-lens caption band — the same
  full-width popup strip the double-tap menu uses (XERK-85), showing the cue's
  title over its detail, auto-dismissed after 10s. The interactive menu takes
  precedence: a cue arriving while the menu is open is dropped.
- **History** (web + mobile + glasses phone History page): an inline, clickable
  box at the point in the transcript where the cue appeared; tapping it opens a
  popup with the detail (not a new page).

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

## Aggressiveness — the global toggle

Users choose how eagerly cues appear with a global toggle in the client UI
(Conservative / Balanced / Aggressive):

- **web**: on the Live panel.
- **mobile**: on the Live screen.

The choice is persisted per client and sent on `session.start` as `cueLevel`. The
server maps the level to a prompt strictness and a minimum gap between cues
(`api/src/api/cue/levels.py`): conservative spaces cues out and only fires on
unambiguous facts; aggressive lets them come thick and fast.

The **glasses** client (the on-lens UI and its phone Session/History pages) has
no cue-level control surface — the lens is display-only and the phone pages are a
live mirror + a review list — so it omits `cueLevel` on `session.start` and the
server default (`API_CUE_DEFAULT_LEVEL`, balanced) applies. A deliberate platform
difference; the toggle lives on the web + mobile clients.

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

- **World knowledge.** Cues are factual lookups (distances, entities, trivia), so
  breadth of knowledge matters most. The Qwen3 family leads open-weight models on
  knowledge/instruction-following benchmarks (MMLU-Pro, IFEval) at this size.
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
