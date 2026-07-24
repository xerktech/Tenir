"""Model-free cue generator for CI/dev (XERK-81).

Deterministic so tests and the model-free single-host stack exercise the whole
cue path — session pump → WS message → persistence → history — without a GPU.
The trigger rule is intentionally crude but level-aware, so switching the UI
toggle visibly changes how often cues fire even against the stub.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from api.contract import CueLevel
from api.cue.base import CueGenerator, GeneratedCue, normalize_cue_title

# Skip these when picking a 1-3 word title from the trigger line.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "how",
    "what",
    "who",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "to",
    "of",
    "in",
    "on",
    "for",
    "and",
    "my",
    "your",
    "far",
    "away",
    "me",
    "i",
    "it",
    "that",
    "this",
}


def _last_line(transcript: str) -> str:
    for line in reversed(transcript.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _title_from(line: str) -> str:
    words = [w for w in re.findall(r"[A-Za-z0-9#]+", line)]
    picked = [w for w in words if w.lower() not in _STOPWORDS] or words
    title = " ".join(picked[:3]).strip()
    return title[:1].upper() + title[1:] if title else "Context"


class StubCueGenerator(CueGenerator):
    def generate(
        self,
        transcript: str,
        *,
        level: CueLevel,
        avoid_titles: Sequence[str] = (),
    ) -> GeneratedCue | None:
        last = _last_line(transcript)
        if not last:
            return None
        has_question = "?" in last
        has_number = any(ch.isdigit() for ch in last)
        if level == CueLevel.conservative:
            trigger = has_question and has_number
        elif level == CueLevel.aggressive:
            trigger = len(last.split()) >= 2
        else:  # balanced
            trigger = has_question or has_number
        if not trigger:
            return None
        title = _title_from(last)
        # Already surfaced this cue earlier in the conversation? Don't repeat it
        # (XERK-102) — the deterministic stub has no other line to draw from, so
        # returning None lets a later, different turn produce the next cue.
        if normalize_cue_title(title) in {normalize_cue_title(t) for t in avoid_titles}:
            return None
        return GeneratedCue(title=title, body=f"Context for “{last}”.")
