"""Fixed cue-generation tuning (XERK-114, XERK-118).

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

Two things are tuned:
  * ``MIN_INTERVAL_MS`` — the minimum gap the session waits between emitted cues.
    A floor, not a target: with the accuracy bar doing the gating, cues emit only
    when there's something correct to say, so this just keeps a burst of hits from
    stacking on top of each other.
  * ``CUE_GUIDANCE`` — the instruction handed to the chat model describing the bar
    for emitting a cue: accurate, verifiable facts and corrections only.
"""

from __future__ import annotations

# Minimum gap between emitted cues. A floor that stops a run of hits from stacking;
# the accuracy bar in CUE_GUIDANCE is what actually governs how often cues appear.
MIN_INTERVAL_MS = 1500

# The instruction handed to the chat model describing the bar for emitting a cue.
# XERK-118: accuracy is the whole job. Only surface a fact the model is confident is
# correct and verifiable; prefer correcting a clear error in the conversation; when
# unsure whether something is right, stay silent rather than guess.
CUE_GUIDANCE = (
    "Your only job is accuracy: a cue must be correct, specific, and worth "
    "trusting. Two things are cue-worthy. (1) A clear factual error in the "
    "conversation — someone states something false or mistaken; surface the "
    "correct fact so the listener isn't misled. (2) A concrete fact you are "
    "confident is true and that adds real information about a name, place, "
    "number, date, or claim just mentioned. Do NOT pad with vague, generic, or "
    "tangential context, and do NOT surface anything you are unsure is accurate: "
    "when in doubt, stay silent. A wrong or hand-wavy cue is worse than no cue, so "
    "prefer silence over a guess."
)


def min_interval_ms() -> int:
    return MIN_INTERVAL_MS


def cue_guidance() -> str:
    return CUE_GUIDANCE
