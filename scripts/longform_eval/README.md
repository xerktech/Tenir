# Long-form STT → translation eval (XERK-1414)

Measures the whole live path — VAD turn segmentation, Parakeet finals, langid, the
translation trigger and MiLMMT — on **long continuous conversation**, which the FLEURS
sweep (`../translation_eval/public/`, ~10 s clips) never exercises. Source: a public
hour-long Spanish conversation video with burned-in Spanish captions (the STT
reference) and uploaded English subtitles (the translation reference), cut into
10-minute chunks and streamed through the app in real time.

Not CI-gated. Downloaded media, OCR output and run JSONs are third-party content:
keep them in a scratch directory, never commit them or paste their text into docs.
Results docs carry aggregate numbers only.

## Pipeline

```bash
pip install yt-dlp jiwer sacrebleu httpx websockets numpy pillow   # + ffmpeg, tesseract-ocr-spa
U=https://www.youtube.com/watch?v=bhQnooudZcs
yt-dlp -f 251 -o audio.webm "$U"                                   # opus audio
yt-dlp --skip-download --write-subs --sub-langs en --sub-format json3 -o subs "$U"
yt-dlp -f "bv*[height<=480][ext=mp4]" -o video.mp4 "$U"            # for the caption OCR

python ocr_captions.py video.mp4 --out es_caps.json                # ~2.8k cues, CPU-bound
python build_refs.py es_caps.json subs.en.json3 --out refs.json

ffmpeg -i audio.webm -ac 1 -ar 16000 -sample_fmt s16 full16k.wav
for i in 0 1 2 3 4 5 6; do ffmpeg -ss $((i*600)) -t 600 -i full16k.wav chunks/c$i.wav; done
# clips.json: [{"id": "c0", "wav": "chunks/c0.wav"}, ...]  — ids MUST be c<N>

# app under test: the api package wired straight at the deployed models
API_STT_BACKEND=parakeet API_STT_MODEL=parakeet \
API_STT_ENDPOINT=http://tenir-stt.ai.svc.cluster.local:8000/v1 API_STT_PARTIAL_INTERVAL_MS=350 \
API_LITELLM_ENDPOINT=http://tenir-translator.ai.svc.cluster.local:8000/v1 \
API_TRANSLATION_BACKEND=openai API_TRANSLATION_MODEL=milmmt-46-4b API_TRANSLATION_PROMPT_STYLE=milmmt \
API_TRANSLATION_HOLD_MS=3000 API_CUE_BACKEND=off API_MUSIC_BACKEND=off \
API_PERSISTENCE_BACKEND=memory API_AUDIO_BACKEND=off \
API_AUTH_ADMIN_USERNAME=evaluser API_AUTH_ADMIN_PASSWORD=... API_AUTH_SECRET=... \
  uvicorn api.main:app --port 8181 --ws-ping-interval 0

TENIR_USERNAME=evaluser TENIR_PASSWORD=... python ../translation_eval/public/ws_driver.py \
  clips.json --out run.json --base http://127.0.0.1:8181 --concurrency 7 --tail-idle 60

python score.py refs.json run.json [--comet]                      # STT WER + translation
# faster iteration on STT/langid knobs (see each script's header):
API_STT_ENDPOINT=... python offline_stt.py clips.json --out stt.json
python replay_trigger.py stt.json --out tr.json --endpoint ... --model milmmt-46-4b
python gold_translate.py refs.json run.json --out gold.json \
  --endpoint http://tenir-translator.ai.svc.cluster.local:8000/v1 --model milmmt-46-4b
python score.py refs.json gold.json                                # MT alone, same turns
```

## Reading the numbers

- **WER** is long-form (concatenated finals vs concatenated cues per chunk), so it is
  independent of turn boundaries. `WERf` also folds accents: the OCR reference loses
  or garbles accents more often than whole words, and Parakeet sometimes drops them,
  so `WERf` is the fairer word-accuracy number; the gap between the two is accents.
- The OCR reference is a *caption* track: captions condense fillers/false starts and
  occasionally paraphrase, so a perfect transcript still scores a non-zero WER floor.
  Compare runs against each other; don't read the absolute WER as a model's error rate.
- **Translation units** are final turns; every English cue goes to the turn it overlaps
  most, uncovered cues form "missed" units scored as empty. `untr` counts every scored
  unit with no translation shown: missed units, turns langid tagged `en`, undetected turns
  outside a run, echo drops and failed calls (replay runs record those as
  `translate_error`; `--out` JSON carries the count). Translated turns that no English cue
  overlaps can't be scored and are reported as `unscored_translated_turns`.
- `gold_translate.py` translates the reference Spanish on (nearly) the same turns — turns
  with no Spanish cue are skipped — so e2e − gold is roughly the cost of STT errors, and
  gold alone is the translator's ceiling on this data.
- Latency columns from this harness are only as good as the host running the app;
  a loaded host inflates decode latency (see below).

## Pitfalls

- uvicorn's default WS keepalive (20 s ping / 20 s timeout) dropped every 10-minute
  session with `1011 keepalive ping timeout` when the app ran on a loaded host: STT
  decodes run inline on the intake path, intake fell behind real time, the frame queue
  filled and pongs went unread. Segmentation counts audio bytes, not wall time, so
  disabling the ping (`--ws-ping-interval 0`) keeps accuracy numbers valid while the
  app lags; raise `--tail-idle` so the driver waits for the backlog after
  `session.end`. The product fix is the partial back-off in `api/src/api/stt/streaming.py`.
- `ws_driver.py` used to discard every final already received when a socket dropped
  (the clip scored as silence); it now keeps them and records the error.
- Concurrent OCR workers must not share a temp PNG (garbled stdout); `ocr_captions.py`
  uses a per-call temp file.

## Tests

`python -m pytest scripts/longform_eval/tests/` — scorer alignment, WER edge cases,
json3 parsing and the OCR mask (synthetic data only).
