# Long-form STT → translation eval — October 2026 (XERK-1414)

Question: on an hour of continuous Spanish conversation, how accurate is the live path
(Parakeet STT → langid → MiLMMT-46-4B translation), where does it lose, and can the
current models be made good enough?

**Answer: STT is the bottleneck, not translation.** Given the reference Spanish, MiLMMT
scores COMET 83.7 / chrF++ 68.3 on the same turns; end to end it scores 71.9 / 50.9. The
fixes below recover about a point of chrF++ and 0.6 COMET; segmentation tuning moves
nothing beyond noise. A better STT model closes much of the rest: **Whisper large-v3-turbo**
cuts WER 29.2 → 18.3 and adds ~6 chrF++ on the same turns at a similar size and speed to
Parakeet (comparison below).

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

## STT model comparison (same turns, same 3090)

Candidates decoded the **same 867 turn spans** of the live run (`turn_stt.py`: segmentation
held fixed, one request at a time), on the production STT card (an RTX 3090 shared with the
translator) with `tenir-stt` scaled down; translations replayed through the deployed MiLMMT
(`replay_trigger.py`). The fixed-turn harness has no partial-text fallback for blank
decodes, so Parakeet scores worse here than live (29.2 vs 26.3 WER) — compare rows within
this table.

| model | params | VRAM on card | turn latency p50 / p90 / max | WER | WERf | chrF++ | BLEU | untranslated |
|---|---|---|---|---|---|---|---|---|
| Parakeet-TDT-0.6B-v3 (prod, auto-lang) | 0.6B | ~3.2 GiB | 103 / 113 / 139 ms | 29.16 | 26.41 | 49.46 | 28.44 | 74 |
| Canary-1B-v2 (`es` pinned) | 1B | ~6.8 GiB | 108 / 179 / 26,753 ms | 21.07 | 18.08 | 54.22 | 32.72 | 10 |
| **Whisper large-v3-turbo** (`es` pinned, vLLM) | 0.8B | ~1.9 GiB + KV | **96 / 117 / 218 ms** | 18.25 | 15.43 | 55.88 | 35.29 | 17 |
| Whisper large-v3 (`es` pinned, vLLM) | 1.55B | ~3.4 GiB + KV | 163 / 250 / 325 ms | 17.92 | 14.95 | 56.57 | 36.01 | 4 |
| Voxtral Mini 3B | 3B | ~9.4 GiB weights | — | did not fit beside the translator on the shared card; ruled out on size | | | | |

- **Pick: Whisper large-v3-turbo.** It cuts WER by ~11 points (29.2 → 18.3, −37%
  relative) and adds ~6 chrF++ end to end over Parakeet at a similar cost: 0.8B vs 0.6B
  params; p50 96 vs 103 ms but p90 117 vs 113 ms and max 218 vs 139 ms; weights +
  activations ~1.9 GiB vs Parakeet's ~3.2 GiB process, but vLLM's KV pre-allocation makes
  its real footprint a serving choice — size it when deploying (XERK-1476). Whisper
  large-v3 buys only another 0.3 WER / 0.7 chrF++ for ~1.7x the p50 latency and ~1.8x the
  weights. Canary is worse than both Whisper models, uses more memory (6.8 GiB card delta;
  not directly comparable to the vLLM rows' weights + activations), and had a 26.8 s
  outlier.
- Pinning `es` is part of the win (it stops the English drift Parakeet showed); the
  candidates were run pinned, as a session's `source_lang` would. An auto-language
  Whisper run was not measured.
- `untranslated` includes translator calls that failed (timeouts on the shared translator):
  turbo 9 of its 17, Canary 4 of 10, large-v3 0 of 4, Parakeet 0 of 74 — so the Whisper
  turbo vs large-v3 gap in that column is mostly translator noise, not the STT model.
- VRAM: vLLM pre-allocates KV cache from `--gpu-memory-utilization`; the figures are its
  reported weights + non-torch + activation, read from server logs (not re-verifiable —
  the eval pod is gone). NeMo figures are the card's used-memory delta. Latency is client wall time from a Turma host
  to the pod, one request in flight, on a card shared with other workloads.
- Not measured: COMET for these rows (chrF++/BLEU only), partials (Whisper has no cheap
  streaming mode — partials would re-decode the turn like Parakeet's do today), and a
  live WebSocket run on the new model. Switching models is follow-up work (serving +
  `api.stt` engine wiring): XERK-1476.
