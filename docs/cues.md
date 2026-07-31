# Cues (XERK-81)

A **cue** is a private note the api derives from the live conversation. Its job
is to **enrich** the conversation with accurate information it did not already
contain: someone asks how far away the sun is and the verified distance
appears; someone names a technique and a one-line definition appears; someone
says the Great Wall is visible from space and a correction appears. A cue that
merely repeats what a speaker said is worthless and the prompt bans it
outright — the added fact, number, definition, comparison, or correction is the
whole product. Accuracy stays absolute underneath (XERK-118): a wrong cue is
worse than no cue. Cues are *private to the listener* — they are not part of
the conversation, and never sent to anyone else in the session.

Where they appear:

- **Live** (web + mobile + glasses phone Session page): a bordered card above
  the live transcript, auto-dismissed after 10s.
- **Glasses lens**: a bordered box above the on-lens caption band — the same
  full-width popup strip the double-tap menu uses (XERK-85), showing the cue's
  title over its detail, auto-dismissed after 10s. The interactive menu takes
  precedence: a cue arriving while the menu is open is queued behind it
  (XERK-102). The title stays pinned; a body longer than the box's four visible
  rows (XERK-112, XERK-133) lives in its own scrolling container inside the box,
  which the host scrolls with its native scroll bar as the wearer swipes.
  Tapping or swiping a live cue resets its auto-dismiss countdown, so an engaged
  reader is never cut off mid-read.

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
separate cue type; it rides the same title/body shape as any other cue. A cue
whose fact was grounded in a live source (XERK-120, below) also carries a
**source** — a short label like "BBC News" or "Wikipedia" rendered as a quiet
attribution line under the body on web, Android, and the glasses phone
Session/History pages. The on-lens box deliberately omits it — a documented
platform exception: the lens is a tiny monochrome strip where every row costs
caption space, and the full cue with its attribution is always a glance away
on the phone.

## How it works

1. **Generation is server-side, off the caption path.** On each finalized
   transcript turn, `api/src/api/session.py` considers a cue: cheap gating runs on
   the event loop (skip when cues are off, one is already in flight, inside the
   fixed rate-limit window, or while a live translation run is open — XERK-160:
   cues neither trigger nor appear during translations, and resume once the run
   ends; see `docs/translations.md`), and only past that does it spawn a
   background task that calls the cue model off-loop. A slow or failing model never stalls
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

## Enrichment over echo, accuracy over volume

A cue earns its place by **adding** something true. The prompt
(`api/src/api/cue/openai.py`) frames the model as a research assistant with
five triggers — answer a question asked aloud; add a concrete fact about a
named person/place/product; define a term or piece of jargon; contribute a
relevant number, precedent, or trade-off to a decision being worked through;
correct a falsehood — and bans restating the transcript. In substantive talk
the posture is emissive (silence is the exception, and if the best candidate
fact is unsafe or already surfaced the model takes the next-best instead of
declining); accuracy rules stay absolute over the content: no speculation, no
facts about garbled/misheard names, never contradict what a speaker stated
firsthand, never "correct" the speakers from a stale training cutoff on things
that change, and stay silent rather than guess. Decoding is greedy
(temperature 0) — measured on replayed deployment sessions it produced the
same volume with materially fewer wrong cues than sampled decoding. The
calibration history and replay measurements live in `docs/cue-rag.md`; the
harness is `scripts/cue_eval/`.

Repeats are blocked in three layers: the prompt carries the recent surfaced
cues (title *and* body) as a do-not-repeat list, the session drops exact title
matches (normalized), and a content-word fingerprint drops the same fact
returning under a fresh title in new words (Jaccard ≥ 0.5 on content tokens —
the recorded failure was one factory-production fact surfacing as three
differently-titled cues).

There is still no per-client aggressiveness toggle. The fixed settings live in
`api/src/api/cue/tuning.py`:

- **`min_interval_ms()`** — the minimum gap the session waits between cues
  (`1500ms`). A floor that stops a burst of hits from stacking, not a target; the
  accuracy bar below is what actually governs how often cues appear.
- **`cue_guidance()`** — the source-of-truth bar slotted into the prompt's
  accuracy rules (the enrichment frame, triggers, and worked examples are
  shared and live in `openai.py`'s `_SYSTEM`). Since XERK-120 the bar is
  **asymmetric**, picked per call by whether live evidence actually made it
  into the prompt:
  - *No evidence* (retrieval off, timed out, or empty): facts that change or
    grow over time (releases, versions, prices, scores, officeholders,
    franchise counts) are off-limits from memory — silence over a stale answer.
  - *Evidence present* (`cue_guidance(grounded=True)`): generous for
    evidence-covered facts — the fact's accuracy comes from the retrieved source
    (and carries its label), not model confidence, so holding it to the
    guessing-era bar just discarded correct, attributable cues. Everything the
    evidence does **not** cover keeps the tight bar: time-sensitive facts may
    come only from evidence, stable facts from memory only when certain.

  The asymmetry is what keeps volume from costing accuracy — measured on the
  deployed model, a *symmetric* loosening answered an uncovered question by
  citing unrelated evidence, while the gated bar emitted on covered facts and
  stayed silent on every uncovered trap (the experiment is in
  `docs/cue-rag.md`). Because the generous bar rides only alongside real
  evidence, a retrieval outage degrades to the tight bar, never to aggressive
  guessing.

The old three-level toggle (Conservative / Balanced / Aggressive) that web + mobile
used to expose and persist per client (XERK-81) is gone; the per-client `cueLevel`
on `session.start` is no longer sent by any client. The glasses client never had
the toggle, so it simply matches the single fixed setting everyone else gets.

## Evidence retrieval (XERK-120)

The cue model's weights are frozen years back (the deployed Qwen3 reports an
October 2023 cutoff), so a cue about anything current answered from memory is a
guess — probed directly, the model confidently names a UK prime minister two
governments stale. With `API_CUE_RETRIEVAL_BACKEND=live`, the session gathers
**evidence** before the model call and rides it in the prompt: three tiers — an
RSS-fed news corpus in Postgres (FTS, <10ms), Wikipedia (~300-400ms), and a
self-hosted SearXNG (~500-800ms with pinned engines) — fan out concurrently
under a hard deadline (`API_CUE_RETRIEVAL_DEADLINE_MS`, 800ms). Whatever lands
in time is numbered into the system prompt with source + date; the prompt tells
the model that for recent events the evidence outranks its memory, and to cite
the items it used. The first citation becomes the cue's `source` label; tiers
that miss the deadline settle into a per-session cache for the next cue instead
of delaying this one. Retrieval failure of any kind degrades to an ungrounded
cue — evidence is an upgrade, never a gate, and captions are never touched.

The full research — latency measurements on the production model, the
vector-DB-vs-FTS decision, SearXNG engine health — lives in
`docs/cue-rag.md`. The RSS ingest loop (`api/src/api/cue/rss.py`) runs in the
api lifespan only when retrieval is `live`, fetching `API_CUE_RSS_FEEDS` every
10 minutes and pruning items older than 14 days, so every corpus hit is recent
by construction.

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
