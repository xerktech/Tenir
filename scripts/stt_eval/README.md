# STT eval harness — real session recordings

Measures STT **accuracy and speed on Tenir's own retained session audio**, the
sibling of `scripts/cue_eval/` for the transcription side. It replays every
stored transcript segment's audio span through an OpenAI-compatible
`POST /v1/audio/transcriptions` endpoint — the same surface and the same
decode unit (one final per turn) production uses — then scores the hypotheses
with jiwer and reports latency/RTFx.

Not part of CI — it needs the GPU model server and real conversation data.
The earlier public-data benchmark (`docs/stt-model-gpu-benchmark.md`, FLEURS)
ranked candidate models in the abstract; this harness answers how they do on
*this household's* actual rooms, voices, and code-switching.

Deployment audio and transcripts are the user's private family conversations:
work in a scratch directory, and never paste transcript content into shipped
code, prompts, tests, or docs.

## Ground truth, honestly

The stored transcripts **are** the deployed model's output, so out of the box
a run measures **divergence from production, not accuracy**. The tooling keeps
the two separate at every step:

- With no corrections, `score.py` reports `divergence vs stored production`.
- `--review-out` writes a TSV of diverging segments ranked worst-first — the
  cheapest path to real references is hand-correcting those (listen to the
  span, write what was actually said).
- Corrections live in a JSON file `{segment_id: "corrected text"}` passed as
  `--refs`. Segments with a correction are scored as `WER vs corrected refs` —
  the only number that is authoritative accuracy.

Comparative use (model A vs model B on identical audio) is sound even before
corrections exist: score both runs against the same references and compare.

## 1. Export segments (with audio keys)

Postgres runs in the `Tenir-Postgres` container; the host has no local `psql`.

```bash
docker exec Tenir-Postgres psql -U tenir -d tenir -tAc \
  "select json_agg(row_to_json(t)) from (select s.segment_id, s.conversation_id, \
   s.text, s.start_ms, s.end_ms, s.lang, c.source_lang, c.audio_key \
   from segments s join conversations c on c.id = s.conversation_id \
   where c.audio_key is not null \
   order by s.conversation_id, s.start_ms) t" > stt_segments.json
```

Retained audio is the api container's `/data/audio` bind mount
(`{household}/{conversation_id}.wav`, 16 kHz mono s16le — see
`api/src/api/persistence/`). Find the host path with
`docker inspect Tenir --format '{{json .Mounts}}'`.

## 2. Transcribe

```bash
pip install httpx jiwer   # the harness's only dependencies

python scripts/stt_eval/transcribe.py stt_segments.json \
  --audio-dir /mnt/data/Docker/Tenir/audio \
  --endpoint http://10.10.10.22:9401 --model nvidia/parakeet-tdt-0.6b-v3 \
  --out parakeet.json [--conversations id1,id2,...]
```

Defaults are production-shaped: language auto-detect (`--language pinned`
uses each conversation's `source_lang` instead), word timestamps on
(`--no-timestamps` measures the text-only fast path), no padding
(`--pad-ms` probes word-edge clipping at segment boundaries). Requests run
serialized, matching the server's one-GPU-lock decode model, so `wall_ms` is
honest per-decode latency including network.

## 3. Score and compare

```bash
python scripts/stt_eval/score.py parakeet.json --review-out review.tsv
# ... hand-correct diverging segments into refs.json, then:
python scripts/stt_eval/score.py parakeet.json --refs refs.json
python scripts/stt_eval/score.py candidate.json --refs refs.json
python scripts/stt_eval/report.py parakeet.scored.json candidate.scored.json
```

Normalization matches `docs/stt-model-gpu-benchmark.md`: NFKC, lower-cased,
punctuation stripped, whitespace collapsed; WER is jiwer's, pooled as
errors / reference-words over whatever set is being aggregated.

## 4. Simulate turn segmentation (no model server needed)

Translations and cues run only on finalized turns, so the VAD turn-close
windows (`stt_silence_ms` / `stt_max_segment_ms`) are their latency floor.
`segment_sim.py` replays retained audio through the **real**
`StreamingTranscriber` windowing/VAD code with a stub engine and reports, per
candidate setting, turn-length percentiles, how turns closed (real pause vs
forced cap), and the wait-for-final distribution over speech chunks — the
XERK-175 methodology. It imports the installed `api` package
(`pip install -e api` first):

```bash
python scripts/stt_eval/segment_sim.py \
  --audio-dir /mnt/data/Docker/Tenir/audio \
  --config 500/8000 --config 700/12000 \
  --out sim.json [--conversations id1,id2,...]
```

Boundary decisions only — no transcription happens, so it runs anywhere in
seconds per audio-hour. To judge *quality* at a candidate setting, feed its
boundaries through a real re-transcribe/re-translate pass (steps 2–3) and
hand-read the result.

## Rules that keep a comparison honest

- **Freeze the eval set.** Fix the conversation-id list before iterating and
  pass it via `--conversations`. Re-export changes the set; re-baseline on the
  current export before comparing anything to older numbers.
- **Same references for every run.** A refs file only means something when
  both sides of a comparison are scored against it.
- **Match production decoding.** Keep timestamps on and language on auto
  unless the production config changes; note any deviation with the numbers
  (report.py prints non-default options in the model column).
- **Baseline first.** Replaying the deployed model (Parakeet) over its own
  stored output should diverge only at slice boundaries; a large baseline
  divergence means a harness or export problem, not a model finding.
- **Speed numbers are environment-bound.** RTFx/latency include the network
  hop from wherever the harness runs and depend on GPU load; compare runs
  taken from the same host in the same sitting, and don't compare against the
  FLEURS benchmark's RTFx (different measurement path entirely).

## Tests

`python -m pytest scripts/stt_eval/tests/` covers normalization, WER edge
cases and aggregation, WAV slicing, and the segmentation simulator's boundary
decisions (synthetic audio; needs jiwer, and the simulator tests skip unless
the `api` package is installed). Not CI-gated, same as the rest of the
harness.
