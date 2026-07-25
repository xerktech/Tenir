"""Transcript → retrieval query, without a model call (XERK-120).

The cue path has a hard latency budget: the LLM call itself is ~1.1s, so the
query the retrieval tiers search with must come from cheap text heuristics,
not a second model round-trip. The heuristics lean on what cue-worthy turns
look like — mentions of names, places, numbers, claims — so the keywords are
the capitalized tokens, digits, and rare content words of the recent turns,
freshest turns weighted first.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# Words that carry no retrieval signal. Small on purpose: over-filtering hurts
# recall more than a stray stopword hurts precision (FTS just ignores misses).
_STOPWORDS = {
    "a", "about", "after", "all", "an", "and", "are", "as", "at", "be", "been",
    "but", "by", "can", "could", "did", "do", "does", "for", "from", "get",
    "got", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if",
    "in", "is", "it", "its", "just", "know", "like", "me", "my", "no", "not",
    "of", "on", "one", "or", "our", "out", "really", "said", "she", "so",
    "some", "than", "that", "the", "their", "them", "then", "there", "they",
    "this", "to", "up", "us", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "yeah", "yes", "you",
    "your",
}  # fmt: skip

_WORD_RE = re.compile(r"[A-Za-z][\w'-]*|\d[\w.,%-]*")

# How many keywords ride the search: enough to pin an entity + a claim, few
# enough that an OR query stays selective.
_MAX_KEYWORDS = 8
# Web search gets the raw tail of the conversation, capped so a rambling turn
# doesn't dilute the query.
_MAX_TEXT_CHARS = 200


@dataclass
class RetrievalQuery:
    """What the tiers search with: scored keywords (news FTS, Wikipedia) and the
    trailing conversation text (web search, which handles prose queries well)."""

    keywords: list[str] = field(default_factory=list)
    text: str = ""

    def __bool__(self) -> bool:
        return bool(self.keywords)

    def cache_key(self) -> str:
        return " ".join(k.casefold() for k in self.keywords)


def build_query(turns: Sequence[str], *, last_n: int = 3) -> RetrievalQuery:
    """Keywords + query text from the last ``last_n`` finalized turns.

    Scoring, per token: capitalized mid-sentence (proper-noun shaped) and
    numeric tokens score highest — cue triggers are entity and figure mentions —
    then any content word, with later turns outranking earlier ones so the
    query tracks where the conversation is *now*. STT transcripts aren't
    guaranteed casing, so content words alone still yield a usable query.
    """
    recent = [t for t in turns if t and t.strip()][-last_n:]
    scored: dict[str, float] = {}
    for turn_idx, turn in enumerate(recent):
        recency = turn_idx + 1  # later turns matter more
        for match in _WORD_RE.finditer(turn):
            # Trim trailing punctuation the number pattern drags in ("1990.").
            token = match.group(0).rstrip(".,%-")
            if not token:
                continue
            lowered = token.casefold()
            if lowered in _STOPWORDS or len(lowered) < 2:
                continue
            mid_sentence = match.start() > 0 and turn[match.start() - 1] not in ".!?\n"
            if token[0].isupper() and mid_sentence:
                score = 3.0  # proper-noun shaped
            elif token[0].isdigit():
                score = 2.5  # figures: dates, distances, prices
            elif len(lowered) >= 4:
                score = 1.0  # ordinary content word
            else:
                score = 0.5
            # Keywords are casefolded — every tier's search is case-insensitive —
            # and scores accumulate so repeated mentions rise.
            scored[lowered] = scored.get(lowered, 0.0) + score * recency

    if not scored:
        return RetrievalQuery()

    # Order-stable top-K: sort by score, tie-break on first-seen order (dict
    # preserves insertion order, so enumerate gives it to us).
    ranked = sorted(
        enumerate(scored.items()), key=lambda pair: (-pair[1][1], pair[0])
    )
    keywords = [word for _, (word, _) in ranked[:_MAX_KEYWORDS]]

    text = " ".join(" ".join(recent).split())
    if len(text) > _MAX_TEXT_CHARS:
        text = text[-_MAX_TEXT_CHARS:].lstrip()
    return RetrievalQuery(keywords=keywords, text=text)
