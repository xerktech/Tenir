# Live translations (XERK-160)

Someone speaks Spanish (or French, German, Portuguese, Italian) into a session:
the transcript keeps showing what they actually said, in the language they said
it — and the **English translation** appears alongside, live. No toggle, no
setup: STT already detects each turn's language, and any finalized turn whose
detected language isn't English is translated automatically.

Where it appears:

- **Glasses lens**: the translation shows in the **cue box's slot** — the same
  full-width popup strip, same place, same size (title row "Translation" over a
  scrolling body). A run of consecutive non-English turns keeps appending to the
  box. The box's 10s auto-dismiss countdown does **not** start while the other
  language is still being spoken: only when the api declares the run done
  (`translation.done` — an English turn arrived, or speech went quiet past the
  hold window) does the countdown appear and run, exactly like a cue's
  (XERK-110). Tapping or swiping the box while it counts down resets the
  countdown (XERK-129 parity), and swipes scroll a long body under the host's
  native scroll (XERK-133).
- **Web, Android, and the glasses phone Session page**: turn-by-turn — the
  English rendering sits directly under the original turn, quieter than the
  spoken text and led by a small accent "EN" tag, in the live transcript as it
  arrives. (Turn-by-turn rather than side-by-side: it reads identically on all
  three surfaces, and on Android it keeps each transcript run one selectable
  `<Text>` (XERK-104).)
- **History** (web + mobile + glasses phone): translations persist on their
  segments (`segments.translation`) and render the same way in the stored
  transcript. Search still matches the original text only — the record is what
  was said.

## Cues stand aside

While a translation run is live, **cues neither trigger nor appear** — the
translation owns their box and their attention. This is enforced in three
places: the session skips cue generation while a run is open, a cue that was
already generating when the run opened is dropped before delivery, and the
glasses queue any cue that does arrive behind the translation box. Cues resume
once the run ends.

## How it works

1. **Detection is free.** Parakeet already reports each final's language
   (`CaptionFinal.lang`); a non-English final opens (or extends) a *translation
   run* in `api/src/api/session.py`.
2. **Translation is server-side, off the caption path.** Each non-English final
   is translated by the same chat model + LiteLLM gateway the cues use
   (`API_LLM_MODEL` over `API_LITELLM_ENDPOINT`) — a `/chat/completions` call
   returning `{"translation": …}` (temperature 0, thinking disabled, defensive
   JSON extraction; `api/src/api/translate/openai.py`). Calls are serialized
   through one per-session worker so translations reach the client in transcript
   order, and every failure degrades to an untranslated turn — captions are
   never disturbed.
3. **The run ends on evidence the speaker is done.** An English final closes the
   run immediately; otherwise a silence hold (`API_TRANSLATION_HOLD_MS`, default
   3000ms) closes it once speech stops — partial captions count as activity, so
   the hold can't fire mid-utterance while finals only land at pauses.
4. **Delivery + persistence.** Each translation is a `translation` WS message
   keyed by `segmentId` (see `contract/ws-messages.schema.json`) so clients pair
   it with the turn it renders under; the run's end is `translation.done`, which
   is queued behind the run's last translation so the glasses countdown never
   starts before the text is on screen. Translations are persisted onto the
   `segments` row (additive `translation` column, applied idempotently on pool
   open like the other schema drift).

## Backends (`API_TRANSLATION_BACKEND`)

| Value    | Behaviour                                                             |
|----------|-----------------------------------------------------------------------|
| `off`    | No translations (default). The stripped core stays STT-only.          |
| `stub`   | Model-free, deterministic (`[es→en] …`) for CI/dev — no GPU.          |
| `openai` | Real chat model via the LiteLLM gateway (the cue model, `gpt-oss:120b`). |

The stub is what CI exercises end-to-end (run state → WS messages →
persistence → history). The real prompt was exercised against the deployed GPU
chat server directly (per `CLAUDE.md`'s bypass-the-gateway note): es/fr/de/pt
utterances came back as faithful English (disfluencies preserved), and an
already-English utterance came back unchanged.

```bash
# Model-free demo translations (no extra container):
API_TRANSLATION_BACKEND=stub docker compose up --build

# Real model (shares the cue LLM container):
API_TRANSLATION_BACKEND=openai docker compose --profile cues up --build
```
