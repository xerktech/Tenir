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
  close match only when every changed source word is `is_english_word`.
- Measure vocab changes against recorded segments (export `segments.text`, run old vs new
  `detect_lang`, hand-read every changed label), not intuition. Keep transcript text out of tests.
