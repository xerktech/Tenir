"""Fixed cue-generation tuning (XERK-114, XERK-118, XERK-120).

Cues used to be governed by a per-session aggressiveness *level* the user picked
in the client UI (conservative | balanced | aggressive, XERK-81). That toggle is
gone (XERK-114): cues run at a single, fixed setting for everyone. This module is
the one place that setting lives, so the api backend and the session rate-limiter
agree.

XERK-118 refocuses that setting on **accuracy**. The point of a cue is to give the
listener information that is correct and worth trusting; padding the transcript
with vague or possibly-wrong "context" is worse than staying silent. So the bar is
now confidence, not volume: surface a fact only when it is specific and the model
is sure it is right, and prefer *correcting* a clear error in the conversation over
adding tangential trivia. When unsure, emit nothing.

XERK-120 makes the bar **asymmetric**. The tight bar was calibrated for a model
that could only guess from stale weights; with live-source evidence in the prompt,
an evidence-backed cue's accuracy comes from retrieval, not model confidence — so
holding those cues to the guessing-era bar just leaves correct, attributable cues
on the table. Measured against the deployed model, the tight bar stayed silent even
when the evidence fully covered the fact, while a naively loosened bar answered an
uncovered question by miscite-ing unrelated evidence. The setting that passed both
traps is one-sided generosity: emit freely where the evidence supports the fact,
keep the tight memory bar everywhere else. The grounded bar is used ONLY when
evidence actually arrived (the payload builder picks per call), so a retrieval
outage degrades to the tight bar, never to aggressive guessing.

Three things are tuned:
  * ``MIN_INTERVAL_MS`` — the minimum gap the session waits between emitted cues.
    A floor, not a target: with the accuracy bar doing the gating, cues emit only
    when there's something correct to say, so this just keeps a burst of hits from
    stacking on top of each other. (Not the frequency lever: one cue attempt costs
    ~2-2.5s of retrieval + model call serialized one-in-flight, and the clients
    show one cue per ~10s band slot — the emission bar governs frequency.)
  * ``CUE_GUIDANCE`` — the bar with no evidence in the prompt: accurate,
    verifiable facts and corrections only, silence when unsure.
  * ``CUE_GUIDANCE_GROUNDED`` — the bar when evidence rides the prompt: generous
    emission for evidence-covered facts, the same tight bar for everything the
    evidence does not cover.
"""

from __future__ import annotations

# Minimum gap between emitted cues. A floor that stops a run of hits from stacking;
# the accuracy bar in CUE_GUIDANCE is what actually governs how often cues appear.
MIN_INTERVAL_MS = 1500

# The instruction handed to the chat model describing the bar for emitting a cue.
# XERK-118: accuracy is the whole job. Only surface a fact the model is confident is
# correct and verifiable; prefer correcting a clear error in the conversation; when
# unsure whether something is right, stay silent rather than guess.
#
# XERK-124 added (2), the spoken question. The original two triggers — correct an
# error, annotate a claim — both presuppose someone *asserted* something, and a
# recorded real session showed the gap: a user spent a whole conversation asking
# their glasses direct factual questions ("How many Toy Story movies are there?",
# "How many rings does Saturn have?") and the model, correctly following its
# instructions, answered {"cue": false} on every turn — a question asserts
# nothing, so neither trigger fired. Asking aloud is the closest thing the
# product has to an explicit request for a cue, so it now ranks as the first
# trigger; the accuracy bar (certain, or silent) is unchanged.
CUE_GUIDANCE = (
    "Your only job is accuracy: a cue must be correct, specific, and worth "
    "trusting. Three things are cue-worthy. (1) A clear factual error in the "
    "conversation — someone states something false or mistaken; surface the "
    "correct fact so the listener isn't misled. (2) A direct factual question "
    "someone asks aloud — how many, how far, who, when, what — surface the "
    "answer; a spoken question is the strongest signal a cue is wanted, so "
    "prefer answering it over staying silent whenever you are certain of the "
    "answer. (3) A concrete fact you are confident is true and that adds real "
    "information about a name, place, number, date, or claim just mentioned. "
    "Do NOT pad with vague, generic, or tangential context, and do NOT surface "
    "anything you are unsure is accurate: when in doubt, stay silent. A wrong "
    "or hand-wavy cue is worse than no cue, so prefer silence over a guess."
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
    "Live evidence accompanies this conversation, so be generous with it: "
    "whenever a name, place, number, date, or claim comes up that the evidence "
    "covers, surface the fact — for evidence-covered facts, prefer emitting a "
    "cue over staying silent. A direct factual question someone asks aloud is "
    "the strongest signal a cue is wanted: answer it from the evidence when the "
    "evidence covers it, or from your own knowledge if it is a stable, "
    "timeless fact you are certain of. Accuracy is still absolute, and the "
    "generosity is one-sided: a fact about recent events, current "
    "officeholders, prices, or scores may come ONLY from the evidence — if the "
    "evidence does not cover it, stay silent rather than answer from memory, "
    "and never cite evidence that does not actually support the fact. A "
    "stable, timeless fact may come from your own knowledge only if you are "
    "certain it is correct. A wrong or hand-wavy cue is still worse than no "
    "cue."
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
