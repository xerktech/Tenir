# Cue RAG: grounding cues in live sources (XERK-120)

XERK-120 asked for a system that lets the cue model "search the Internet to
gather and confirm information", including — *if actually helpful* — an
RSS-fed corpus in a vector DB, with accuracy and speed as the two hard
requirements. This doc records the research that sized the problem, the
measurements that drove each design decision, and the shape that shipped.

## Why grounding is needed at all

The deployed cue model (Qwen3.6-27B-FP8 on the GPU host) was probed directly:

- Asked what it knows, it reports a **training cutoff of October 2023** —
  about three years stale at the time of writing.
- Asked about a current event with no context, it either abstains or, worse,
  **states a stale fact confidently**: asked who the UK prime minister is, it
  answered "Keir Starmer, took office July 5 2024" — presented exactly like a
  verified fact, which XERK-118's accuracy-first bar exists to prevent. The
  grounded run of the same conversation answered with the office-holder from
  a five-day-old news item, with attribution.

So for anything time-sensitive, an ungrounded cue isn't just weaker — it is a
liability. Retrieval is the fix; the question was only how to do it without
blowing the latency budget.

## The latency budget (measured)

Everything below was measured against the production model server and live
sources from the deployment host:

| Operation | Latency |
|---|---|
| Cue LLM call (~70-token prompt, thinking off) | **~1.0–1.1s** (decode-bound) |
| … + ~500 evidence tokens | +~50ms |
| … + ~1,400 evidence tokens | +~200ms |
| … + ~2,700 evidence tokens | +~300ms |
| … + ~5,400 evidence tokens | +~900ms |
| `max_tokens` 80 vs 300 | no difference |
| Postgres FTS over a pruned news corpus | **<10ms** |
| Wikipedia search+extract (one round trip) | **~300–400ms** |
| SearXNG, engines pinned (`startpage,bing,duckduckgo news`) | **~500–800ms** |
| SearXNG, full engine fan-out | ~1–1.5s |

Three consequences fell straight out of these numbers:

1. **One LLM call, evidence in the prompt.** A second model call (e.g. to
   formulate a search query, or to verify afterwards) would double the fixed
   ~1.1s cost. So the query is built by cheap text heuristics
   (`api/src/api/cue/retrieval/query.py`) and evidence rides the single
   existing call's system prompt.
2. **A ~1,500-token evidence budget.** Prompt tokens are nearly free up to
   roughly that point (~200ms) and get expensive after; the retriever caps
   snippets and count accordingly.
3. **Retrieval must be deadline-boxed, not awaited.** The tiers fan out
   concurrently under `API_CUE_RETRIEVAL_DEADLINE_MS` (default 800ms —
   news + Wikipedia virtually always make it, SearXNG usually does). What
   lands in time rides the prompt; a slow tier contributes nothing rather
   than delaying the cue. Worst-case added cue latency is the deadline, full
   stop. Tiers that miss keep running into a per-session cache keyed by the
   query, so a conversation that stays on topic gets their evidence on the
   next cue consideration.

## The three tiers

- **News corpus** — RSS feeds (`API_CUE_RSS_FEEDS`, default BBC/NPR/Guardian
  world) ingested every 10 minutes into the `news_items` table, pruned after
  14 days, searched with Postgres FTS. Freshest, most curated, effectively
  free at query time.
- **Wikipedia** — one `generator=search` + intro-extract round trip. Stable
  facts: entities, places, figures.
- **SearXNG** — the deployment's self-hosted metasearch instance
  (`API_CUE_SEARXNG_ENDPOINT`), with engines pinned per request. General and
  recent web coverage.

The model cites which evidence items it used (`"evidence": [1]` in its JSON
reply); the backend maps the first citation to the item's source label, which
travels the whole path — WS `cue` frame → `cues.source` column → history API →
an attribution line on web, Android, and the glasses phone Session/History
pages (the on-lens box deliberately omits it — a tiny monochrome strip where
every row costs caption space). An uncited cue (from model knowledge, fine for
stable facts) simply
carries no label. Malformed or out-of-range citations are dropped rather than
guessed: a wrong label is worse than none.

## The vector DB decision: FTS instead, deliberately

The ticket floated "a vector db where rss feeds … can be stored" and asked
for it *only if actually helpful*. The **corpus** part is kept — it is the
single best recency source. The **vector index** part is consciously replaced
with Postgres FTS on the same corpus:

- The corpus is tiny and fresh by construction (a few thousand
  headline+summary rows, 14-day retention). At that size, keyword FTS with
  rank-then-recency ordering already retrieves well.
- The queries are entity keywords pulled from live transcripts (names,
  places, numbers) — the case keyword matching is *best* at. Embedding
  search earns its keep on paraphrase recall over large corpora, which this
  is not.
- A vector index needs an embedding model. That means either a new GPU
  co-tenant (competing with STT + the cue LLM for VRAM) or a heavy CPU
  dependency in the api image, plus embedding latency **on the cue hot
  path** — recall that the whole retrieval budget is 800ms.
- FTS is zero new infrastructure: the repo already uses the exact same
  functional-GIN pattern for transcript search, and Postgres is already in
  the stack.

The retrieval seam (`EvidenceRetriever`) hides the implementation, so if the
corpus ever grows past what FTS serves well, a vector-backed news tier can
slot in behind the same interface without touching the session or generator.

## SearXNG: instance findings and the pinned-engine decision

The shared self-hosted instance (deployed from the DockerOps repo) was
audited rather than modified:

- `format=json` already enabled; limiter off; uwsgi workers sized to cores —
  the deployment itself needed no changes.
- Engine health (from `/stats/errors` and per-engine probes): DuckDuckGo
  general is CAPTCHA-banned (~80%), Brave rate-limited, Google/Qwant return
  nothing — upstream anti-scraping reality, not config, and SearXNG already
  auto-suspends them. Working: Startpage (~360ms), Bing (~810ms), and the
  news engines DuckDuckGo News (~380ms), Startpage News (~470ms), Bing News
  (~510ms), Reuters (~970ms).
- Pinning engines **per request** (`API_CUE_SEARXNG_ENGINES`, default
  `startpage,bing,duckduckgo news`) cuts the query from ~1–1.5s to
  ~500–800ms and leaves the shared instance's configuration untouched for
  its other consumers. That per-request parameter is the entire performance
  lever; no instance changes were needed.

## End-to-end proof

The full path was exercised against the real stack (live SearXNG, live
Wikipedia, a seeded news item, the production Qwen endpoint) with the
conversation "So who is the UK prime minister right now? / I think it's still
Rishi Sunak isn't it?":

- Retrieval: 4 evidence items in ~800ms (news corpus hit, a 5-day-old CNN
  result via SearXNG, Wikipedia's Sunak article, one noise hit the model
  correctly ignored).
- Grounded cue: *"Andy Burnham is the current UK Prime Minister, having
  taken office on 20 July 2026. Rishi Sunak served as Prime Minister from
  2022 to 2024."* — source **BBC News**, correct.
- Ungrounded (control): *"The current Prime Minister … is Keir Starmer, who
  took office on July 5, 2024."* — confidently two years stale.

## Configuration summary

All off by default; the stripped core and existing cue behaviour are
untouched until opted in (see `api/src/api/config.py` for full comments):

| Var | Default | Meaning |
|---|---|---|
| `API_CUE_RETRIEVAL_BACKEND` | `off` | `off` \| `stub` (CI) \| `live` |
| `API_CUE_RETRIEVAL_DEADLINE_MS` | `800` | hard retrieval deadline |
| `API_CUE_RETRIEVAL_MAX_EVIDENCE` | `6` | evidence snippets in the prompt |
| `API_CUE_WIKIPEDIA_ENDPOINT` | `https://en.wikipedia.org` | `""` disables the tier |
| `API_CUE_SEARXNG_ENDPOINT` | `""` (off) | your SearXNG root |
| `API_CUE_SEARXNG_ENGINES` | `startpage,bing,duckduckgo news` | per-request engine pin |
| `API_CUE_RSS_FEEDS` | BBC/NPR/Guardian world | comma-separated URLs |
| `API_CUE_RSS_INTERVAL_SECONDS` | `600` | ingest cadence |
| `API_CUE_RSS_KEEP_DAYS` | `14` | corpus retention |

## Follow-up: the asymmetric emission bar

With grounding shipped, the XERK-118 emission bar was re-examined: it was
calibrated for a model that could only guess, and measured against the live
model it stayed silent even when the evidence fully covered the fact — a
correct, attributable cue left on the table. Three guidance variants were
tested on the deployed model (evidence about the new UK PM in the prompt):

| Case | tight (XERK-118) | loose (naive) | loose, evidence-gated |
|---|---|---|---|
| Chit-chat, nothing factual | silent | silent | silent |
| Fact covered by evidence | silent (missed) | emits, grounded | emits, grounded |
| Current event not covered | silent | silent | silent |
| Stale-memory trap (recent election) | silent | **emits, miscites** | silent |

The naive loosening reintroduced exactly what XERK-118 killed — asked about an
uncovered election, it answered by citing the unrelated PM evidence. The
setting that passed every trap is **one-sided generosity**
(`CUE_GUIDANCE_GROUNDED` in `tuning.py`): emit freely where the evidence
supports the fact; everything else keeps the tight bar (time-sensitive facts
only from evidence, stable facts from memory only when certain). The payload
builder picks the bar by whether evidence actually arrived, so a retrieval
outage degrades to the tight pre-grounding bar rather than to aggressive
guessing.

Frequency mechanics, for completeness: `MIN_INTERVAL_MS` (1500ms) is not the
lever — one cue attempt costs ~2–2.5s (deadline-boxed retrieval + the ~1.1s
model call, one in flight) and the clients show one cue per ~10s band slot, so
the emission bar governs how often cues actually appear.
