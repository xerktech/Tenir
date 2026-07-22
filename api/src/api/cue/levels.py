"""Per-level tuning for cue aggressiveness (XERK-81).

The user picks the level in the client UI; it rides session.start and shapes two
things: how strict the generator prompt is (what counts as cue-worthy) and how
long the session waits between cues (so 'aggressive' can surface more of them).
Kept in one place so the api backend and the session rate-limiter agree.
"""

from __future__ import annotations

from api.contract import CueLevel

# Minimum gap between emitted cues, per level. Conservative spaces them out;
# aggressive lets them come thick and fast.
MIN_INTERVAL_MS: dict[CueLevel, int] = {
    CueLevel.conservative: 20000,
    CueLevel.balanced: 8000,
    CueLevel.aggressive: 3000,
}

# The instruction handed to the chat model describing the bar for emitting a cue.
LEVEL_GUIDANCE: dict[CueLevel, str] = {
    CueLevel.conservative: (
        "Only emit a cue for an unambiguous, verifiable factual reference or a "
        "direct factual question (a named entity, a number, a place, a date). "
        "When in doubt, emit nothing."
    ),
    CueLevel.balanced: (
        "Emit a cue when the conversation makes a clear factual reference or asks "
        "a question that a private fact would helpfully answer (e.g. 'how far is "
        "the sun', a mention of a specific Pokémon). Skip small talk and opinions."
    ),
    CueLevel.aggressive: (
        "Emit a cue whenever anything is even loosely lookup-worthy — a name, a "
        "topic, a claim worth context. Prefer surfacing context over staying silent."
    ),
}


def min_interval_ms(level: CueLevel) -> int:
    return MIN_INTERVAL_MS.get(level, MIN_INTERVAL_MS[CueLevel.balanced])


def level_guidance(level: CueLevel) -> str:
    return LEVEL_GUIDANCE.get(level, LEVEL_GUIDANCE[CueLevel.balanced])
