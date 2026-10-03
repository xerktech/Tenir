"""Text-based language identification fallback for finalized turns (XERK-160).

The live-translation trigger is ``CaptionFinal.lang`` — but the deployed
Parakeet server transcribes multilingual speech without *reporting* which
language it detected (the NeMo hypothesis exposes no language attribute, so
``language`` comes back null; recorded on session a6ef5cad: a fully-Spanish
conversation stored with every segment's lang NULL, so no translation ever
triggered). This module recovers the signal from the transcribed text itself,
which for a finalized turn is right there and unambiguous far more often than
not.

Deliberately dependency-free and conservative: each contract language gets a
set of its most distinctive high-frequency words (function words that rarely
appear in the others) plus characters unique to its orthography. A turn is
labeled only when exactly one language clearly wins AND the evidence clears a
floor; anything ambiguous — proper-noun lists, interjections, other languages
entirely — returns ``None``, which downstream means "decide nothing": no
translation fires, and no run closes. A wrong "es" would translate English at
the listener; a wrong "en" would cut a live run short; ``None`` costs only a
missed turn.
"""

from __future__ import annotations

import re
import unicodedata

# Words that are simultaneously very frequent in their language and rare in the
# other five. Deliberately NOT exhaustive stopword lists: shared forms (es/pt
# "no", en/it "a", es/it "e"→no...) are excluded so a hit is real evidence.
# Diacritics count as written; the tokenizer keeps them.
_WORDS: dict[str, frozenset[str]] = {
    # NOTE: interjections that ride along in any language's casual speech
    # ("yeah", "okay") are deliberately absent — a bare "Yeah." mid-Spanish-run
    # must not read as an English turn (it would cut the live translation run
    # short; session a6ef5cad contains exactly that turn).
    # Contractions and short verbs are the bulk of casual English turns ("I did.",
    # "I don't remember.") and occur in no other contract language (XERK-1415).
    "en": frozenset(
        "the and is are was were of to in that it you they this with for not have "
        "has had but what there about just so would could should think know "
        "really because did can like get got be if out up how who some mean "
        "guess we my your i'm i'd i'll i've don't didn't can't it's that's what's "
        "there's we're they're you're gonna wanna".split()
    ),
    "es": frozenset(
        "el la los las es son está están y de del que un una uno en por para con "
        "no sí pero como más muy este esta esto ese esa eso porque cuando también "
        "hace tiene ser estar todo nada algo yo tú usted nosotros ya".split()
    ),
    "fr": frozenset(
        "le la les est sont et de du des que un une dans pour avec ne pas mais "
        "comme plus très ce cette c'est je tu vous nous ils elle il y a été être "
        "avoir tout rien quelque parce quand aussi oui non".split()
    ),
    "de": frozenset(
        "der die das ist sind und von zu den dem ein eine einen nicht aber wie "
        "mehr sehr dieser diese dieses weil wenn auch ja nein ich du sie wir ihr "
        "es war waren sein haben hat alles nichts etwas schon noch auf wurde "
        "werden wird kann muss gibt heute morgen jetzt hier ohne gegen zwischen "
        "immer".split()
    ),
    "pt": frozenset(
        "o os as é são e do da dos das que um uma em não mas como mais muito "
        "este esta isso esse essa porque quando também já faz tem ser estar tudo "
        "nada algo eu você nós vocês ele ela eles elas para com por".split()
    ),
    "it": frozenset(
        "il lo la i gli le è sono e di del della che un una uno in per con non "
        "ma come più molto questo questa quello quella perché quando anche già "
        "fa ha essere stare tutto niente qualcosa io tu lei noi voi loro sì".split()
    ),
}

# Everyday English words that are ALSO distinctive words of another contract
# language. Spoken by an English speaker they were evidence for the other
# language, so plain English turns got translated: across the recorded sessions
# "Um" and "Do" tagged pt, "I did." / "Ha ha" it, "No." es, "It's a very" fr
# (XERK-1415). They still count for that language, but only once the turn holds
# other evidence for it — "No me importa lo que pase" stays Spanish, "No way."
# and "I don't remember." stop being Spanish and Italian. A short turn of nothing
# but these is undecidable, and inside a live run it still inherits the run's
# language, so a Spanish "No." mid-conversation is translated as before.
_ENGLISH_HOMOGRAPHS = frozenset(
    "i a o e ha ma per um em do com no in come as die hat war den son plus pour non "
    "ya yo".split()
)

# Characters that pin a language on their own (strong evidence — they are typed
# by the STT model's own orthography, not by chance).
_CHARS: dict[str, str] = {
    "es": "ñ¿¡",
    "de": "ß",
    "pt": "ãõ",
}

# One distinctive-word hit is enough only when the turn is this short — "Los
# planetas." carries one hit in two words and is plainly Spanish; one hit in a
# twelve-word turn is noise.
_SINGLE_HIT_MAX_WORDS = 4
# A longer turn needs at least this many hits.
_MIN_HITS = 2
# ...and the winner must beat the runner-up by at least this margin, else the
# turn is genuinely mixed/ambiguous and labeling it would be a guess.
_MIN_MARGIN = 1

_TOKEN_RE = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?", re.UNICODE)


def _latin(text: str) -> bool:
    """True when the text's letters are (extended) Latin — the six contract
    languages all are; a Cyrillic/CJK/etc. turn can never be one of them."""
    for ch in text:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            if not name.startswith("LATIN"):
                return False
    return True


def _scores(text: str) -> tuple[dict[str, int], list[str]] | None:
    """Per-language evidence for one turn (distinctive-word hits, +2 for a unique
    character), with its words; None when the text can't be any contract language."""
    if not text or not _latin(text):
        return None
    lowered = text.lower()
    words = _TOKEN_RE.findall(lowered)
    if not words:
        return None
    scores: dict[str, int] = {}
    for code, vocab in _WORDS.items():
        scores[code] = sum(
            1 for w in words if w in vocab and (code == "en" or w not in _ENGLISH_HOMOGRAPHS)
        )
    for code, chars in _CHARS.items():
        if any(c in lowered for c in chars):
            scores[code] += 2
    for code, vocab in _WORDS.items():
        # English homographs only corroborate a language already evidenced.
        if code != "en" and scores[code]:
            scores[code] += sum(1 for w in words if w in vocab and w in _ENGLISH_HOMOGRAPHS)
    return scores, words


# One English "hit" is not evidence on its own: has/was/in/so are everyday words of the
# other contract languages too ("Has visto a Marco", "Was kostet das", "So bene").
_LEAN_MIN_EN_HITS = 2


def leans_english(text: str, versus: str) -> bool:
    """Whether an undetected turn is English enough to skip translating "from ``versus``"
    (the run's language).

    For the inherited turns of a run: ``detect_lang`` returned None (too short or too
    mixed to call), so the turn is translated as a continuation. A prompt that must name
    a source language would then tell the model this is ``versus``; on English text a
    translation model rewrites it instead of returning it (XERK-1354), so the caller
    skips those. Deliberately strict — a skipped real turn is lost, a translated English
    one is only reworded: at least two English hits, and more than ``versus`` has.
    """
    scored = _scores(text)
    if scored is None:
        return False
    scores, _ = scored
    return scores["en"] >= _LEAN_MIN_EN_HITS and scores["en"] > scores.get(versus, 0)


# English vocab that is also an everyday word of another contract language, ASR-spelled
# (no accents): de "was"/"is"(ist)/"so"/"in", es "has", pt "to"(tô)/"for", it "be"/"so"/"in",
# fr "but"/"the"(thé). Found by QA of XERK-1423 with the real translation model.
_SHARED_EN = frozenset("was is so in has to for be but the".split())


def is_english_word(word: str) -> bool:
    """Whether a lowercased token can only be English: English vocabulary that is no
    word of another contract language (not a homograph, not in ``_SHARED_EN``, not in
    another language's vocabulary). "was" is German "what", so it is not one."""
    if word not in _WORDS["en"] or word in _ENGLISH_HOMOGRAPHS or word in _SHARED_EN:
        return False
    return not any(word in vocab for code, vocab in _WORDS.items() if code != "en")


def detect_lang(text: str) -> str | None:
    """Best-effort language of one finalized turn, as a contract code, or None.

    Conservative by design (see module docstring): returns a code only when one
    language clearly wins with enough evidence for the turn's length.
    """
    scored = _scores(text)
    if scored is None:
        return None
    scores, words = scored

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, best_score), (_, second_score) = ranked[0], ranked[1]
    if best_score == 0:
        return None
    if best_score - second_score < _MIN_MARGIN:
        return None
    if best_score < _MIN_HITS and len(words) > _SINGLE_HIT_MAX_WORDS:
        return None
    if best == "en" and best_score < _MIN_HITS and _foreign_doubt(text, words):
        # One English word next to a homograph or an accented letter is a
        # code-switched turn ("Like, no sé.", "Ya, it's ok."), not an English one:
        # calling it en would close a live run mid-Spanish (XERK-1415 QA).
        return None
    return best


# Homographs that cast doubt on a one-word English call. "i" and "a" are left out:
# they are in nearly every English turn, and real it/fr text has other evidence.
_DOUBT_WORDS = _ENGLISH_HOMOGRAPHS - _WORDS["en"] - {"i", "a"}


def _foreign_doubt(text: str, words: list[str]) -> bool:
    """Whether a turn carries any non-English signal that isn't scored: an English
    homograph that isn't English vocab ("no", "ya") or a non-ASCII letter ("sé")."""
    if any(w in _DOUBT_WORDS for w in words):
        return True
    return any(ch.isalpha() and not ch.isascii() for ch in text)
