# FLEURS STT checks (XERK-1476)

Public-data checks for an STT model swap, through Tenir's own client (`api.stt.parakeet`) and,
for streams, the whole live path. Not CI-gated. Downloaded audio stays in a scratch directory.

```bash
pip install -e api jiwer httpx websockets numpy
for L in es_419 en_us fr_fr; do                       # FLEURS test split, ~1.2 GB in all
  curl -sfLO https://huggingface.co/datasets/google/fleurs/resolve/main/data/$L/test.tsv && mv test.tsv $L.tsv
  mkdir -p $L && curl -sfL https://huggingface.co/datasets/google/fleurs/resolve/main/data/$L/audio/test.tar.gz | tar xz -C $L
done
# 1. per-clip WER, language tag, 1.5 s partial, silence/noise probe (auto; --pinned pins each set)
python fleurs_lang.py --root . --endpoint http://127.0.0.1:18001/v1 --model whisper-large-v3-turbo --out auto.json
# 2. live path: six ~3 min streams through a local api (STT only) + ws_driver
python build_streams.py . streams
API_STT_BACKEND=parakeet API_STT_ENDPOINT=http://127.0.0.1:18001/v1 API_STT_MODEL=whisper-large-v3-turbo \
API_STT_PARTIAL_INTERVAL_MS=350 API_PERSISTENCE_BACKEND=memory API_AUDIO_BACKEND=off \
API_AUTH_ADMIN_USERNAME=... API_AUTH_ADMIN_PASSWORD=... API_AUTH_SECRET=<32+ chars> \
  uvicorn api.main:app --port 8182 --ws-ping-interval 0
python ../../translation_eval/public/ws_driver.py streams/clips.json --out run.json \
  --base http://127.0.0.1:8182 --concurrency 6 --no-wait --tail-idle 15
python score_streams.py streams/clips.json run.json http://127.0.0.1:8182
```

- FLEURS clips are read speech with leading/trailing room noise; the 0.8 s gaps between turns
  are where a model that invents text on non-speech shows it.
- A `kubectl port-forward` drops under six concurrent streams; `stage.stt.errors` in a run is
  mostly that. Finals retry through it (XERK-1499); compare WER, not error counts.
- Results: `../../longform_eval/RESULTS-2026-10.md`, section "Whisper switch check".
