# Cue-model eval — July 2026 (running results log)

Side-by-side comparison of the production cue model against candidate
replacements, replayed on the frozen 12-conversation deployment dataset with
the shipped enrichment prompt (`scripts/cue_eval/replay.py`, temperature 0.0).
This file is the durable record of the testing session — every iteration's
numbers land here as they are produced, so the comparison never depends on any
one session's memory. Deployment side: DockerOps PR #99 (`compose/tenir-gpu.yaml`).

## Eval setup

- **Dataset**: 12 recorded conversations (676 non-empty turns, ~63 talk-min)
  exported from the production DB 2026-07-26 — entity-dense media audio, an
  engineering release discussion, direct questions, and two low-signal
  controls (movie audio `87e2acbe`, garbled-STT `ed1d330d`) where more cues
  would be a defect, not a win. Conversation ids in `replay.py`'s history:
  `f7f4b10c 363a125e 77f7414d d3b5acc9 8d0cac50 87e2acbe 9a84d209 48de05be
  36692e0a 1cf67366 bbead742 ed1d330d`.
- **Replay**: exact session gating (8-turn window, one attempt in flight
  modelled as 2.5 s, 1.5 s min interval, title + substance dedupe), ungrounded
  (no retrieval evidence).
- **Judging**: LLM judge scores each cue 0–2 on novelty / relevance /
  accuracy + duplicate flag. Iteration 1 onward judges with a **cross-family
  model** (Gemma 3 27B) to remove the judge-shares-generator-weights blind
  spot that capped the July prompt-calibration numbers; those earlier numbers
  (judged by Qwen itself) are marked (qj).
- **Latency**: mean wall-clock per model call, measured by the replay run —
  it bounds cue frequency directly (attempts are serialized one-in-flight).

## Candidates

| iteration | endpoint | model | why |
|---|---|---|---|
| baseline | 10.10.10.22:9402 | Qwen/Qwen3.6-27B-FP8 | current production cue model |
| 1 | 10.10.10.22:9404 | openai/gpt-oss-20b | frequency lever: 3.6B active params, ~3–5× faster |
| 1 | 10.10.10.22:9405 | RedHatAI/gemma-3-27b-it-FP8-dynamic | like-for-like size; factual QA + Spanish; cross-family judge |
| 2 | 10.10.10.22:9406 | openai/gpt-oss-120b | quality lever: ~4× knowledge at near-27B latency (staged, commented in compose) |

Iteration 2 requires swapping iteration 1 out (VRAM); its Qwen comparison
column reuses the iteration-1 Qwen run on the same frozen dataset.

## Prompt-calibration baselines (2026-07-26, Qwen-judged (qj))

From the enrichment-prompt calibration (Tenir PR #80), same dataset:

| run | cues | attempts | novelty | relevance | accuracy | restates | wrong | dups |
|---|---|---|---|---|---|---|---|---|
| production cues (old prompts + grounding) | 61 | — | 1.49 (qj) | 1.90 | 1.74 | 8 | 6 | 8 |
| old prompt, replayed ungrounded, t=0.2 | 8 | 566 | 1.88 (qj) | 2.00 | 2.00 | 0 | 0 | 0 |
| enrichment prompt, t=0.2 | 29 | 675 | 1.83 (qj) | 1.86 | 1.52 | 1 | 5 | 2 |
| enrichment prompt, t=0.0 (shipped) | 27 | 675 | 1.78 (qj) | 1.93 | 1.93 | 2 | 1 | 0 |

Known noise floor: re-running the same variant swings cue counts ~±15%
(sampling at t=0.2; window alignment at t=0.0), so treat single-run deltas
under ~4 cues as noise. Residual wrong-cue class to watch: confident
specifics at the knowledge frontier (niche chemistry, hardware pinouts,
Docker internals) — the main thing a stronger model should fix.

## Iteration 1 — Qwen3.6-27B vs gpt-oss-20b vs Gemma 3 27B (run 2026-07-26)

All replays on the frozen 12-conversation set (675 attempts each), shipped
enrichment prompt, t=0.0, ungrounded. Cross-family judging: the Qwen run was
judged by gpt-oss-20b; the gpt-oss runs by Qwen; Gemma by **both** (the two
judges agreed closely — wrong 40 vs 38, dups 87 vs 74 — which validates the
judge protocol). "perfect" = novelty 2 + relevance 2 + accuracy 2 + not a
duplicate. gpt-oss was run at two reasoning efforts since that dial sets its
speed/selectivity trade.

| model | cues | emit% | novelty | relevance | accuracy | perfect | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Qwen3.6-27B-FP8 (current) | 22 | 3% | 1.59 | 1.95 | 1.86 | 14 | 3 | 1 | 1 | 0 | 1.30 |
| gpt-oss-20b, reasoning medium | 32 | 5% | 1.97 | 1.62 | 1.88 | 20 | 0 | 1 | 0 | 6 | 1.98 |
| gpt-oss-20b, reasoning low | 251 | 37% | 1.76 | 1.65 | 1.76 | 125 | 20 | 22 | 23 | 36 | 0.83 |
| gemma-3-27b-it-FP8 (qwen judge) | 448 | 66% | 1.57 | 1.58 | 1.77 | 179 | 55 | 40 | 87 | 95 | 1.66 |
| gemma-3-27b-it-FP8 (gpt-oss judge) | 448 | 66% | 1.62 | 1.71 | 1.68 | — | 70 | 38 | 74 | 95 | 1.66 |

### Findings (with hand review of flagged cues)

- **The prompt's emission calibration is model-specific.** It was tuned on
  Qwen and holds there (3% emit, controls silent). Gemma ignores it almost
  completely (66% emit, 28 cues on the garbled-STT control, 87 rephrased
  duplicates) — disqualified as a generator without its own calibration
  round. gpt-oss respects it at medium reasoning and abandons it at low.
- **The dataset holds far more good cues than the current model surfaces.**
  gpt-oss-low found ~125 judged-perfect cues (9x Qwen's 14) — volume is
  limited by the emission bar, not by available material. But it paid with
  22 confidently-wrong cues (invented display specs, wrong Pi 5 core count,
  wrong drone prices) — the exact failure class the accuracy rules exist for,
  at ~9% of output. Not shippable as-is.
- **gpt-oss-20b at medium is the honest challenger**: 20 perfect cues vs
  Qwen's 14 at the same wrong-cue count (1 each). Its 6 "control" cues are
  mostly *legitimate on hand review* (accurate definitions — carapace, Dolly
  the sheep, Red Skull — during a movie the household was watching; the judge
  docked relevance, a listener might not). Two real downsides: judged
  relevance 1.62 (it enriches tangentially more often) and 1.98 s/call vs
  Qwen's 1.30 s — the "frequency lever" thesis FAILS at medium effort because
  the reasoning tokens eat the speed advantage; the lever only exists at low
  effort, where accuracy collapses.
- **Judge-harshness caveat**: the gpt-oss judge marked Qwen's "32+32=64" cue
  novelty-0 ("restates the answer") — but the transcript *asked* the
  question aloud; answering is the product working. A couple of per-model
  points in the novelty/restate columns are judge artifacts of this shape.

### Iteration-1 verdict

Qwen stays ahead on precision-per-cue; gpt-oss-20b-medium edges it on good-cue
yield at equal accuracy but is no faster and noisier on relevance; neither
candidate delivers "more volume at maintained accuracy" cleanly. The
hypothesis that remains untested is that a *stronger* model can hold Qwen-like
discipline at a higher emit rate — exactly the iteration-2 gpt-oss-120b test
(~4x knowledge, ~5B active params). **Proceed to iteration 2.**

## Iteration 2 — gpt-oss-120b (run 2026-07-26, via Ollama)

**Serving note**: vLLM cannot load gpt-oss-120b on this host — SM120 gets the
Marlin MXFP4 fallback and its load-time repack transient OOMs the 96 GB card
every time (four attempts across shares 0.70/0.85, `--enforce-eager`, and
allocator tuning; WSL2 rules out the VMM-based mitigations — DockerOps
#104-#108). It runs cleanly on **Ollama/llama.cpp** (`gpt-oss:120b`, ~65 GB
GGUF, streams with no repack), which the eval used. Ollama latency is NOT
runtime-comparable to the vLLM rows (single-request stream; replayed with
`--workers 1`). Judged cross-family by Qwen.

| model | cues | emit% | novelty | relevance | accuracy | perfect | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-oss-120b, reasoning medium | 234 | 35% | 1.81 | 1.71 | **1.96** | **152** | 12 | **4** | 14 | 43 | 1.98 |
| gpt-oss-120b, reasoning low | 362 | 54% | 1.59 | 1.73 | 1.92 | 189 | 48 | 10 | 69 | 50 | 1.12 |

**The scale hypothesis held.** At medium effort the 120b emits 10x the current
model's volume at HALF its wrong-rate (1.7% vs 4.5%) — 152 judged-perfect cues
vs Qwen's 14 on the same replay. Hand review of all four wrong cues shows a
single failure class: confidently defining a misheard STT token ("GPX",
"CQV", "Yonka") instead of skipping it — the garbled-name rule holds less
firmly at this emit rate, a future prompt-tuning target. Its 43
control-conversation cues are mostly accurate definitions (carapace, Dolly the
sheep, SCNT) the judge docked on relevance, same pattern as the 20b in round
1. Low effort adds volume but degrades novelty (48 restates) and duplicates
(69 caught by the substance backstop) — medium is the shipping setting.

## Round 3 — Mistral Small 3.2 24B FP8 (run 2026-07-26)

vLLM note: Mistral's tokenizer mode rejects `chat_template_kwargs` with a 400
— replayed with `--no-template-kwargs` (harness flag added for this).

| model | cues | emit% | novelty | relevance | accuracy | perfect | restates | wrong | dups | ctrl cues | mean call s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| mistral-small-3.2-24b FP8 | 207 | 31% | 1.36 | 1.79 | 1.61 | 73 | 33 | **30** | 35 | 18 | **0.71** |

Fastest runtime of any candidate (0.71 s/call) and an emissive posture, but
the worst calibrated-run precision: 14.5% of its cues are judged wrong and 16%
restate the transcript. Same knowledge-frontier confabulation class as the
other small models, at higher volume. Eliminated.

## Decision

**Recommend gpt-oss-120b at reasoning-effort medium as the cue model.** Final
standings across every calibrated run (frozen 12-conversation replay, shipped
enrichment prompt, cross-family judged):

| model | perfect cues | wrong rate | emit% | runtime |
|---|---|---|---|---|
| **gpt-oss-120b, medium** | **152** | **1.7%** | 35% | Ollama, 1.98 s/call |
| gpt-oss-120b, low | 189 | 2.8% | 54% | Ollama, 1.12 s/call |
| gemma-3-27b | 179 | ~9% | 66% | vLLM, 1.66 s/call |
| gpt-oss-20b, low | 125 | 8.8% | 37% | vLLM, 0.83 s/call |
| mistral-small-3.2 | 73 | 14.5% | 31% | vLLM, 0.71 s/call |
| gpt-oss-20b, medium | 20 | 3.1% | 5% | vLLM, 1.98 s/call |
| **Qwen3.6-27B (current)** | 14 | 4.5% | 3% | vLLM, 1.30 s/call |

The 120b at medium is the only candidate that delivers the product goal —
substantially more cues at maintained-or-better accuracy. It is also the only
one whose volume comes from knowledge depth rather than loosened discipline:
every other high-volume run paid 9-15% wrong cues.

Open items for productionizing (not part of this eval):

1. **Serving**: production cues would ride Ollama (:9406) until vLLM's SM120
   MXFP4 path can load the model (upstream work exists). LiteLLM can alias an
   Ollama endpoint, so the api needs no change beyond the alias target and
   `reasoning_effort: medium` on the request (a small `api` config addition).
2. **Concurrency**: Ollama serves the eval fine single-stream; cue attempts
   are serialized per session anyway. Multi-session households should verify
   `OLLAMA_NUM_PARALLEL` covers their session count.
3. **Prompt fine-tune**: the misheard-name failure class (4 cues) suggests
   strengthening the garbled-name rule for this model; the relevance dings on
   ambient-media definitions may also warrant a "only cue what the LISTENER
   is engaged with" nudge. Both are small calibration passes with this same
   harness.
4. **VRAM**: 120b (~65 GB) + both STT models (~5 GB) fit the 96 GB card with
   room to spare once the eval services are torn down; Qwen would retire.

## The audience-model calibration (2026-07-26, from the first real session)

The first real-world session on the deployed stack (a 21-minute family outing,
conversation `dbc3080e`, now a harness fixture alongside the original 12) was
factual and well-paced but ~60% irrelevant, in two patterns the entity-dense
eval set never exposed: **dictionary cues** (defining everyday things — foods,
pockets, playground games — to an adult, made worse by the grounded bar, since
Wikipedia has evidence for every mundane noun) and **famous-entity misfires**
(casual words, kid-talk, and slang resolved to brands/celebrities/works).

Fixes, calibrated over four replay iterations against that session plus the
regression set:

- **Audience model in the prompt**: a cue must carry something an adult
  listener plausibly does not know; everyday things are never cue-worthy as
  topics (trivia about a mundane thing is still a cue about a mundane thing);
  casual-register conversations get the interest bar (questions, guesses,
  disputes, plus ES⇄EN translations — which measured as some of the most
  valuable cues) rather than every-noun coverage; short bare names in casual
  talk are people present, not celebrities. Negative worked examples for the
  two failure classes; evidence-coverage generosity in the grounded bar now
  explicitly subordinate to these rules.
- **max_tokens 300 → 600**: the reasoning model's analysis channel measurably
  starved the answer at 300 (finish_reason=length with empty content —
  silently dropped cues on exactly the highest-context turns). This was
  suppressing volume across all v7/v8 iterations and in production.

Family-session scorecard (hand-bucketed against the categorized production
run; GOOD = expressed-interest answers, engaged-entity facts, translations):

| run | cues | good | dictionary | misfires |
|---|---|---|---|---|
| production (v6 prompt @300 tokens, grounded) | 39 | ~15 | 12 | 12 |
| v6 replayed ungrounded | 42 | 12 | 9 | ~8 |
| **v9 + 600 tokens (this change)** | **48** | **23** | 5 | 6 |

Regression set holds volume (330 cues at 600 tokens vs 243 at starved 300)
with the same shape. Known residuals, deliberately not chased further with
prompt text: brand tokens inside coherent-looking garbled windows (the
"Audi" cluster — an ASR-confidence problem; retrieval cross-checking of
entity cues is the likely future fix), kids-media trivia on ambient
conversations, and a compound edge case where a thrice-repeated question in
one window can still exhaust the reasoning budget.

## Deployment verification & final tuning (2026-07-26, post-promotion)

gpt-oss-120b was promoted (DockerOps #110) and wired through LiteLLM. Final
verification against the deployed layout:

- **Gateway overhead is negligible.** Identical cue-style calls, 12 each,
  warm: direct Ollama **1.440 s**, LiteLLM via LAN **1.424 s**, LiteLLM via
  the Cloudflare tunnel (the api's actual path) **1.460 s**. Routing through
  the gateway — even the tunnel — costs ~1–2%. No endpoint change needed.
- **Gateway entry must use OpenAI-compat passthrough.** A LiteLLM entry using
  the native `ollama` provider returns EMPTY content whenever the request
  carries `response_format: {"type": "json_object"}` — which the api sends on
  every cue call. Isolated by ablation: same payload works with
  response_format removed, and works with response_format against Ollama's
  own /v1. The entry must be `model: openai/gpt-oss:120b` with
  `api_base: http://GPU_HOST:9402/v1` (the surface this whole eval
  validated), plus `reasoning_effort: medium` pinned.
- **Also fixed**: the api's `API_LLM_MODEL` default now names the gateway
  entry directly (`gpt-oss:120b`, DockerOps #111) — the retired `qwen3-llm`
  alias 403s.
- **Prompt v6 (shipped in this PR)**: the misheard-name rule now includes a
  domain-fit check (a famous meaning from the wrong domain = a mishearing —
  skip the name). Full replay at medium effort: garbled-name cues **5 → 3**
  with volume **up** (234 → 243) and controls unchanged. The residual ~1% is
  boundary noise — single-window A/Bs flip run to run — and is best cleaned
  up by the live retrieval evidence in production (a misheard name retrieves
  nothing that supports the wrong definition), not by more prompt text.

## The session-2 precision pass (2026-07-27, from the second real session)

The second real-world recording (21 minutes of entity-dense YouTube tech
audio, conversation `7ca38157`, now a fixture alongside `dbc3080e` and the
original 12) confirmed the audience model held — good frequency, zero
restatements, corrections and specialist enrichment landing — and exposed
five precision classes the family session couldn't:

1. **Cross-generation confabulation** (worst): a model number newer than the
   weights gets a sibling's specs pasted under its name ("Galaxy Z Fold 8 …
   launched August 2023, Snapdragon 8+ Gen 1"), including via retrieval
   miscite (a `[Wikipedia]`-labeled launch date lifted from the predecessor's
   article).
2. **False corrections**: "there is no official version called Jet Grind
   Radio" — there is; it was the US title. The correction trigger inverted
   truth for the first time.
3. **Paraphrase duplicates**: the same fact re-surfacing retitled and
   reworded ("God of War 4 naming" → "Official title", 50 s apart). Measured
   substance-Jaccard of the pairs: 0.18–0.38 — all under the 0.5 backstop.
4. **Mid-word truncation**: 8 of 51 bodies chopped by the hard
   `body[:240]` slice ("…vascular trend monito").
5. **Stale-topic cues**: with fast topic switches, the 8-turn window kept
   cueing topics the audio had left 60+ s earlier (a sleep-apnea cue
   mid-emulator-talk).

### Code fixes (measured, not prompted)

- **Word-boundary clip**: `_clip_at_word` clips at the last full word + "…".
  With the new "under 200 characters" nudge in the output format, overruns
  nearly vanished (v10 full replay: 0 of 363 bodies clipped; v12 final: 2 of
  401, both ending on a clean word + ellipsis instead of mid-word).
- **Substance-dup threshold 0.5 → 0.35**: calibrated on both real sessions'
  90 production cues — near-verbatim rewords 0.57–0.87, retitled paraphrases
  0.30–0.38, closest genuinely-distinct pairs 0.26–0.27. The two classes
  overlap below 0.30, so 0.35 is the safe floor; deeper paraphrases are the
  prompt's job (below).

### Prompt iterations (v10 → v12, replayed on both real sessions per step)

- **v10** — generation-mismatch rule (memory and evidence side), correction
  caution (never cue that a name the speakers used "does not exist"),
  same-subject-different-angle ban on the avoid list, newest-turns recency
  rule. Session-2 replay: 88 → 53 cues with the cuts landing on the failure
  classes (backstop drops 9 → 24); Jet Grind Radio now *correctly*
  attributed; stale and dictionary cues mostly gone. But it introduced
  **date-arithmetic false corrections** ("…so its 20th anniversary occurred
  in 2021-23 — not a 25th" — the model doesn't know the current year), and
  cross-generation confabulation survived.
- **v11** — "you do not know today's date" rule, lifestyle-advice ban, and a
  negative worked example for newer-product specs. Date corrections fixed
  (LOTR 25th now correctly 2026), advice cues gone from the family session.
  Confabulation *still* survived: the model doesn't experience "Z Fold 8" as
  post-cutoff — it pattern-matches the family and confidently retrieves
  sibling facts, so a "newer than you know" test never fires.
- **v12 (shipped)** — reformulated as a **specific-memory test** the model
  can actually run: "family resemblance is not knowledge — before stating a
  spec, check you specifically remember THAT model's release; no specific
  memory, no cue", extended to vaguely-recognized names and acronyms, plus a
  bilingual mishear rule (a stray foreign-looking word is the speakers'
  other language, not Dutch). Result: the invention class collapsed — the
  invented "ALOP = Advanced Lens Optimization Process" expansion, "Eden is a
  PS2 emulator", and a fabricated "RingCon is Oura's annual conference" all
  gone, replaced by silence or hedged class-level statements (the ideal: "an
  S26 Ultra would be expected around 2026" instead of invented specs). The
  effect is real but stochastic: the final full run re-produced a couple
  (Eden-as-PS2 returned; Jet Grind Radio kept the right name but moved to
  the wrong platform), so the class is roughly halved per run, not
  eliminated — see Residuals.

### Final numbers (full 14-conversation replay, gpt-oss-120b @ medium)

All three runs replay the same 14-conversation export (985 attempts:
the frozen 12 + both real sessions), self-judged by the same model at low
effort — treat scores comparatively, not absolutely.

| run | cues | novelty | relevance | accuracy | judge dups | garbled ctrl `ed1d330d` | question conv `1cf67366` |
|---|---|---|---|---|---|---|---|
| v9 (shipped baseline) | 461 | 1.87 | 1.93 | 1.92 | 28 | 10 | 1 |
| v10 | 363 | 1.85 | 1.91 | 1.92 | 19 | 6 | 5 |
| **v12 (shipped)** | **401** | **1.88** | 1.90 | **1.94** | **17** | **5** | **5** |

Hand-bucketed on session 2 (the entity-dense one), the wrong/confabulated
class runs ~15 (v9 replay) → ~10 (v10) → ~5±2 (v12); paraphrase-duplicate
pairs 6+ → ~1-2; stale-topic and dictionary cues near zero from v10 on.
Volume lands at 87% of baseline with the cuts concentrated in the failure
classes, while the direct-question conversation goes from near-mute (1 cue)
to properly answered (5). The ambient-movie control (`87e2acbe`, 40 → 44)
is unmoved — ambient media stays the audience-model's known weak spot.

### Residuals

- **First-party sibling-spec transfer** (the Z Fold 8 case) survives every
  prompt formulation tried — four variants plus production — because the
  model genuinely believes it knows the product. Prompt-side mitigation has
  hit its ceiling; the fix is the already-planned **retrieval cross-check of
  entity cues** (a cue asserting specs for a named model must be covered by
  evidence about that exact model).
- Casual-register replay variance: the family session fluctuates ±6 cues and
  ~1/3 marginal-quality run-to-run at temperature 0 (avoid-list path
  dependence); the wins that persist across all runs are the structural ones
  (clusters collapsed, advice and date-corrections gone).

## The professional-audience pass (2026-07-28, from seven post-v12 sessions)

v12 shipped the morning of 07-27 (v0.3.13). Seven real sessions ran on it
over the next two days — and three of them were a register no prior review
had covered end to end: professional working conversations (a DevOps
onboarding call, its screen-share continuation, and a team standup). Hand
review of all 396 production cues found v12's replay wins did not carry
into that register. Failure classes, by size:

- **Professional-vocabulary dictionary flood** (~120 cues, the dominant
  class): the audience model banned everyday things but not the speakers'
  OWN working vocabulary — a standup of engineers was told what a pull
  request, TLS, a backlog, and CI are; a DevOps onboarding call got
  "DevOps", "Semantic Versioning", and — the emblem of the class —
  "Monday (ISO): Monday is officially the first day of the week" and
  "Link sharing: sending a URL to someone is called 'sharing a link'".
  Root causes: rule (3) invited definitions of "specialist terms" with no
  audience test, the worked GOOD example (feature flags, at engineers)
  legitimized exactly this, and the "do not give up — take the next-best
  candidate" escalation forced emission when nothing qualified.
- **Wrong-referent names and acronyms** (~25): coworkers resolved to
  celebrities and products (Jonathan → the actor Jonathan Roumie mid-
  standup; Archie → Archie Moore the boxer, the Archie FTP index, AND a
  name-origin cue; a kid's friend Ezra → the biblical scribe), internal
  names to public things (their Tesoro brand → the oil refiner; "D scope"
  (Descope) → programming scope), and acronyms across domains (MCP →
  Minecraft Coder Pack in a Model Context Protocol discussion; their RPM
  app → Red Hat Package Manager AND Release Planning Meeting; VBS misheard
  as BBS → two dial-up bulletin-board cues).
- **Same-subject fan-out below the Jaccard floor** (~30 pairs/clusters):
  eight SAML cues, three Kibana, three Atom, "AWS Cognito User Pools" then
  "...User Pool" (0.348 vs the 0.35 threshold), "Pressure Cooker" then
  "Pressure Cooking". Measured pair similarity 0.04–0.33 — overlapping the
  genuinely-distinct band, unreachable by any substance threshold.
- **Garbled-audio firehose**: once a screen-share degenerated into
  fumbling ("Open lid", "free center", mumbled "cat"/"less"), every
  recognizable token got a cue — Freeway Lid, BYU Testing Center, the Unix
  commands, Kevin Sorbo from "Hercules road", plus homophones of live
  topics (Kibana → Kiva microfinance, SAML → the biblical Samuel).
- **Retrieval keyword-matches** (~6): evidence sharing only a phrase with
  the talk produced cues about the wrong subject — two Australian
  cost-of-living-poll cues during talk about the West — and one
  retrieval-powered FALSE correction ("the price increase starts 5 October,
  not September 1" — a Royal Mail article, against speakers discussing
  Xbox prices).
- **Smaller classes**: calendar/duration trivia ("Friday is the fifth
  day", "planning meetings run 30-60 minutes"), invented glosses of
  garbled foreign fragments ("Vivía de centro" as a Spanish "idiom", a
  wrong Russian imperative), invented generic definitions of internal
  service names, restatements with silently altered numbers (spoken 24/39
  min → cue 25/40), and one first-person frame-break ("Link Access — I
  can't open or view external URLs").

### The fix (v14 = the audience pass + the absorbed v13.3 iteration)

A parallel iteration on branch `fix/cue-mishear-dedup-audience` had
independently reviewed the 07-27 work calls and replay-validated fixes for
the same classes (work-call volume 142→83 and 54→38 with movie/family
controls healthy); its refinements are merged here rather than re-derived.

Prompt: novelty is judged against THESE listeners — fluent use is proof of
knowledge, jargon fires only on a shown knowledge gap, and for
practitioners the bar is a certain, specific fact ("news to a
practitioner"), never an invented statistic substituted for a banned
definition. Bare names default to coworkers/friends/internal systems;
acronyms resolve in-domain or not at all; entities need conversational
support plus a second signal; sound-alikes of in-conversation things ARE
those things; a bare mumbled word is not a topic; answered questions are
closed; translations require recognizing the whole phrase; the everyday
ban covers calendar facts, units, well-known sites/formats, and typical
durations; evidence must match the conversation's subject, never powers a
correction, and cannot rescue a mishearing; the observer stance is
explicit. The "take the next-best candidate" escalation is replaced by
"declining is a normal outcome".

Code: a third dedupe layer — intersection over distinctive title-subject
tokens, one subject one cue per conversation — with an angle/category/
brand-family exclusion list grown by hand-classifying every collision
across both calibrations (316-cue/8-session, then 396-cue/7-session: 97
collisions classified, 7 generic tokens added, 90 drops all
same-subject repeats, zero distinct-subject losses).

### Final numbers (full 10-conversation replay, gpt-oss-120b @ medium)

Frozen set: the seven post-v12 sessions plus three controls (ambient-movie
`87e2acbe`, ambient-TV `1cf67366`, garbled `ed1d330d`). Both runs on the
same 2026-07-28 export (1326 attempts), self-judged by the same model at
low effort — comparative, not absolute. The v12 baseline reuses the 07-27
iteration's four per-conversation checkpoints (prompt-identical) and
replays the other six.

| run | cues | emit% | novelty | relevance | accuracy | restates | wrong | judge dups |
|---|---|---|---|---|---|---|---|---|
| v12 (shipped baseline) | 522 | 39% | 1.89 | 1.92 | 1.98 | 16 | 1 | 21 |
| **v14 (this branch)** | **179** | 13% | **1.94** | **1.94** | 1.98 | **3** | **0** | **4** |

Where the volume went, hand-read per conversation: the onboarding call
63→15 (survivors include CREATE INDEX CONCURRENTLY for their failing
index and RabbitMQ binding-deletion mechanics for their manual-unbinding
problem — the "news to a practitioner" bar working), its screen-share
165→51 (the eight-SAML fan-out replaced by "Azure AD mail vs
userPrincipalName", directly on the bug being debugged; the mumble-driven
Unix-command cues 10→3), the standup 158→56 (96 same-subject repeats
absorbed by the new gate; Twilio Lookup pricing and CSP frame-ancestors
survive), the podcast+family session 47→22, and the garbled control 7→1.
Spoken questions still get answered (the euros-vs-pounds and did-the-UK-
rejoin questions both cue correctly). The ambient-movie control 40→16
with every survivor but one a genuine companion cue (Dolly, Highlander,
LGM-1, Kessler, SETI) — same junk-removal shape the 07-27 subset run
measured (its 19-cue movie result), not a suppression of the register.
`1cf67366` turns out on transcript reading to be ambient sitcom audio,
not the direct-question session earlier notes assumed: its v12 cues were
four wrong-referent/trivia cues plus one genuine companion cue, and v14
keeps exactly the companion cue.

### Residuals

- **Wrong-referent and invented cues survive stochastically** at roughly
  10-15% of the (much smaller) volume: "Grunt the JS task-runner" for
  grunt work, "RPM package format" for their RPM app, an "Opus codec" cue
  for their Opus integration, one invented "Global Instruction File"
  definition, and one invented "follow-up within 24 hours" statistic
  despite the new guard. Same shape as v12's confabulation class: prompt
  rules halve it per run, never eliminate it. The planned retrieval
  cross-check of entity cues remains the structural fix.
- **The 13% emit rate is a deliberate floor-setting** — if deployment
  feels too quiet in casual registers, tune the register paragraph, not
  the accuracy rules.
- **The grounded-path rules ship replay-untested**: evidence
  subject-match, no-corrections-from-evidence, and
  evidence-cannot-rescue-a-mishearing only fire with live retrieval,
  which the harness cannot replay. The next deployment review must check
  the Guardian-keyword-match and Royal-Mail-style false-correction
  classes specifically.

## The garble-and-vendor pass (2026-07-30, from the first sessions under v14)

### The deployment review

Four sessions recorded since the v0.3.18 deploy (2026-07-28): an ambient
tech video (`0aff8ae8` — a Z Fold 8 review plus a Framework 13 Pro build,
both post-cutoff products) and a two-part accented engineering meeting
(`2837af01`/`bd758f83`, a malware-analysis-pipeline knowledge share — the
first real professional-audience data since the v14 audience pass). Hand
review of all 113 production cues found ~36 defective, in six classes:

- **Garbled-STT acronym chasing** (~15, the dominant class): the accented
  audio garbled the team's internal acronym differently each utterance and
  every variant got its own cross-domain definitional cue — one internal
  term was "defined" five ways (occupational-health Display Screen
  Equipment, automotive Data-Collecting Unit, telecom Data Service Unit,
  Software Transactional Memory, plus a confabulated "industry" expansion
  of the real one). Other members: RabbitMQ → Autodesk Revit plugins,
  "verdicts" → Google Vertex AI (the speakers had named OpenAI), "upload
  sample" → Apache Airflow, Mandiant's capa → Kappa architecture, "malware"
  → an invented "Evalware" family. One cascade: a bad automotive-DCU cue
  was followed by an NB-IoT cue chaining off the cue's own wrong content.
- **Wrong-vendor tech swaps** (~6): Kafka topics/offsets and an SQS
  visibility timeout explained to a team that had repeatedly named RabbitMQ
  as their broker — the SQS cue landing directly on their real RabbitMQ
  double-delivery bug — plus Airflow pitched at their in-house orchestrator
  and S3 Glacier at their internal archive service.
- **False corrections / cross-gen confabulation** (~4, known residual):
  the Fold 8's stated cover screen "corrected" with Fold-7-era specs, the
  correct IP48 rating "corrected" to IPX8, a post-cutoff Intel chip filed
  under Meteor Lake, the 2021 iPad mini called current.
- **Thematic redundancy below both gates** (~7 clusters): pub/sub explained
  three times in 80 s ("Kafka topic broadcast" / "Publish-Subscribe" /
  "Message Broker"), GraphQL round-trips twice in 23 s, sequence numbers
  twice in 23 s. Measured pairwise: substance 0.03–0.28, subject overlap
  0.000 — legitimately below the calibrated thresholds, so these are not
  backstop bugs but an unhandled class (same idea, fresh title and angle).
- **Invented stats** (~5): Defender "over 200 attack-surface-reduction
  rules" (real count ≈19), "4:3 gives ~33% more vertical space" (≈22%),
  "~1 ms per inter-service call", "retry libraries default to three".
- **Bare-fragment misfire** (1): a lone "Confidence." during goodbyes
  produced a confidence-interval statistics cue.

### The fix (v15 → v15.2) and what replay actually showed

Four prompt rules, each pinned by a test: (1) a short acronym a letter or
two off one already in the conversation is that acronym misheard, and once
the speakers treat an acronym as their own, later variants are the same
internal term — never a fresh cross-domain definition; (2) when the
speakers have named the product they use for a job, a rival product doing
the same job is the wrong subject, and another vendor's defaults never
apply to theirs; (3) typical/common/default/round-number figures are
invented statistics unless the documented value for the exact named thing
is specifically remembered (v15.2 promoted this into the top-level accuracy
rules after the first candidate run showed it leaking in casual registers);
(4) the avoid-list counts different names for one idea as one subject, and
"concept" joins the bare-mumbled-word rule.

Frozen set: the three new sessions plus three established controls
(ambient-movie `87e2acbe`, ambient-TV `1cf67366`, garbled `ed1d330d`),
re-exported and re-baselined 2026-07-30; gpt-oss:120b @ medium, temp 0;
795 attempts per full run (baseline, v15, v15.2 subset, v15.2 full).

| run | cues | emit% | novelty | relevance | accuracy | restates | wrong | judge dups |
|---|---|---|---|---|---|---|---|---|
| v14 (shipped baseline) | 147 | 18% | 1.90 | 1.97 | 1.95 | 3 | 1 | 4 |
| **v15.2 (this branch, full run)** | 150 | 19% | 1.92 | 1.93 | **1.99** | **1** | **0** | **1** |

Hand-read, the honest picture is narrower than the scorecard suggests:

- **Reproducible wins.** The serial same-acronym expansion pattern (five
  definitions of one internal term) appears in no candidate run. The
  message-broker cues now mostly ground in the speakers' actual RabbitMQ
  (including the best cue of the exercise — RabbitMQ's single-active-
  consumer mode, v3.8, offered exactly on their double-delivery bug), and
  Document360/Ingate get accurate descriptions where the baseline invented
  an Atlassian acquisition and a web-gateway product. Judged dups 4→1 and
  the movie control sheds its one confabulated cue (a nonexistent "Yonka
  vodka") in both candidate runs; all three controls otherwise unchanged.
- **Churn, not elimination.** The broader garble-chasing class holds
  roughly flat per run (~8–10 baseline, ~7–10 across candidates) — it
  relocates instead of shrinking: one candidate run invented an "Orchid
  ransomware" family from an "orchestrator" garble, another an "Orchid
  VPN"; a garbled "stored procedure" became DTMF signalling; "the C and
  Python" became MicroPython; the team's own product got defined as a
  public SAST scanner. The wrong-vendor class dropped in part 1 but
  returned in part 2 of the final run (a Kafka/SQS cluster around the
  same bug). The invented-stat rule removed the retry-count and
  compression figures in one session while a CTR benchmark elsewhere
  gained an invented citation ("WordStream 2023 data").
- **Unmoved.** False corrections/cross-gen confabulation on the ambient
  video (~9–12 per run in every condition — one run "corrected" the Fold
  8's price onto a $199 Analogue Pocket). Sub-threshold thematic
  redundancy (the pub/sub cluster reformed in three of four runs).

### Residuals and the next step

Three prompt passes in three days have converged: each new rule removes
its named examples and the model's error mass shifts to the nearest
unnamed neighbour. The classes that remain — confabulated resolutions of
garbled tokens, plausible-but-wrong vendor facts, invented figures with
invented citations, post-cutoff false corrections — are all failures of
*unverifiable memory*, which prompt text can redistribute but not remove.
The structural fixes stay what the v14 residuals section named: a
retrieval cross-check of entity cues before emission (an entity cue whose
subject retrieval cannot corroborate in-domain gets dropped), and possibly
a session-level garble-density gate (suppress entity cues entirely while
the transcript's coherence is low). Those are the next investment, not a
v16 wording pass.
