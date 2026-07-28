"""Cue generation seam (XERK-81).

A cue is a private contextual info card the api derives from the live
conversation — someone asks how far the sun is and the answer appears above the
transcript, private to the listener. Generation sits behind this narrow seam so
the model-backed backend and the model-free stub are interchangeable, exactly
like the STT ``Transcriber`` seam.

The generator is *pure* per call: given the recent transcript it returns a cue or
``None``. It knows nothing about the WebSocket, persistence, or rate-limiting —
the session owns those (so timing and delivery stay testable without a model).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from api.cue.retrieval.base import Evidence


@dataclass
class GeneratedCue:
    """A model's cue proposal, before the session assigns an id / timeline slot."""

    title: str
    body: str
    # Short label of the evidence source the cue's fact was grounded in
    # (XERK-120), e.g. "BBC News"; None when the cue rests on model knowledge.
    source: str | None = None


def normalize_cue_title(title: str) -> str:
    """Canonical form of a cue title for de-duplication (XERK-102).

    Casefolded, punctuation dropped, whitespace collapsed — so trivial variants
    of the same cue ("Sun", "sun.", " The  Sun ") collapse to one key and don't
    slip past the dedupe as "different" titles.
    """
    return " ".join(re.sub(r"[^\w\s]", " ", title).casefold().split())


# Function words carry no cue substance; dropping them keeps the similarity
# signal on the content words that actually distinguish one fact from another.
_SUBSTANCE_STOPWORDS = frozenset(
    "the a an of to and in is are was were on for with that this it as by at "
    "from or be not has have had can its their which when where how what who "
    "into more most also than then only about over under between each per "
    "such may might will would could should very much many some".split()
)
_SUBSTANCE_WORD = re.compile(r"[a-z0-9']+")


# Below this many content tokens a fingerprint is too small for its Jaccard
# similarity to mean anything (a handful of shared words swings it past any
# threshold), so the substance check only applies to fingerprints this big.
# Real model cues (median body ~134 chars) fingerprint well above it.
CUE_SUBSTANCE_MIN_TOKENS = 5


def cue_substance_tokens(title: str, body: str) -> frozenset[str]:
    """The content-word fingerprint of a cue, for substance-level de-duplication.

    Replayed production sessions showed the same fact re-surfacing under a fresh
    title and reworded body ("Charlotte Drone Facility" / "US Drone Production
    Scale" / "Charlotte Drone Production" — one fact, three cues), which the
    title-normalized dedupe cannot catch. Content words of title+body are the
    stable core such rewordings share. Numbers are kept whatever their length —
    a figure is usually the heart of the fact ("#133", "90 seconds").
    """
    words = _SUBSTANCE_WORD.findall(f"{title} {body}".casefold())
    return frozenset(
        w
        for w in words
        if (len(w) > 2 or w.isdigit()) and w not in _SUBSTANCE_STOPWORDS
    )


# Words that name a cue's ANGLE or a broad category rather than its subject —
# sharing one of these between two titles says nothing about the cues being
# about the same thing ("Release Management" / "RabbitMQ Management API";
# "Save Failure" / "Pipeline Failure"). Grown by simulating the subject gate
# over recorded production cues and hand-classifying every collision — first
# the 316-cue/8-session 2026-07-27 calibration, then the 396-cue/7-session
# 2026-07-28 one, which contributed the tokens from "platform" on. Includes
# the common nouns ("ring", "sky") and brand-family tokens ("galaxy",
# "amazon") whose collisions in that data were between genuinely distinct
# cues. Entries are in normalized (plural-stripped) form.
_SUBJECT_GENERIC = frozenset(
    "origin history meaning definition overview basic launch size scale level "
    "type cause rule mode series system device company feature process "
    "management length software user service failure name language computing "
    "link testing emulator ring sky galaxy amazon "
    "platform code number resolution trend environment kubernete development "
    "database test design ai eastern".split()
)
# Unlike the ASCII substance regex, subject tokens keep unicode word chars so
# an accented name ("Pokémon") stays one token instead of splitting into
# meaningless fragments that collide with everything.
_SUBJECT_WORD = re.compile(r"[\w']+", re.UNICODE)


def _normalize_subject_word(word: str) -> str:
    """Fold possessives and trivial plurals so "Instances"/"Instance's" match."""
    word = word.rstrip("'")
    if word.endswith("'s"):
        word = word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def cue_subject_tokens(title: str) -> frozenset[str]:
    """The distinctive subject words of a cue title, for same-subject suppression.

    Reviewed production sessions surfaced ten SAML cues, three Kibana cues,
    and near-identical pairs ("ADP Scale"/"ADP", "Spot Instances"/"Spot
    Instance termination", "AWS Cognito User Pools"/"...User Pool" at Jaccard
    0.348 vs the 0.35 threshold) seconds to minutes apart: same subject, fresh
    angle each time. Those pairs measure 0.04-0.33 substance Jaccard —
    overlapping the genuinely-distinct band — so no substance threshold can
    catch them; what the repeats share is the distinctive words of their
    TITLES. Bare digits are excluded (a "3" in two titles relates nothing)
    along with stopwords and the angle/category words above.
    """
    # Two-character tokens stay: they are acronyms and model names in this
    # domain ("A1", "S3", "CI"), and dropping them let a replayed "Go/No-Go"
    # title (all short tokens) slip past the gate entirely. Simulated over the
    # 316-cue calibration set, admitting them added exactly one drop — a true
    # duplicate ("A1 Weight" after "Antigravity A1") — and no false ones.
    return frozenset(
        w
        for w in (_normalize_subject_word(x) for x in _SUBJECT_WORD.findall(title.casefold()))
        if len(w) > 1
        and not w.isdigit()
        and w not in _SUBSTANCE_STOPWORDS
        and w not in _SUBJECT_GENERIC
    )


def cue_substance_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two cue fingerprints, in [0, 1].

    On the recorded production cues, near-verbatim rewords of the same fact
    score 0.57-0.87, retitled same-fact paraphrases 0.30-0.38, and the closest
    genuinely distinct pairs (different facts about the same entity) 0.26-0.27
    — the drop threshold callers apply sits above that last band (see
    session._CUE_SUBSTANCE_DUP_THRESHOLD).
    """
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class CueGenerator(Protocol):
    def generate(
        self,
        transcript: str,
        *,
        avoid_cues: Sequence[GeneratedCue] = (),
        evidence: Sequence[Evidence] = (),
    ) -> GeneratedCue | None:
        """Return a cue for the given recent transcript, or ``None`` for nothing
        cue-worthy. Synchronous (may block on model I/O); the session calls it off
        the event loop via ``asyncio.to_thread``.

        ``avoid_cues`` are cues already surfaced earlier in this conversation
        (XERK-102): the generator should not repeat them — neither their titles
        nor their substance in new words, which is why the full title+body rides
        along — so a genuinely new cue spawns instead of an old one popping up
        again later. The session also drops any repeat post-hoc as a backstop,
        but honoring the list here lets a model-backed generator find *fresh*
        context rather than returning the same proposal to be discarded.

        ``evidence`` is retrieved live-source context (XERK-120), freshest first.
        A generator that grounds its cue in an evidence item should set the
        returned cue's ``source`` to that item's label so the listener sees the
        attribution; with no evidence the generator falls back to model
        knowledge, exactly the pre-grounding behaviour."""
        ...
