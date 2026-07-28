"""Fixed cue-generation tuning (XERK-114, XERK-118, XERK-120, XERK-124).

Cues used to be governed by a per-session aggressiveness *level* the user picked
in the client UI (conservative | balanced | aggressive, XERK-81). That toggle is
gone (XERK-114): cues run at a single, fixed setting for everyone. This module is
the one place that setting lives, so the api backend and the session rate-limiter
agree.

XERK-124 settled what a cue *is* — a question is answered, a mentioned fact
gets extra context, a falsehood gets corrected. The enrichment calibration
after it (measured by replaying recorded deployment conversations through the
production model; harness in ``scripts/cue_eval/``) kept those triggers, added
two more (define terms/jargon; inform a decision being worked through), and
made *enrichment* the defining property: a cue must add something that was NOT
said aloud — restating the transcript is banned outright, since replayed
production cues showed ~13% pure restatements.

The calibration underneath swung twice before landing here. XERK-114 ran "when
in doubt, emit", which on a replayed real session produced confidently *wrong*
cues. XERK-118 swung to accuracy-only assertions, which measured **zero cues on
entire real sessions** — including one made purely of direct questions to the
glasses, since its triggers only fired when somebody asserted something. The
current setting keeps XERK-118's accuracy rule absolute over the *content* (never
state what you are unsure of; prefer the modest accurate statement over a
specific guess) while keeping an emissive posture over the *triggers*.

XERK-120 keeps the bar **asymmetric** on top of that. With live-source evidence
in the prompt, an evidence-backed cue's accuracy comes from retrieval, not model
confidence — so evidence-covered facts emit freely, while time-sensitive facts
the evidence does not cover stay silent rather than answered from stale weights.
Measured traps: a naively symmetric loosening answered an uncovered question by
miscite-ing unrelated evidence; the one-sided wording passed. The grounded bar is
used ONLY when evidence actually arrived (the payload builder picks per call), so
a retrieval outage degrades to the memory bar, never to aggressive guessing.

Three things are tuned:
  * ``MIN_INTERVAL_MS`` — the minimum gap the session waits between emitted cues.
    A floor, not a target: it keeps a burst of hits from stacking on top of each
    other. (Not the frequency lever: one cue attempt costs ~2-2.5s of retrieval +
    model call serialized one-in-flight, and the clients show one cue per ~10s
    band slot — the emission bar governs frequency.)
  * ``CUE_GUIDANCE`` — the source-of-truth bar with no evidence in the prompt:
    time-changing facts are unsafe from memory, so stay silent on them.
  * ``CUE_GUIDANCE_GROUNDED`` — the bar when evidence rides the prompt:
    generous for evidence-covered facts, time-sensitive facts only from
    evidence.

The rest of the prompt — the enrichment framing, the five triggers, the
no-restatement/no-speculation rules, and the worked examples — is shared by
both bars and lives in ``openai.py``'s ``_SYSTEM``.
"""

from __future__ import annotations

# Minimum gap between emitted cues. A floor that stops a run of hits from stacking;
# the accuracy bar in CUE_GUIDANCE is what actually governs how often cues appear.
MIN_INTERVAL_MS = 1500

# The source-of-truth bar slotted into the system prompt's accuracy rules
# (openai.py `_SYSTEM`). Since the enrichment calibration the shared frame —
# role, the five triggers (answer / entity context / define terms / inform
# decisions / correct), the no-restatement rule, candidate discipline, and the
# worked examples — lives in `_SYSTEM`; the two strings below carry only what
# differs between an ungrounded and an evidence-grounded call: where a fact is
# allowed to come from.
#
# History of the calibration: XERK-114 ran "when in doubt, emit", which on a
# replayed real session produced confidently WRONG cues; XERK-118 swung to
# accuracy-only, which measured 0 cues on whole real sessions; XERK-124
# restored an emissive posture over three triggers (answer, contextualize,
# correct). The enrichment round after that replayed 12 recorded deployment
# conversations (566 attempts per variant) against the production model and
# found the XERK-124 wording still near-mute without evidence (8 cues, 1.4% of
# attempts) while production cues showed ~13% restatements, ~13% rephrased
# duplicates, and ~10% wrong cues (invented facts about misheard names, or
# contradictions of what the speaker just said). The replacement — enrichment
# framing, five triggers, no-restatement + no-speculation + garbled-name +
# firsthand-details + world-moved rules, candidate discipline, worked examples
# — measured 39 cues on the same replay (5x) with judged novelty 1.85/2,
# relevance 2.0/2, accuracy 1.79/2 and zero pure restatements, versus novelty
# 1.49 / accuracy 1.74 and 8 restatements + 8 duplicates for the production
# baseline. See scripts/cue_eval/ for the harness that produced those numbers.
CUE_GUIDANCE = (
    "Facts that change or grow over time — recent events, latest releases, "
    "current versions, prices, scores, officeholders, how many entries an "
    "ongoing film series, product line, or franchise has — are NOT safe from "
    "memory even when they feel settled: your knowledge of them ends at your "
    "training cutoff and the true answer may have moved, so stay silent on "
    "them rather than answer from memory."
)

# The bar when live evidence rides the prompt (XERK-120). One-sided generosity:
# an evidence-covered fact is safe to surface freely (its accuracy comes from
# the retrieved source, and it carries that source's label to the listener) —
# but anything the evidence does NOT cover keeps the tight memory bar above:
# silence over a guess. Chosen empirically (docs/cue-rag.md): a symmetric
# loosening made the model answer uncovered questions by citing unrelated
# evidence; the one-sided wording emitted on covered facts and stayed silent on
# every uncovered trap.
CUE_GUIDANCE_GROUNDED = (
    "Live evidence accompanies this conversation — be generous with it: for "
    "facts the evidence covers, prefer emitting a cue over staying silent. "
    "Generosity applies only to topics that are cue-worthy in the first "
    "place: evidence exists for every mundane noun (a dessert, a pocket, a "
    "playground game) and for every term of the speakers' own trade, and "
    "covering one with a citation does not make it worth the listener's "
    "attention — the everyday-knowledge, known-to-these-listeners, and "
    "register rules above outrank evidence coverage. "
    "Facts that change or grow over time — recent events, latest releases, "
    "current versions, prices, scores, officeholders, how many entries an "
    "ongoing film series, product line, or franchise has — may come ONLY from "
    "the evidence, never from memory: your knowledge of them ends at your "
    "training cutoff and the true answer may have moved. If the evidence does "
    "not cover them, stay silent rather than answer from memory, and never "
    "cite evidence that does not actually support the fact. A stable, "
    "timeless fact may come from your own knowledge only if you are certain "
    "it is correct. Accuracy stays absolute either way."
)


def min_interval_ms() -> int:
    return MIN_INTERVAL_MS


def cue_guidance(*, grounded: bool = False) -> str:
    """The emission bar for the chat prompt: the tight memory bar by default,
    the evidence-gated generous bar when the caller actually has evidence to
    put in the prompt (XERK-120). Callers must pass ``grounded=True`` only when
    evidence is present — that conditionality is what keeps a retrieval outage
    degrading to the tight bar instead of to aggressive guessing."""
    return CUE_GUIDANCE_GROUNDED if grounded else CUE_GUIDANCE
