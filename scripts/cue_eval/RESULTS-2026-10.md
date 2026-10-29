# Cue + STT review — October 2026 (first session on the INT4 cue server)

Hand review of the 2026-10-06 recording (27 min, 390 turns, 26 production cues) on
the new cue stack: Qwen3.8-27B W4A16 INT4 on vLLM with MTP drafting (`tenir-cue`), 512-token
thinking cap in the LiteLLM route. The session is mostly a desktop-CNC promo video played twice,
a family disagreement, and a water-flosser demo. Previous rounds: `RESULTS-2026-09*.md`.

## Production cues, hand-graded

12 good, 9 weak, 5 wrong. Failure classes:

- **Assistant mode** — the cue edited what was being said ("Copy polish: Here's a tighter
  version…" of the ad copy) or spoke as an assistant.
- **Neighbouring-domain transfer** — a CNC term explained as 3D printing ("In 3D printing, a
  ready-to-machine model…"; a safety interlock "disables hot parts").
- **Refereeing a disagreement** — cues backing one side of a family argument or lecturing on
  habits.
- **Garbled names given meanings** — "BMSM" (misheard PMSM) and the misheard brand mapped to a
  real but different company.
- Weak: generic textbook definitions late in a topic, tangents, a non-answer to a direct question.

## Latency (live)

- Cue calls landed every ~8 s during speech (median gap between calls in the API log).
- One call ≈ 4.7 s mean / 6.1 s p90 on an idle server: ~0.4 s prefill (~1.4k-token prompt),
  the rest decode at ~95 tok/s; about half the calls run the full 512-token thinking budget.
- Evidence retrieval adds ≤0.8 s (its deadline) and is usually a cache hit.
- vLLM prefix-cache hit rate is ~0%: the hybrid model caches in 1,600-token blocks and the
  shared prompt prefix is shorter than one block. Not worth restructuring the prompt for.
- The server's KV pool admits one request at a time ("Maximum concurrency 1.14x"), so a second
  session's cue — or an eval replay — queues behind the first.

## Change 1: catch-up cue calls (session.py)

A turn finalized while a call was in flight was skipped; it waited for the next final, or got no
call at all if the speaker then went quiet. Now the in-flight call, when it ends without showing a
cue, starts one more call on the newest turns.

- Simulated on the 5-conversation eval set's final timings with the measured call times (4.7 s
  mean + 0.3 s retrieval): time from a turn's final to the end of the first call that saw it went
  from **8.1 s mean / 13.5 s p90 to 6.9 s / 10.4 s**, for ~30% more calls (892 → 1,150).
- `replay.py` now models the catch-up, so replays measure production gating; the replayed attempt
  count on the reviewed session rose 285 → 352.

## Change 2: prompt guards (cue/openai.py)

Eval set (frozen): the reviewed session, three earlier sessions with real cue traffic (a music
session, a worship service, a board-game evening) and a low-cue control. `replay.py --grounded`,
t=0, shared evidence cache; temperature 0 made repeat runs identical, so each row is one run.
Bad cues hand-counted on the reviewed session.

| prompt | reviewed session cues | bad (hand) | other 2 sessions |
|---|---|---|---|
| shipped | 47 | ~10 (21%) | 8 |
| + 3 BAD examples (copy edit, side-taking, neighbouring domain) | 43\* | 1 copy edit, 2 side-taking, 3 new 3D-printer guesses | 6\* |
| + "don't guess what the thing is" + "never take sides" rules | 31\* | ~4 | 0\* — lost good cues |
| **shipped choice: guess rule narrowed to "which device or product"** | **35** | **~6 (17%)** | **10** |
| same minus the side-taking rule/example and the e-bike example | 43 | ~10 | 5 |

\* run before `replay.py` modelled catch-up calls (fewer attempts); compare within a column only.

- Copy-editing / assistant-mode cues: 2–5 per run on the shipped prompt → 0–1.
- Device guesses ("if it's a pump…", 3D-printing marketplaces): ~3 → ~1.
- The broad guess rule silenced good cues elsewhere (idioms, pigments, history); narrowing it to
  devices and products restored them.
- Removing the side-taking example and the e-bike example brought the bad count straight back,
  so they ship even though side-taking cues themselves did not drop (2 → 3): **that class is not
  fixed** — it reads the disagreement as a definitional question and answers it.

## STT

- Product names misheard many ways ("Tool Dance / Toolance / Two bands"): a Parakeet limit, also
  wrong in a 25 s-window re-transcription. Needs vocabulary biasing the server doesn't offer.
- 122/390 turns are ≤3 words and 6 boundary words appear in both neighbouring turns: the VAD
  closes turns mid-word (false silences on quiet far-field speech; the 8 s cap). Replaying the
  WAV through the real VAD: silence 800 ms + cap 12 s cut divergence from a long-window proxy
  16.2% → 12.1% but delays finals ~2.4 s → 3.9 s — a product decision, filed as XERK-1667.
- A post-hoc "drop the repeated boundary word" fix was tried three ways and abandoned after QA
  (text match, audio-level gate, joined re-decode): each deleted real repeats across a pause.
  Details on XERK-1667.

## Residuals / next

- Side-taking in family arguments survives every prompt variant tried; next attempt should be a
  session-level signal (two speakers disagreeing) rather than more prompt text.
- Garbled-name meanings ("BMSM") persist in all variants.
- Decode speed (~95 tok/s for a 27B INT4 + MTP on an RTX PRO 6000) is the dominant latency term;
  that is server tuning in the ArgoCD repo, not Tenir.

## Thinking-cap accuracy and the self-check (2026-10-11, XERK-1455)

Is the 512-token thinking cap in the `qwen3.8-27b-int4` route accurate enough on real
conversations, and does the self-check help at that cap?

Setup:

- Frozen 6-conversation set, fresh export. `replay.py --grounded --realtime --workers 1` through
  the gateway alias. The model is INT4 + MTP on the RTX 3090.
- The cap is set per request (`--extra '{"thinking_token_budget": N}'`), which overrides the
  route's value. All runs shared one warmed evidence cache (145/917 windows empty).
- Blind judging: the runs were pooled and shuffled into 4 packs, one reviewer each
  (`blind_judge.py`).
- Anchor: the production cues of the same six conversations (July 2026, gpt-oss-120b). Those
  stay in the `cues` table, so later batches can rebuild the same anchor.
- Self-check: `VERIFY_SYSTEM` with thinking off, run over each replay's own cues (same window,
  same evidence). That isolates what it drops from the avoid-list knock-on of a separate run.

| run | call mean / p90 | cues | wrong | perfect |
|---|---|---|---|---|
| anchor (production, July) | — | 114 | 15.8% | 26 |
| **cap 512 (shipped)** | **4.5 s / 5.8 s** | 137 | **3.6%** | 81 |
| cap 512 + self-check | +0.3 s | 114 | 2.6% | 65 |
| cap 768 | 5.5 s / 7.9 s | 136 | 6.6% | 86 |
| cap 768 + self-check | +0.3 s | 119 | 5.9% | 74 |

What this shows:

- Cap 512 is accurate on this data. Its 3.6% wrong (5 cues) is below the September full-thinking
  row (5.6%). Spot-checked wrong cues: a misheard game title given invented facts, Thunderbolt 4
  called the current generation, a 1,300 km orbit called MEO, a Fold cover-screen aspect claim,
  and a GNOME pronunciation.
- Cap 768 buys nothing for 1.0 s more mean and 2.1 s more p90. It had 9 wrong vs 5, and 86
  perfect vs 81. Both gaps are within the ±3 pt batch noise. The route stays at 512.
- The self-check does not help at the shipped cap, so it is not shipped.
  - At 512 it dropped 23 cues and only 2 of them were wrong. It cost 16 perfect cues to remove
    2 wrong ones.
  - Almost all its drops are hedged engineering advice in the meeting sessions ("If X means
    RabbitMQ…", idempotency and queue-design notes). It reads them as unsafe guesses.
  - It still passes subtle spec errors such as the aspect ratio, the IP rating and the
    pronunciation. That matches September: without thinking it only catches coarse errors.
  - Live (realtime) it dropped 48 of 163 candidate cues (115 shown) at 0.30 s mean / 0.35 s p90
    per check. That is the same picture as the after-the-fact check.
- Latency on real transcripts at 512 (4.5 s mean, 5.8 s p90 on the 3090) is about half the
  synthetic-transcript figure from XERK-1422 (8.9 s). Real windows are shorter, and about 30%
  of calls decline early.

Residuals:

- Remaining wrong cues are subtle facts about post-cutoff or edge-of-knowledge products.
- Better grounding is the remaining lever: the news FTS tier is still skipped in replay.
- A self-check would need a new prompt that tolerates hedged advice, or thinking on, which costs
  the latency the cap saved. Neither is measured yet.

## Value bar (2026-10-18, prompt "v8")

Hand review of the 2026-10-11..14 sessions (355 production cues, 3 reviewers): about 10% were
good, about 75% added nothing. The failure classes, largest first:

- Defining what the speakers obviously know: their own town, their church's vocabulary, the show
  they are watching, a term a speaker had just explained.
- Unsolicited advice: medical, parenting, legal and moral lectures on stories nobody asked about.
- Restating what was just said, or labelling the moment with a concept ("cognitive load").
- Background audio: worship lyrics, sermon filler ("a fitting verse is…"), a TV show.
- Confabulated specifics (wrong actor, type, year, scripture detail), about 10% of cues.

Method:
- Frozen eval: 4 segments of those sessions (25 min AI/piracy talk, an 18 min sermon with a
  live tornado warning, a 45 min slice of the 3.6 h session, a 35 min church slice).
- Replay: `replay.py --grounded`, shared frozen evidence cache, about 1,140 attempts per run.
- Grading: blind and cross-family. Claude graders saw both runs' cues pooled and shuffled in
  the transcript, unlabelled, and graded each cue GOOD, MARGINAL or BAD with a class.
- The shipped prompt was re-graded in every batch as the anchor. Its good count moved 22-27
  between batches, so differences of ±3 good cues are noise.

| prompt | cues | good | marginal | bad | good/bad |
|---|---|---|---|---|---|
| shipped v5 (3 gradings) | 176 | 22-27 | 49-52 | 98-105 | 0.21-0.28 |
| v1: no "most turns", value block, blanket advice ban | 82 | 12 | 26 | 44 | 0.27 |
| **v8 (= v1 + answer asked questions, define only when asked; 2 gradings)** | 85 | 14-16 | 24-25 | 44-47 | 0.30-0.36 |
| v9: v8 + stricter definition and past-story rules | 85 | 12 | 30 | 43 | 0.28 |

What moved, between the shipped prompt and v8:
- Unsolicited advice fell from about 32 to 9.
- Background-audio cues fell from 9 to 0, and restatements from about 10 to 3.
- Definitions fell from about 30 to 15.
- Wrong facts barely moved (about 10 to about 11).

Failed attempts:
- v1's blanket ban on medical, legal and financial advice also suppressed answers to asked
  questions ("how is that not illegal?", live tornado shelter guidance). v8's "answer the
  questions the speakers raise" brought those back.
- v9 lost more good cues than it removed bad ones.

Residuals:
- v8 shows about 35% fewer good cues. That is the price of the bad cues it removes, not a free
  win.
- Confabulated specifics are unchanged. The "know it cold" rule didn't move them, and prompt
  wording hasn't touched this class in any round. A grounded entity check is the next lever.
- Definitions are still the largest bad class.

Output guards (same change):
- Assistant offers ("If you want, I can help…") are dropped: 2 of 1,619 stored cues.
- Leaked `[n]` / `【n】` citations are stripped: 2 of 1,619.
- An overlong body ends at its last full sentence rather than clipping mid-sentence: 14 of
  1,619 clipped.

## Encyclopedia fact check (2026-10-18)

Wrong facts were the one class the value bar didn't move: about 11% of shown cues. On the 369
production cues of the four reviewed sessions, two Claude graders labelled 39 wrong, 274
correct, 18 unsure and 38 with no factual claim.

Design (`api/cue/factcheck.py`): each cue about to be shown is checked like this.
- The model names up to 3 encyclopedia articles that would state its facts.
- Kiwix returns each article's lead plus the paragraphs (and infobox cells) that contain the
  cue's own words.
- The model answers one question: does this text explicitly contradict a specific claim?
- "Not mentioned" and overlapping estimates are not contradictions, and the conflicting words
  (2 to 40 words) must actually appear in the text.
- Cues citing live news, or about recent events, are not checked: the mirror is older.
- It fails open.

| version | wrong dropped | correct dropped | note |
|---|---|---|---|
| first paragraph of a title search | 7 / 39 | 2 / 74 good | retrieval found the wrong article |
| + thinking on | 7 / 39 | 4 / 74 good | the model isn't the bottleneck |
| v2: model-proposed titles, keyword passages | 9 / 39 | 4 / 274 | dropped recent events and estimate nitpicks |
| **v3 (shipped): skip recent, allow overlaps, near-verbatim quote** | **10 / 39** | **2 / 274** | 1 "unsure" drop was right (40th, not 70th, anniversary) |

Findings:
- It drops the worst confabulations: an actor cast in a role he never played, a wrong Pokémon
  type, the wrong film cast, the wrong source work, a wrong team league.
- Compare the self-check (no reference), which lost 16 good cues to remove 2 wrong ones.
- Residual: about three quarters of wrong cues survive. Scripture details are the largest group,
  because Kiwix has no Bible text. Niche or edge-of-knowledge specifics are next.
- Latency: 1.2-3.3 s per checked cue when the cue server is free, and only shown cues are
  checked. The cue server serves one request at a time, so a check queued behind another cue
  call can take longer. Past `cue_fact_check_timeout_s` (6 s) the cue is shown unchecked.
- QA then found three reference-building bugs, all now fixed:
  - The paragraph pattern matched `<link>` in the page head, so the "lead" was CSS.
  - Navigation boxes and the references list out-ranked infobox rows and cast lines.
  - Every citation repeats the article title, which the production "title: body" note also
    carries.
- After Kiwix stopped crash-looping (XERK-2065, ArgoCD #762), its full-text search still took
  2-15 s on the NFS-backed ZIM. Articles are now read straight by the model's proposed titles,
  in parallel (0.3-0.9 s each).
- Final re-measure on a fresh labelling of the same 369 cues (41 wrong, 277 correct), checks
  run one at a time against live Kiwix and the real model:
  - 17 of 41 wrong cues dropped (was 10/39), including 2 scripture confabulations.
  - 2 of 277 correct cues dropped: "inflection point" (business vs maths sense, fixed by a
    prompt rule and verified live) and Jehoshaphat "alongside Ahab" (open).
  - Latency p50 2.9 s, p90 4.0 s; 5 of 358 checks went past 5.5 s and showed unchecked.
  - Run two checks at once and most time out, because the cue server serves one request at a
    time. Live, a check queued behind other cue traffic fails open.
- An early run took 11 s because the model pasted a whole paragraph as its "conflicting words".
  Capping the quote at 20 words (and the reply at 150 tokens) fixed both the latency and that
  weakness in the quote check.

## Claude Haiku vs Sonnet as the cue model (2026-10-29)

Question: is `claude-haiku-5-5` good enough for cues, or is `claude-sonnet-5-5` needed? The local
models were off, so both ran through the LiteLLM aliases `anthropic/claude-{haiku,sonnet}-5-5`.

Setup:
- Data: the four 2026-10-11..14 value-bar sessions (two whole, plus a 45 min and a 34 min slice;
  1,192 segments). Shipped v8 prompt.
- Replay: `replay.py --grounded --realtime --workers 4` (Anthropic has no shared GPU queue, so
  four conversations at once don't slow each other).
  - Each run had its own evidence cache, filled within the same hour.
  - Wikipedia rate-limited (429) part of the time. Retrieval then fell back to Kiwix, as
    production does.
- Claude rejects `temperature: 0.0` with a 400 error (only 1 is allowed). Every run sent
  `--extra '{"temperature": null, "chat_template_kwargs": null}'`.
- Haiku 5.5 reasons by default (about 160 reasoning tokens per call). Sonnet 5.5 doesn't. Each
  model ran at its default and at `reasoning_effort=low`.
- Grading: `blind_judge.py`, 4 packs, Claude subagent reviewers. The rubric added a
  GOOD/MARGINAL/BAD value grade, as in the value-bar round.
  - The reviewers are the same model family as the candidates. Treat the gap between Haiku and
    Sonnet as the signal, not the absolute wrong-rates.
- Anchor: production Qwen3.8-27B cues from the same windows. They were made under the older v5
  prompt, so this is context, not a like-for-like Qwen comparison.

| run | call mean / p50 / p90 | cues | wrong | GOOD | MARGINAL | BAD | perfect |
|---|---|---|---|---|---|---|---|
| **Sonnet, default (no thinking)** | 1.9 / 1.4 / 3.8 s | 66 | 0 | **40** | 26 | **0** | **46** |
| Sonnet, effort low | 1.4 / 1.3 / 1.9 s | 27 | 1 | 13 | 12 | 2 | 15 |
| Haiku, default (thinks) | 2.0 / 1.7 / 3.6 s | 68 | 1 | 23 | 35 | 10 | 27 |
| Haiku, effort low | 2.1 / 1.4 / 4.0 s | 58 | 0 | 21 | 29 | 8 | 21 |
| anchor: production Qwen (v5, cap 512) | ~4.5 s | 102 | 11 | 15 | 49 | 38 | 17 |

Findings:
- Speed doesn't separate them. Every Claude config is about 2x faster than Qwen's 4.5 s mean,
  and Sonnet at its default is no slower than Haiku.
- Accuracy doesn't separate them either: both are about 0-1.5% wrong.
- Value does separate them. Sonnet's default gave about 1.7x Haiku's GOOD cues, with no BAD
  cues against Haiku's 8-10. The gap is far beyond the ±3 batch noise.
- Haiku's BAD classes are the ones the v8 prompt targets, which Sonnet follows and Haiku
  doesn't:
  - Defining what the speakers obviously know (a savings-account type, a game mechanic, the
    company whose app they are using).
  - Off-topic trivia (founders, team histories).
  - Confident tangents built on misheard STT.
  - A suicide-crisis-line cue fired on a joking remark, in both Haiku runs. This is the most
    user-visible failure.
- Sonnet at effort low is too quiet: 27 cues from 1,177 attempts, declining almost every window.
- Remaining unverifiable claims in every run are post-cutoff events (the 2026 NBA Finals) or
  live figures (fund AUM). The fact check doesn't cover recent events.

Decision input:
- Haiku is accurate and fast enough, but it surfaces noticeably less value and more noise. Use
  it only if cost forces it, and expect a Haiku-specific prompt pass (definitions, trivia,
  crisis-line trigger).
- Sonnet at its default is the clear quality pick.
- Either one needs a code change first: the cue request must stop sending `temperature: 0.0`
  and `chat_template_kwargs` to Claude models, or they 400. The self-check and the fact-check
  requests also send `temperature: 0.0`.
- Not measured:
  - The fact check with Claude as its model.
  - Translations.
  - Cost per session. Replay doesn't record token usage, so use the LiteLLM spend logs.
  - A second run of each config. The Haiku/Sonnet gap is large enough that a rerun is unlikely
    to flip it.
