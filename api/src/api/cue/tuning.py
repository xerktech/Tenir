"""Fixed cue-generation tuning (XERK-114, XERK-118, XERK-120, XERK-124).

Cues used to be governed by a per-session aggressiveness *level* the user picked
in the client UI (conservative | balanced | aggressive, XERK-81). That toggle is
gone (XERK-114): cues run at a single, fixed setting for everyone. This module is
the one place that setting lives, so the api backend and the session rate-limiter
agree.

XERK-124 settles what a cue *is*, per the reporter's own definition — three
triggers, one per thing a conversation can bring:

  * a question comes through   → **answer** it
  * a fact/topic comes through → **add extra context** to it
  * a falsehood comes through  → **correct** it

The calibration underneath swung twice before landing here. XERK-114 ran "when
in doubt, emit", which on a replayed real session produced confidently *wrong*
cues. XERK-118 swung to accuracy-only assertions, which measured **zero cues on
entire real sessions** — including one made purely of direct questions to the
glasses, since its triggers only fired when somebody asserted something. The
current setting keeps XERK-118's accuracy rule absolute over the *content* (never
state what you are unsure of; prefer the modest accurate statement over a
specific guess) while restoring an emissive posture over the *triggers*.

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
  * ``CUE_GUIDANCE`` — the bar with no evidence in the prompt: answer,
    contextualize, and correct from the model's own knowledge, with accuracy
    outranking coverage.
  * ``CUE_GUIDANCE_GROUNDED`` — the bar when evidence rides the prompt: the same
    three triggers, generous for evidence-covered facts, time-sensitive facts
    only from evidence.
"""

from __future__ import annotations

# Minimum gap between emitted cues. A floor that stops a run of hits from stacking;
# the accuracy bar in CUE_GUIDANCE is what actually governs how often cues appear.
MIN_INTERVAL_MS = 1500

# The instruction handed to the chat model describing the bar for emitting a cue.
#
# XERK-124 settled the emission model as three triggers, one per thing a
# conversation can bring (per the reporter's own definition of the product):
#
#   a question comes through   → answer it
#   a fact/topic comes through → add extra context to it
#   a falsehood comes through  → correct it
#
# History of the calibration: XERK-114 ran "when in doubt, emit", which on a
# replayed real session produced confidently WRONG cues; XERK-118 swung to
# accuracy-only, which measured 0 cues on whole real sessions — including ones
# made entirely of direct questions to the glasses ("How many rings does Saturn
# have?"), since its triggers only fired on *assertions*. The setting below
# keeps XERK-118's accuracy rule absolute over the *content* (never state what
# you are unsure of) while restoring an emissive posture over the *triggers*:
# answer, contextualize, correct — and when the specific detail is uncertain,
# say the accurate modest thing rather than either guessing or going mute.
CUE_GUIDANCE = (
    "A cue must be accurate — never guess — but within what you know, be "
    "forthcoming. Three triggers, one per thing the conversation brings: "
    "(1) Someone asks a factual question aloud — how many, how far, who, when, "
    "what — answer it. (2) A fact, name, place, number, date, or topic is "
    "mentioned — add a short piece of useful extra context about it: a stable "
    "fact about it you are certain of (what it is, where it is, when it "
    "happened, how big it is, what it is known for). When something concrete "
    "has been mentioned and you know such a fact, prefer emitting it over "
    "staying silent. (3) Someone states something false or mistaken — correct "
    "it with the right fact. Stay silent only for pure filler or small talk "
    "with nothing to look up, or when anything you could say might be "
    "inaccurate. Accuracy outranks coverage: say only what you are sure of, "
    "and when you are sure of something modest but not the specifics, surface "
    "the modest accurate thing instead of guessing at specifics. Facts that "
    "grow over time are NOT safe from memory even when they feel settled — "
    "how many entries an ongoing film series, product line, or franchise has, "
    "its latest release, current versions, prices, scores, officeholders: "
    "your knowledge of these ends at your training cutoff and the true answer "
    "may have moved, so stay silent on them rather than answer from memory. A "
    "wrong cue is worse than no cue."
)

# The bar when live evidence rides the prompt (XERK-120). One-sided generosity:
# an evidence-covered fact is safe to surface freely (its accuracy comes from
# the retrieved source, and it carries that source's label to the listener), so
# don't hold it to the guessing-era bar — but anything the evidence does NOT
# cover keeps the tight memory bar above, verbatim in spirit: silence over a
# guess. Chosen empirically (docs/cue-rag.md): a symmetric loosening made the
# model answer uncovered questions by citing unrelated evidence; this wording
# emitted on covered facts and stayed silent on every uncovered trap.
CUE_GUIDANCE_GROUNDED = (
    "Live evidence accompanies this conversation, so be generous with it. "
    "Three triggers, one per thing the conversation brings: (1) Someone asks "
    "a factual question aloud — answer it, from the evidence when the evidence "
    "covers it, or from your own knowledge if it is a stable, timeless fact "
    "you are certain of. (2) A fact, name, place, number, date, or topic is "
    "mentioned — add a short piece of genuinely useful extra context about "
    "it; for evidence-covered facts, prefer emitting a cue over staying "
    "silent. (3) Someone states something false or mistaken — correct it with "
    "the right fact. Accuracy is still absolute, and the generosity is "
    "one-sided: a fact that changes or grows over time — recent events, "
    "current officeholders, prices, scores, and how many entries an ongoing "
    "film series, product line, or franchise has or what its latest release "
    "is — may come ONLY from the evidence; your memory of such facts ends at "
    "your training cutoff and the true answer may have moved. If the "
    "evidence does not cover it, stay silent rather than answer from memory, "
    "and never cite evidence that does not actually support the fact. A "
    "stable, timeless fact may come from your own knowledge only if you are "
    "certain it is correct; when you are sure of something modest but not "
    "the specifics, surface the modest accurate thing instead of guessing. A "
    "wrong cue is worse than no cue."
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
