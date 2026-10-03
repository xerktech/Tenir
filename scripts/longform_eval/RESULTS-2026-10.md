# Long-form STT → translation eval — October 2026 (XERK-1414)

Question: on an hour of continuous Spanish conversation, how accurate is the live path
(Parakeet STT → langid → MiLMMT-46-4B translation), where does it lose, and can the
current models be made good enough?

**Answer: STT is the bottleneck, not translation.** Given the reference Spanish, MiLMMT
scores COMET 83.7 / chrF++ 68.3 on the same turns; end to end it scores 71.9 / 50.9. The
fixes below recover about a point of chrF++ and 0.6 COMET; segmentation tuning moves
nothing beyond noise. Closing the rest needs a better STT model — candidates below.

## Setup

- **Data**: a public hour-long two-speaker Spanish conversation (68 min), 7 × 10-minute
  chunks. Spanish reference = OCR of its burned-in captions (2,825 cues); English
  reference = its uploaded English subtitles (2,790 cues). Content is third-party: only
  aggregates are recorded here.
- **App**: the `api` package at origin/main 4bf3283 run locally, wired directly to the
  deployed `tenir-stt` (Parakeet-TDT-0.6B-v3) and `tenir-translator` (MiLMMT-46-4B,
  `milmmt` prompt), production STT settings (350 ms partials, 8 s cap, 500 ms silence),
  translation hold 3 s. Chunks streamed in real time over the WebSocket (`ws_driver.py`).
- **Metrics**: long-form WER per chunk (`WERf` = accent-folded); translation per final
  turn with each English cue assigned to the turn it overlaps most (chrF++, BLEU,
  COMET-22 `wmt22-comet-da`). See README for caveats — the caption reference condenses
  speech, so absolute WER has a non-zero floor; compare rows.

## Results

| run | WER | WERf | chrF++ | BLEU | COMET | untranslated units |
|---|---|---|---|---|---|---|
| live baseline (main) | 26.27 | 23.33 | 50.91 | 29.55 | 71.94 | 55 / 822 |
| + langid fix (replay of the live finals) | 26.27 | 23.33 | 51.32 | 29.89 | 72.43 | 39 / 822 |
| + blank-final retry (offline STT, no partials) + langid | 25.65 | 22.68 | 51.91 | 30.54 | 72.59 | 38 / 825 |
| **reference Spanish → MiLMMT** (translator ceiling) | — | — | **68.29** | **51.71** | **83.72** | 1 / 816 |

- Replay-to-replay noise is ~0.3–0.6 chrF++ (translator nondeterminism + timeouts), so
  the langid row alone is within noise on this Spanish data; its value is fewer wrong
  source-language labels and far fewer English turns misread (below).
- The retry row comes from `offline_stt.py --no-partials`, whose baseline is 28.81 /
  26.02 with 22 turns lost (no partial fallback); the retry recovers 21 of them. Live,
  partials already rescue most of those turns with lower-quality partial text.

## Where it loses

- **STT deletions dominate**: sub 7.3%, del 13.2%, ins 2.8% of reference words; deleted
  words come mostly in runs of 4+ (1,048 of 1,577), i.e. whole stretches inside turns.
- **Parakeet blanks some windows outright** — deterministic, ~3.5% of final decodes
  (31/875 live); padding the same audio with silence decodes it (fix in PR #176).
- **Parakeet emits English for Spanish speech** on some turns (STT text with English
  function words where the reference is Spanish): those turns tag `en`, close the run and
  are never translated. Pinning `language=es` is ignored by the deployed model
  (byte-identical output).
- **Long windows hurt Parakeet**: fixed 30 s / 60 s windows score WER 38.7 / 50.1 vs 26.3
  on the app's ≤8 s turns. A max-segment sweep (5/8/10/12 s) moved WER by ±1 with no
  monotonic trend — mostly which windows happen to blank; production 8 s stays.
- **Langid misread Spanish as French/Italian** on everyday Spanish words in the fr/it
  vocabularies ("a", "y", "le", "lo", "ha"): 31/867 turns. Fixed on a branch with a
  trade-off for French households (QA corpus probe: English mis-tags 2.8% → 0.6%, Spanish
  FLEURS 97.6 → 99.1%, French FLEURS 99.0 → 94.5% with 0.9% now read as Spanish).
- **Live sessions dropped**: every 10-minute session closed with `1011 keepalive ping
  timeout` when decodes outlasted the partial cadence (inline on the WS intake path).

## STT candidates to test next (last resort, per the ticket)

Run each through `offline_stt.py` against an OpenAI-compatible `/v1/audio/transcriptions`
server on the same chunks, then `replay_trigger.py` + `score.py`; compare to the
baseline row above. In order of expected payoff for Spanish conversational speech:

1. **Whisper large-v3 / large-v3-turbo** — strong multilingual Spanish, honours a pinned
   language (would stop the English drift); offline decode, so latency per turn is the
   cost to measure.
2. **NVIDIA Canary-1B-v2** — same NeMo serving stack as Parakeet, explicit source-language
   prompt, multilingual ES/EN.
3. **Mistral Voxtral Mini** — speech-LLM with strong multilingual ASR; check latency and
   VRAM against the shared GPU budget.

The decision metric is the end-to-end COMET/chrF++ row, not WER alone: the translator
ceiling shows ~11 COMET points available from better Spanish text.
