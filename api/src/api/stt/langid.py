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
    "en": frozenset(
        "the and is are was were of to in that it you they this with for not have "
        "has had but what there about just so would could should think know "
        "really because".split()
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


def detect_lang(text: str) -> str | None:
    """Best-effort language of one finalized turn, as a contract code, or None.

    Conservative by design (see module docstring): returns a code only when one
    language clearly wins with enough evidence for the turn's length.
    """
    if not text or not _latin(text):
        return None
    lowered = text.lower()
    words = _TOKEN_RE.findall(lowered)
    if not words:
        return None

    scores: dict[str, int] = {}
    for code, vocab in _WORDS.items():
        scores[code] = sum(1 for w in words if w in vocab)
    for code, chars in _CHARS.items():
        if any(c in lowered for c in chars):
            scores[code] += 2

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (best, best_score), (_, second_score) = ranked[0], ranked[1]
    if best_score == 0:
        return None
    if best_score - second_score < _MIN_MARGIN:
        return None
    if best_score < _MIN_HITS and len(words) > _SINGLE_HIT_MAX_WORDS:
        return None
    return best
