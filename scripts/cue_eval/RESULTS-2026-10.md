# Cue + STT review — October 2026 (first session on the INT4 cue server)

Hand review of the 2026-10-06 recording (27 min, glasses mic, 390 turns, 26 production cues) on
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
