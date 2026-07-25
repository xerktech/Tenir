"""Fixed cue-generation tuning (XERK-114).

Cues used to be governed by a per-session aggressiveness *level* the user picked
in the client UI (conservative | balanced | aggressive, XERK-81). That toggle is
gone: cues now run at a single, fixed, maximally-aggressive setting for everyone,
tuned past the old "aggressive" level. This module is the one place that setting
lives, so the api backend and the session rate-limiter agree.

Two things are tuned:
  * ``MIN_INTERVAL_MS`` — the minimum gap the session waits between emitted cues.
    Half the old "aggressive" gap (3000ms), so cues come roughly twice as thick.
  * ``CUE_GUIDANCE`` — the instruction handed to the chat model describing the bar
    for emitting a cue. Set below the old "aggressive" bar: surface context for
    almost anything, staying silent only for pure conversational filler.
"""

from __future__ import annotations

# Minimum gap between emitted cues. Below the old "aggressive" 3000ms so cues come
# thick and fast for everyone.
MIN_INTERVAL_MS = 1500

# The instruction handed to the chat model describing the (very low) bar for
# emitting a cue. More aggressive than the old "aggressive" level: surface context
# for essentially any reference, and only stay silent for pure filler.
CUE_GUIDANCE = (
    "Surface a cue for essentially anything that carries a fact, name, place, "
    "number, date, topic, or claim — any reference a listener might want context "
    "on. Err heavily toward emitting: when in doubt, emit. Stay silent only for "
    "pure conversational filler (greetings, backchannel like 'yeah' or 'okay', "
    "and empty small talk with nothing to look up)."
)


def min_interval_ms() -> int:
    return MIN_INTERVAL_MS


def cue_guidance() -> str:
    return CUE_GUIDANCE
