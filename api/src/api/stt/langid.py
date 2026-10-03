"""Text-based language identification fallback for finalized turns (XERK-160).

The live-translation trigger is ``CaptionFinal.lang`` — but the deployed
Parakeet server transcribes multilingual speech without *reporting* which
language it detected (the NeMo hypothesis exposes no language attribute, so
``language`` comes back null; recorded on session a6ef5cad: a fully-Spanish
conversation stored with every segment's lang NULL, so no translation ever
triggered). This module recovers the signal from the transcribed text itself,
which for a finalized turn is right there and unambiguous far more often than
not.

Deliberately conservative (word frequencies, via wordfreq, only back the
rewording check in ``is_english_word``): each contract language gets a
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

from wordfreq import zipf_frequency

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
    "i a o e ha ma per um em do com no in come as die hat war den son plus pour non ya yo".split()
)

# English vocab that is also an everyday word of another contract language, ASR-spelled
# (no accents): de "was"/"is"(ist)/"so"/"in", es "has", pt "to"(tô)/"for", it "be"/"so"/"in",
# fr "but"/"the"(thé). Found by QA of XERK-1423 with the real translation model.
_SHARED_EN = frozenset("was is so in has to for be but the".split())

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


# Word frequencies (wordfreq's "small" lists: every word down to ~1 per million) for words
# outside the tiny vocabularies above (XERK-1520). Zipf is log10 frequency per billion
# words, so a margin of 1.0 means ten times as frequent in English as in any other
# contract language: "seen" +1.6, "care" +1.5, while es "sin" is -1.9 and de "was" +0.3.
_OTHER_LANGS = ("es", "fr", "de", "pt", "it")
_EN_ONLY_MARGIN = 1.0


def _english_margin(word: str) -> float:
    """How much more frequent ``word`` is in English than in the most frequent other
    contract language, in Zipf units (log10)."""
    return zipf_frequency(word, "en", wordlist="small") - max(
        zipf_frequency(word, code, wordlist="small") for code in _OTHER_LANGS
    )


# Load the frequency lists now: the first lookup reads them from disk (~0.3 s), which
# must not stall the event loop mid-session.
_english_margin("the")


def _excluded(word: str) -> bool:
    """Whether a word is a homograph, a ``_SHARED_EN`` word or another language's vocab:
    a native word of another contract language however frequent it is in English."""
    if word in _ENGLISH_HOMOGRAPHS or word in _SHARED_EN:
        return True
    return any(word in vocab for code, vocab in _WORDS.items() if code != "en")


def is_english_word(word: str) -> bool:
    """Whether a lowercased token can only be English: English vocabulary, or a word
    ten times as frequent in English as in every other contract language, that is no
    word of another contract language (not a homograph, not in ``_SHARED_EN``, not in
    another language's vocabulary). "was" is German "what", so it is not one."""
    if _excluded(word):
        return False
    return word in _WORDS["en"] or _english_margin(word) >= _EN_ONLY_MARGIN


# English subject pronouns that are also an everyday word of another contract language:
# es "he" (I have). In "he don't care" it is English; the rewording check
# (``_same_text``) needs that to catch "he" -> "I". Deliberately not frequency-derived:
# that class holds de "will"/"also"/"kind" and fr "place", which code-switched turns
# translate for real (XERK-1520 QA).
_SHARED_EN_PRONOUNS = frozenset({"he"})


def is_shared_english_word(word: str) -> bool:
    """Whether a lowercased token is an English pronoun that is also a native word of
    another contract language: English beside English words, a translation otherwise."""
    return word in _SHARED_EN_PRONOUNS


def detect_lang(text: str) -> str | None:
    """Best-effort language of one finalized turn, as a contract code, or None.

    Conservative by design (see module docstring): returns a code only when one
    language clearly wins with enough evidence for the turn's length. ``en`` comes
    from the word lists alone; a non-English language from word frequencies when
    they clearly name one (``_frequency_lang``), else from the word lists.
    """
    scored = _scores(text)
    if scored is None:
        return None
    scores, words = scored
    by_words = _word_list_lang(text, scores, words)
    if by_words == "en":
        return by_words
    return _frequency_lang(scores, words, by_words) or by_words


def _word_list_lang(text: str, scores: dict[str, int], words: list[str]) -> str | None:
    """The distinctive-word decision: one language must clearly win on hits."""
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
    if best == "en" and best_score < _MIN_HITS and any(w in _SHARED_EN for w in words):
        # The one English hit is a shared word, as native as it is English: pt "to cansado",
        # de "is mir egal", fr "the vert" closed live runs as en (XERK-1516).
        return None
    return best


# A non-English call from word frequencies (XERK-1349): a unigram model over wordfreq's
# "small" lists. The word lists leave much real speech undecided or wrong because their
# few words are shared: "Después del accidente, trasladaron a Gibson a un hospital, ..."
# ties es with fr (un, and the homograph "a"), so a Spanish turn that starts a
# conversation was never translated (FLEURS es: 21% of finals None, 5% mislabelled).
# A word missing from a language's list scores the floor (below the lists' ~3.0 cutoff).
_FREQ_FLOOR = 1.5
# The winner's summed Zipf must beat every other language, English included, by this
# much: a likelihood ratio of 1000.
_FREQ_MARGIN = 3.0
# A turn the word lists left undecided is called only with a word-list hit or character
# of the winner's own and this many distinct words. Short English turns of names, places,
# food or filler win on frequency, often helped by one shared word: "Terre Haute." fr,
# "Um hum." pt, "Playa del Carmen." es, "Cul de sac." fr (XERK-1349 QA). Correcting a
# non-English word-list call (es tagged fr) needs neither.
_FREQ_MIN_DISTINCT = 4


def _frequency_lang(scores: dict[str, int], words: list[str], by_words: str | None) -> str | None:
    """The non-English language the turn's word frequencies clearly favour, or None.

    Never returns ``en``: closing a live run stays with the word lists' stricter bar,
    so this can only open or extend a run, or correct which language one is in. Any
    English evidence leaves the call to the word lists: an English vocab hit ("The pate
    de fois gras, sir.") or an English-only word ("Penne alla vodka, please.").

    Measured on Tatoeba (20k sentences per language), FLEURS test (sentences and their
    comma-split chunks) and 422k Cornell movie-dialog lines: no Tatoeba or FLEURS English
    sentence newly tagged foreign, ~3 movie lines (foreign names and phrases); Spanish
    right 65% -> 85% (Tatoeba), 75% -> 85% (FLEURS), wrong 2.4% -> 0.4% (FLEURS)."""
    if scores["en"] or any(is_english_word(w) for w in words):
        return None
    totals = {
        code: sum(max(zipf_frequency(w, code, wordlist="small"), _FREQ_FLOOR) for w in words)
        for code in _WORDS
    }
    (best, best_total), (_, second_total) = sorted(
        totals.items(), key=lambda kv: kv[1], reverse=True
    )[:2]
    if best == "en" or best_total - second_total < _FREQ_MARGIN:
        return None
    if by_words is None and (not scores[best] or len(set(words)) < _FREQ_MIN_DISTINCT):
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
