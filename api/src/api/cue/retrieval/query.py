"""Transcript → retrieval query, without a model call (XERK-120, XERK-124).

The cue path has a hard latency budget: the LLM call itself is ~1.1s, so the
query the retrieval tiers search with must come from cheap text heuristics,
not a second model round-trip. The heuristics lean on what cue-worthy turns
look like — mentions of names, places, numbers, claims — so the keywords are
the capitalized tokens, digits, and rare content words of the recent turns,
freshest turns weighted first.

XERK-124 recalibrated those heuristics against real STT output. As first
written they were tuned on clean written prose and collapsed on live speech:
a replay of a recorded session produced queries like ``also 10thinger means
anti-glare 60 24-bit`` and retrieved **nothing** on every single turn, so the
grounded emission bar never once engaged in production. Two causes, both fixed
here:

  * **Conversational filler outscored the topic.** The stopword list held only
    written-prose function words, so speech fillers ("actually", "gonna",
    "thing", "kind of", "I mean") sailed through as ordinary content words and,
    being frequent, accumulated more score than the entity being discussed.
  * **The topic aged out.** Scoring looked at the last 3 turns only, but a
    speaker introduces a subject once and then talks *about* it for minutes —
    by turn four the name that makes the query answerable is gone. Scoring now
    spans the same window the cue prompt sees, with recency still ranking, so a
    persistent entity survives while the query still tracks where the talk is.

Proper-noun *runs* ("Raspberry Pi", "New York") are the strongest retrieval
signal speech offers, so they score highest and are exempt from stopwording —
that exemption is what keeps the "New" in "New York".
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# Words that carry no retrieval signal.
#
# Two groups. The first is ordinary written-prose function words. The second is
# *speech* filler — the discourse markers, hedges, and all-purpose nouns that
# dominate a spoken transcript's token counts but never help a search engine
# ("actually", "gonna", "thing", "stuff", "guess"). Both must be here: a live
# transcript is mostly the second group, and because scores accumulate across
# the window, filler left in the list doesn't merely add noise, it outranks the
# subject. A capitalized run is exempt (see ``build_query``), so dropping common
# words here is still safe for names built from them.
_STOPWORDS = {
    # written-prose function words
    "a", "about", "after", "all", "an", "and", "are", "as", "at", "be", "been",
    "but", "by", "can", "could", "did", "do", "does", "for", "from", "get",
    "got", "had", "has", "have", "he", "her", "him", "his", "how", "i", "if",
    "in", "is", "it", "its", "just", "know", "like", "me", "my", "no", "not",
    "of", "on", "one", "or", "our", "out", "really", "said", "she", "so",
    "some", "than", "that", "the", "their", "them", "then", "there", "they",
    "this", "to", "up", "us", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "yeah", "yes", "you",
    "your",
    # speech filler: discourse markers and hedges
    "actually", "again", "almost", "already", "also", "always", "anyway",
    "basically", "because", "before", "being", "bit", "both", "come", "coming",
    "definitely", "even", "ever", "every", "gave", "give", "go", "goes",
    "going", "gonna", "gotta", "guess", "here", "honestly", "hope", "kind",
    "kinda", "least", "let", "lets", "literally", "little", "long", "look",
    "looked", "looking", "looks", "lot", "made", "make", "makes", "many",
    "maybe", "mean", "means", "might", "more", "most", "much", "must", "need",
    "needs", "never", "new", "next", "now", "obviously", "off", "oh", "okay",
    "only", "other", "over", "own", "pretty", "probably", "put", "quite",
    "right", "same", "saw", "say", "saying", "says", "see", "seen", "seriously",
    "should", "since", "sorta", "still", "sure", "take", "taken", "takes",
    "tell", "thanks", "thing", "things", "think", "thinking", "thought",
    "through", "time", "times", "today", "too", "took", "totally", "tried",
    "try", "trying", "turns", "under", "use", "used", "uses", "using", "very",
    "want", "wanted", "wants", "way", "well", "went", "while", "why", "work",
    "worked", "works", "wow",
    # speech filler: all-purpose pronouns/quantifiers and interjections
    "anyone", "anything", "dude", "everyone", "everything", "folks", "guys",
    "huh", "man", "nope", "nothing", "please", "someone", "something",
    "stuff", "thank", "uh", "um", "yep",
    # contractions: the tokenizer keeps the apostrophe, so these never collapse
    # onto the bare forms above and have to be listed in their own right.
    "ain't", "can't", "couldn't", "didn't", "doesn't", "don't", "hasn't",
    "haven't", "he's", "here's", "i'd", "i'll", "i'm", "i've", "isn't",
    "it's", "let's", "she's", "that's", "there's", "they're", "they've",
    "wasn't", "we're", "we've", "what's", "won't", "wouldn't", "you're",
    "you've",
}  # fmt: skip

_WORD_RE = re.compile(r"[A-Za-z][\w'-]*|\d[\w.,%-]*")

# How many keywords ride the search: enough to pin an entity + a claim, few
# enough that an OR query stays selective.
_MAX_KEYWORDS = 8
# Of those, how many may be bare figures — see the selection loop in
# ``build_query`` for why they are rationed rather than ranked normally.
_MAX_FIGURES = 2
# Web search gets the raw tail of the conversation, capped so a rambling turn
# doesn't dilute the query.
_MAX_TEXT_CHARS = 200
# How many finalized turns are scored. Matches the window the cue prompt itself
# sees (``cue_context_segments``): the retrieval query should be answerable for
# the same stretch of talk the model is asked to find a cue in. Scoring only the
# newest turns loses the subject, which a speaker names once and then discusses
# at length (XERK-124).
_DEFAULT_TURNS = 8


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


def _mid_sentence(turn: str, start: int) -> bool:
    """Is the token at ``start`` inside a sentence rather than opening one?

    Looks back past whitespace for the previous printing character. Checking
    only the immediately preceding character — as this did before XERK-124 —
    finds the *space* after "back. Also", never the period, so every
    sentence-opening word came out "proper-noun shaped" and scored above the
    real subject. On live speech, which is short sentences chained with "So",
    "Now", "Right", "Well", that single character was most of the reason the
    generated queries retrieved nothing.
    """
    i = start - 1
    while i >= 0 and turn[i].isspace():
        i -= 1
    return i >= 0 and turn[i] not in ".!?"


def _scan(turn: str) -> list[tuple[str, bool, bool]]:
    """Tokens of one turn as ``(token, is_capitalized_mid_sentence, in_run)``.

    ``in_run`` marks a token inside a run of two or more adjacent capitalized
    words — "Raspberry Pi", "New York", "Network Chuck". A run is the clearest
    entity signal an uncased-ish STT transcript offers, so it both scores
    highest and overrides stopwording; without that override the "New" in
    "New York" would be filtered as filler.
    """
    scanned: list[tuple[str, bool, bool]] = []
    for match in _WORD_RE.finditer(turn):
        # Trim trailing punctuation the number pattern drags in ("1990.").
        token = match.group(0).rstrip(".,%-")
        if not token:
            continue
        scanned.append((token, token[0].isupper() and _mid_sentence(turn, match.start()), False))

    # Second pass: a capitalized token adjacent to another one is part of a run.
    # Sentence-initial capitals are excluded above, so "The Eiffel Tower is..."
    # runs over "Eiffel Tower" and not over "The".
    for i, (token, capped, _) in enumerate(scanned):
        if not capped:
            continue
        prev_capped = i > 0 and scanned[i - 1][1]
        next_capped = i + 1 < len(scanned) and scanned[i + 1][1]
        if prev_capped or next_capped:
            scanned[i] = (token, capped, True)
    return scanned


def build_query(turns: Sequence[str], *, last_n: int = _DEFAULT_TURNS) -> RetrievalQuery:
    """Keywords + query text from the last ``last_n`` finalized turns.

    Scoring, per token: proper-noun runs score highest, then lone capitalized
    mid-sentence tokens and numerals — cue triggers are entity and figure
    mentions — then any content word, with later turns outranking earlier ones
    so the query tracks where the conversation is *now* without losing a
    subject named several turns back. STT transcripts aren't guaranteed casing,
    so content words alone still yield a usable query.
    """
    recent = [t for t in turns if t and t.strip()][-last_n:]
    scored: dict[str, float] = {}
    for turn_idx, turn in enumerate(recent):
        recency = turn_idx + 1  # later turns matter more
        for token, capped, in_run in _scan(turn):
            lowered = token.casefold()
            if len(lowered) < 2:
                continue
            # Run members bypass the stopword list — see ``_scan``.
            if lowered in _STOPWORDS and not in_run:
                continue
            if in_run:
                score = 4.0  # multi-word name: the strongest signal available
            elif capped:
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
    # Bare figures are weak retrieval keys on their own — it is the subject that
    # finds the page, and the figure is then checked against it. A spec-heavy
    # stretch of speech ("1200 by 1920, 224 ppi, 400 candelas") otherwise fills
    # every slot with numerals and evicts the one word that makes the query
    # answerable, which measurably returned nothing at all (XERK-124). Keep a
    # couple so dates and quantities still reach the news tier's FTS.
    keywords: list[str] = []
    figures = 0
    for _, (word, _) in ranked:
        if len(keywords) >= _MAX_KEYWORDS:
            break
        if word[0].isdigit():
            if figures >= _MAX_FIGURES:
                continue
            figures += 1
        keywords.append(word)

    text = " ".join(" ".join(recent).split())
    if len(text) > _MAX_TEXT_CHARS:
        # Cut at a word boundary: slicing blind left the web tier searching for
        # fragments like "trings attached, so I can say..." (XERK-124).
        text = text[-_MAX_TEXT_CHARS:]
        head, sep, tail = text.partition(" ")
        text = (tail if sep else head).lstrip()
    return RetrievalQuery(keywords=keywords, text=text)
