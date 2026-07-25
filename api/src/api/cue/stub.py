"""Model-free cue generator for CI/dev (XERK-81).

Deterministic so tests and the model-free single-host stack exercise the whole
cue path — session pump → WS message → persistence → history — without a GPU.
The trigger rule is intentionally crude but maximally aggressive (XERK-114): any
non-empty line is cue-worthy, so cues fire on essentially every turn even against
the stub.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

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
        avoid_titles: Sequence[str] = (),
    ) -> GeneratedCue | None:
        last = _last_line(transcript)
        if not last:
            return None
        # Maximally aggressive (XERK-114): any non-empty turn is cue-worthy. The
        # old per-level trigger is gone with the aggressiveness toggle.
        title = _title_from(last)
        # Already surfaced this cue earlier in the conversation? Don't repeat it
        # (XERK-102) — the deterministic stub has no other line to draw from, so
        # returning None lets a later, different turn produce the next cue.
        if normalize_cue_title(title) in {normalize_cue_title(t) for t in avoid_titles}:
            return None
        return GeneratedCue(title=title, body=f"Context for “{last}”.")
