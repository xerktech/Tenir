---
paths:
  - "api/src/api/stt/langid.py"
  - "api/tests/test_langid.py"
---

# Text language ID (translation trigger)

- `detect_lang` is the only source of segment `lang` in production (Parakeet reports none), and a
  non-`en` tag is what sends a turn to the translator. A false foreign tag translates English.
- Never put an everyday English word in a non-English vocab as plain evidence. Words that are
  both (`i`, `a`, `um`, `do`, `no`, `ha`, `in`, `yo`, …) go in `_ENGLISH_HOMOGRAPHS`, which only
  corroborates a language the turn already evidences. Before XERK-1415 these alone tagged ~1,000
  of ~11.4k recorded turns non-English ("Um" → pt, "I did." → it, "No." → es, "It's a" → fr).
- A short turn left `None` is cheap: outside a run nothing is translated, inside one it inherits
  the run's language. A wrong `en` cuts a live run, so new English vocab must be English-only
  (contractions; not `me`/`he`/`on`/`will`, which are es/fr/de words).
- A one-hit `en` call beside a homograph or an accented letter returns `None` (`_foreign_doubt`):
  code-switched turns like "Like, no sé." otherwise close a live Spanish run.
  `i`/`a` don't count as doubt, or "I did." stops being English.
- Don't make `leans_english` skip on one English hit + no foreign hit (tried, XERK-1423 QA):
  the non-English vocabs are tiny, so real turns score 0 and get dropped silently — pt `to`/`for`
  ("se for preciso…"), de `is`, it `be`, and "the" in titles ("me encanta the weeknd").
  English rewordings of inherited turns are dropped after the call instead (`_same_text`).
- `_same_text` (session.py) can't use word-overlap ratio alone: name-heavy real translations
  ("…, Pedro, sin Ana" → "…, without Ana") score as high as English rewordings. It drops a
  close match only when every replaced source word is English (rules in its docstring).
- `is_english_word` = tiny vocab OR ≥10× (Zipf +1.0) as frequent in English as in every other
  contract language (wordfreq "small" lists). A bare allow-list fails both ways: was/a/i/but
  dropped real translations, while "seen"/"care" weren't on it (XERK-1520).
- wordfreq has no ASR spellings of accented words (pt "tô" → "to", fr "thé" → "the"), so
  `_SHARED_EN` stays an explicit exclusion; don't drop it for the frequency check.
- Only es "he" may count as an English shared word in a rewording (≥2 English-only words kept). A frequency-derived "shared" class (margin ≥0) also holds de
  `will`/`also`/`kind`, fr `place`: real code-switched turns ("Marco will my new car") dropped.
- `_same_text` must count deleted source words as changed: SequenceMatcher encodes a reorder
  as delete+insert ("Ana incluida" → "including Ana"). Only a pure deletion skips the check;
  "output words ⊆ source words" drops "she came gestern too" → "… yesterday too".
- Mid-turn capitals aren't reliably names: German nouns ("Hund") and words after `.!?` aren't.
  A name is skipped only where the model's span is shorter (it dropped the name).
- A one-hit `en` call whose hit is a `_SHARED_EN` word returns `None` ("to cansado", "the vert"
  closed live runs, XERK-1516). Gate the decision, not `_scores`: dropping shared words from the
  score lost ~13% of English and let "The Las Vegas trip." win es. Costs ~3.5% of short English.
- `is_english_word` excludes English vocab that is also a native word (`_SHARED_EN`: de `was`,
  pt `to`/`for`, fr `but`/`the`, …): "…, Pedro, was?" → "…, what?" is a real translation.
- Non-English calls come from word frequencies (`_frequency_lang`, XERK-1349) when they beat
  every other language by 3 Zipf; the word lists' few shared words tied es/fr ("a", "un")
  and left ~1/3 of Spanish None. It never returns `en` (closing a run stays with the lists).
- `_frequency_lang` must stay out of any turn with English evidence (an English vocab hit or
  an `is_english_word`). On a turn the lists left None it needs the winner's own list hit
  AND ≥4 distinct words: short English names/places/food/filler win on frequency, and one
  shared word ("del", "de") is not real evidence ("Playa del Carmen." es, "Cul de sac." fr).
- Correcting a non-English list call (es tagged fr) needs neither; that is where most of the
  short-turn gain is. Loosening the None-turn gate re-opens runs on English (two QA FAILs).
- Written corpora (Tatoeba, FLEURS) barely reach that path: written English almost always has
  list hits. Check English false positives on spoken lines (Cornell movie dialogs, 422k) and
  hand-written place/food/name-list probes; Spanish recall on Tatoeba + comma-split FLEURS.
- Measure vocab changes against recorded segments (export `segments.text`, run old vs new
  `detect_lang`, hand-read every changed label), not intuition. Keep transcript text out of tests.
- A word everyday in two Romance languages goes in both vocabs, never one (XERK-1419): es "le"/"a"
  scored only for fr tipped real Spanish to fr. (pt "está" was tried: "Está bien." became None.)
- Spanish loanwords in English (hay, hola, gracias, bueno, mucho, donde, aquí) are homographs, not
  plain es vocab: with "a" corroborating, "a bale of hay" was tagged es and translated (XERK-1419 QA).
- `_FOREIGN_CHARS` zeroes a language for letters it never writes (fr, it: á í ó ú ñ), in lowercase
  words only: French turns name "María"/"Cancún". Don't add `en` (English names "José" too).
  A list call it overturns still reaches `_frequency_lang` as a correction: zeroing fr first
  sent "Tu pelo volverá a crecer." through the stricter None-turn gate and lost it. A clear
  frequency call still wins over the letters ("Le jalapeño est très piquant." stays fr).
- New es vocab must be ≥10× rarer (wordfreq) in pt/it: pt `vamos`/`estás`, it `tengo`/`ella`
  are Spanish-looking but native there, so they stay out.
