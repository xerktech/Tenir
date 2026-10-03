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
- Only es "he" may count as an English shared word in a rewording (before a kept English-only
  word, ≥2 such kept). A frequency-derived "shared" class (margin ≥0) also holds de
  `will`/`also`/`kind`, fr `place`: real code-switched turns ("Marco will my new car") dropped.
- `_same_text` must count deleted source words as changed: SequenceMatcher encodes a reorder
  as delete+insert ("Ana incluida" → "including Ana"). Only an output made solely of source
  words skips the check.
- Mid-turn capitals aren't reliably names: German nouns ("Kind") and words after `.!?` aren't.
- A one-hit `en` call whose hit is a `_SHARED_EN` word returns `None` ("to cansado", "the vert"
  closed live runs, XERK-1516). Gate the decision, not `_scores`: dropping shared words from the
  score lost ~13% of English and let "The Las Vegas trip." win es. Costs ~3.5% of short English.
- `is_english_word` excludes English vocab that is also a native word (`_SHARED_EN`: de `was`,
  pt `to`/`for`, fr `but`/`the`, …): "…, Pedro, was?" → "…, what?" is a real translation.
- Measure vocab changes against recorded segments (export `segments.text`, run old vs new
  `detect_lang`, hand-read every changed label), not intuition. Keep transcript text out of tests.
