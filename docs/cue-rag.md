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
- Pinning engines **per request** (`API_CUE_SEARXNG_ENGINES`) cuts the query
  from ~1–1.5s to ~500–800ms and leaves the shared instance's configuration
  untouched for its other consumers. That per-request parameter is the entire
  performance lever.

> **Superseded by XERK-124.** The pin above (`startpage,bing,duckduckgo news`)
> was measured once, at a moment when those engines happened to answer, and did
> not survive contact with production. Re-measured against the same instance:
> Startpage and Google return only CAPTCHAs, DuckDuckGo denies access, and Bing
> was never among the instance's *enabled* engines at all — so the web tier
> returned **zero usable results for every real query**.
>
> Three changes fixed it. The retriever stopped re-querying on every finalized
> turn, which is what drove the engines to suspend the instance in the first
> place. The instance's own config was rebuilt (DockerOps `searxng/settings.yml`)
> — it had been a stale full copy of an older release's settings, which is why
> `google cse`, upstream's default and the best-performing engine available
> here, was missing entirely. And the pin became **plural**.
>
> Redundancy, not engine choice, is what makes this work. Every one of these
> engines rate-limits or CAPTCHA-walls a self-hosted instance eventually, so a
> pin is only as good as its spares — measured over four consecutive queries, a
> two-engine pin answered **1 of 4** (whichever engine gets asked twice
> suspends), while `google cse,duckduckgo,mojeek` answered **4 of 4**, ~1.0s
> median. Engine reachability is a *moving* property: treat a pin as perishable
> and re-probe it rather than trusting a one-off measurement — including this
> one.

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
| `API_CUE_SEARXNG_ENGINES` | `google cse,duckduckgo,mojeek` | per-request engine pin |
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

## XERK-124: why none of the above ever ran in production

Reported as "no cues are appearing". It was not the emission bar, and it was
not design — the grounded path above had **never once executed** in production.
Retrieval returned zero evidence on every single cue call, so
`_build_payload` always took the ungrounded branch and applied the tight bar.

Replaying a recorded session (34 finalized turns) through the deployed stack:

| | before | after |
|---|---|---|
| Turns where retrieval returned any evidence | **0 / 34** | **29 / 34** |
| Wikipedia queries | junk bags → 429s mid-session | on-topic, cached per topic |
| SearXNG queries answered | 0 (every engine suspended) | 4 / 4 on a 3-engine pin |

Three independent causes, all in the retrieval path:

1. **The query builder was tuned on written prose.** On live STT it emitted
   `also 10thinger means anti-glare 60 24-bit`. Speech filler outscored the
   subject; worse, sentence-opening words ("So", "Now", "Right") were scored as
   proper nouns, because the check looked at the single character before the
   token and found the *space* after "back.", never the period. Scoring also
   spanned only 3 turns, so the subject — named once, then discussed for
   minutes — aged out of the query entirely.
2. **The tiers were queried wrong and too often.** Wikipedia's
   `generator=search` ranks against the whole string, so padding it to 8
   keywords returned "Personal computer" where the top 3 returned "Raspberry
   Pi". And both network tiers re-fired on *every* finalized turn (~1/s), which
   is what earned the instance a session-long stream of 429s and left every
   SearXNG engine suspended.
3. **The pinned SearXNG engines did not work** — see the note above.

The fix is retrieval-only; the XERK-118/120 bars are untouched. Verified
end-to-end on a news-covered conversation: 4/4 turns emitted correctly sourced
cues, including a genuine correction of the speaker ("more than 250,000
evacuated, not just a few thousand", source *The Guardian*).

One consequence worth stating plainly: cues stay silent when the evidence does
not cover the specific claim. On a hardware review full of post-training-cutoff
spec numbers, retrieval returns the right *page* ("Raspberry Pi") but nothing
that covers "1200 by 1920, 224 ppi, $80", so the grounded bar correctly says
nothing. That is the bar working as designed — but it means cue volume tracks
how well the corpus covers what people actually talk about, which is now the
open tuning question rather than a bug.

## XERK-124, part two: questions are cue triggers

The retrieval fix shipped and the very next test session still produced zero
cues — for a *different* reason, visible in its transcript: the session was
almost entirely direct factual questions to the glasses ("How many Toy Story
movies are there?", "How many rings does Saturn have?"). Replayed through the
deployed stack, retrieval was healthy (evidence on 24/27 turns, the grounded
bar engaged) and the model answered `{"cue": false}` on every turn — correctly,
because both cue triggers to that point (correct an error, annotate a claim)
presuppose somebody *asserted* something. A question asserts nothing, so
neither fired. Yet asking aloud is the closest thing the product has to an
explicit request for a cue.

The reporter then clarified the intended product outright, and it settled the
emission model as **three triggers, one per thing a conversation can bring**:

* a question comes through → **answer** it
* a fact/topic comes through → **add extra context** to it
* a falsehood comes through → **correct** it

Both bars (`tuning.py`) and the system prompt now carry all three verbs.
XERK-118's accuracy rule stays absolute over the *content* — never state what
you are unsure of; when sure of something modest but not the specifics, say the
modest accurate thing — while the *posture* over the triggers is emissive
again. The reaction-word gap this exposed in the query builder ("Nice." as a
whole turn retrieved *Nice Côte d'Azur Airport*) is fixed with backchannel
stopwords, run-exempt so "Great Wall" survives.

Re-verified against the deployed model:

| Case | Result |
|---|---|
| "How many rings does Saturn have?" (stable fact, memory) | emits, correct |
| "How many Toy Story movies?" | emits, hedged correctly ("four main films, fifth scheduled for 2026") |
| Mention: "climbing Mount Fuji" / "listening to Fleetwood Mac" | emits accurate context |
| Mention: "flying into Reykjavik" (with retrieval) | emits sourced airport context |
| Covered current-events Q ("who is the UK PM?") + evidence | emits, cited, corrects the stale guess |
| False claim ("Great Wall is 500 km") | emits the correction |
| Uncovered current-events Q + *unrelated* evidence | **silent** (the miscite trap) |
| Uncovered current-events Q, no evidence | silent |
| Personal Q / personal mention / small talk | silent |
| Raspberry Pi session | 3 accurate sourced context cues, **0 wrong ones** (the XERK-114 wrongness stays dead) |

## XERK-124, part three: the stale-count incident and the freshness path

The three-trigger model immediately surfaced the next weakness: asked "how many
Toy Story movies are there?", the model answered from memory with its
truth-at-cutoff ("four main films, with a fifth scheduled for 2026") — stale,
because the fifth film had already shipped. Root-caused end to end, this was
three separate misses stacked:

1. **The fresh fact was one rank away.** Live Wikipedia's #2 hit for
   `toy story` is *Toy Story 5* ("a 2026 American animated comedy-drama
   film") — but the query builder had let a stray conversational numeral in
   ("At approximately 5 45" → keyword `45`), and `toy story 45` demotes the
   fifth film clean out of the ranking. Numerals are now excluded from the
   encyclopedia tier's query entirely (they still help the news tier's FTS,
   where "250,000" matches the headline), and the tier takes the top **3**
   hits instead of 2 — the 2-cap had clipped exactly the freshness the tier
   exists to provide, to save ~75 prompt tokens.
2. **The web tier could never land.** The 800ms retrieval deadline was
   calibrated against an engine set that turned out to be dead; the engines
   that actually answer take ~1.0–1.2s, so the freshest general source always
   missed the deadline and only ever settled into the topic cache — useless
   for a question, which gets exactly one cue attempt on the turn it is asked.
   The default deadline is now **2000ms** (suspended engines fail in ~10ms, so
   a down tier still costs nothing).
3. **The bar treated a growing count as a stable fact.** Both bars now name
   facts that *grow over time* — how many entries an ongoing film series,
   product line, or franchise has; its latest release — as unsafe from
   memory: the tight bar stays silent on them, the grounded bar takes them
   only from evidence.

Re-verified against the deployed model after all three fixes:

| Case | Result |
|---|---|
| "How many Toy Story movies?" with retrieval | **"There are five Toy Story feature films … Toy Story 5"**, cited to Wikipedia (the cited extract literally enumerates all five) |
| Same question, evidence withheld | **silent** — was the stale "four" |
| "What is the newest iPhone?", no evidence | silent |
| "How many rings does Saturn have?" / "How many US states?" (genuinely stable) | still answered, correct |
| Miscite / small-talk / personal traps | all still silent |
| Raspberry Pi session canary | accurate cues only, 0 wrong |

## XERK-124, part four: the Kiwix mirror as encyclopedia fallback

The deployment hosts a Kiwix instance with an English Wikipedia ZIM (June 2026
nopic, refreshed by the DockerOps updater). Compared head-to-head from the
Tenir container on the same queries:

| | Kiwix (local ZIM) | live Wikipedia |
|---|---|---|
| Latency | **~30–110ms** | ~220–400ms |
| Rate limits | none | throttles (the 429s that opened XERK-124) |
| Freshness | snapshot, trails by **weeks** (had Toy Story 5; missing the July PM change and July wildfires) | crowd-edited within **hours** |
| Ranking | measurably worse ("eiffel tower height" → *Eiffel Tower (Paris, Texas)* first) | correct on the same probes |

So the mirror does **not** replace live — freshness and ranking are the whole
job — but it is the encyclopedia tier's fallback (`API_CUE_KIWIX_ENDPOINT`):
when live Wikipedia fails, the tier serves ZIM article leads instead of
contributing nothing, so cues stay grounded through an outage or throttle.
Kiwix's own search snippets are keyword-fragment cruft, so the fallback
fetches each hit's article and takes its first real paragraph — the same
content live's `exintro` returns. Hits are de-duped by title because the
instance deliberately serves two Wikipedia flavours side by side.

Verified with live Wikipedia pointed at a dead host: fallback retrieval in
~790ms, June-ZIM leads as evidence — and on a time-sensitive count that the
stale evidence did not cover, the bar stayed silent. Degradation is to
caution, never to stale confident answers.

## The enrichment calibration: cues must add, not echo

The first sustained real-world deployment (July 2026: 87 conversations, 1,966
transcript turns, 148 cues) surfaced the next round of problems, measured by
exporting every conversation and judging every cue (harness:
`scripts/cue_eval/`):

* **Restatement.** ~13% of production cues merely repeated what the speaker had
  just said ("Drone Payload: the drone can carry 1 kg of explosives" — the
  speaker's own sentence, re-worded). Zero information added.
* **Rephrased duplicates.** ~13% re-surfaced an earlier cue's fact under a
  fresh title ("Charlotte Drone Facility" / "US Drone Production Scale" /
  "Charlotte Drone Production" — one fact, three cues). The title-normalized
  dedupe cannot see these.
* **Wrong-cue classes.** ~10% judged wrong, in three recurring shapes: facts
  invented for misheard STT names ("PowerUs", "Grantham Show"), contradictions
  of what the speaker said firsthand (cue says 120 g right after the speaker
  read 201 g off the box), and "corrections" of the world from a stale training
  cutoff.
* **Muteness.** Replayed through the then-current ungrounded prompt, the same
  conversations yielded **8 cues in 566 attempts (1.4%)** — engineering
  discussions, plans, and how-to conversations got nothing at all, because the
  triggers keyed on named entities and the accuracy language read as
  "when in doubt, say nothing".

The fix reframed the prompt end to end (`openai.py _SYSTEM`): the model is a
research assistant whose cue must **enrich** — add a fact, definition, number,
comparison, or piece of background that was *not* said aloud — with restatement
banned outright, five triggers (answer questions; concrete facts about named
entities; define terms/jargon; inform the decision being worked through;
correct falsehoods), per-failure-class accuracy rules (no speculation, skip
garbled names, never contradict firsthand details, never fight the speakers
about post-cutoff changes), candidate discipline (if the best candidate is
unsafe or already surfaced, take the next-best instead of declining), and three
worked examples. The avoid-list now carries **title and body** so the model can
steer clear of substance, not just titles, and the session grew a second
dedupe backstop: a content-word fingerprint (Jaccard ≥ 0.5) that catches the
same fact returning re-worded. Decoding went greedy (temperature 0.2 → 0.0):
A/B on the same 675 replayed attempts, greedy cut judged-wrong cues 5 → 1 at
equal volume.

Replayed on the same 12-conversation set (LLM-judged, 0–2 scales):

| | cues | novelty | relevance | accuracy | restates | wrong | dups |
|---|---|---|---|---|---|---|---|
| production cues (old prompts + grounding) | 61 | 1.49 | 1.90 | 1.74 | 8 | 6 | 8 |
| old prompt, replayed ungrounded | 8 | 1.88 | 2.00 | 2.00 | 0 | 0 | 0 |
| shipped (enrichment + t=0.0), ungrounded | 27 | 1.78 | 1.93 | 1.93 | 2 | 1 | 0 |

Volume is ~3.5x the honest baseline before grounding adds coverage, quality is
at production level or above on every axis, and the conversations that used to
get zero cues (an engineering release discussion, a Raspberry Pi video, a
firearms lecture) now get genuinely additive ones (cherry-pick semantics, SemVer
patch conventions, cd/m² brightness context). The low-signal controls (movie
audio, a garbled-STT session) stay near-silent, as they should.
