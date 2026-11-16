# tenir-diarizer — speaker tracking for mic captions (XERK-2958)

One phone mic hears the whole table. This service answers, per caption turn, **who spoke
when** and, where two people talked at once, **each voice on its own**, so the api can caption
"Speaker 1", "Speaker 2", … consistently for a whole conversation and attribute overlapped
words to the right person.

## How it works

- **Streaming Sortformer** (`nvidia/diar_streaming_sortformer_4spk-v2`, NeMo, CC-BY-4.0)
  keeps a per-conversation speaker cache. Speakers are numbered by first appearance and the
  cache carries them through the session, so numbering stays stable. Up to **4 speakers**;
  a fifth is folded into the most similar of the four (a model limit).
- `server.SortformerStream` drives the model a turn at a time. A chunk is committed only once
  its right context has arrived, so committed frames match NeMo's own whole-file streaming
  pass (max abs diff 1e-6 on a real recording). The turn's last ~1 s is scored on a *copy* of
  the state: labelled now, without teaching the cache audio it has not finished hearing.
- `planner.plan_pieces` cuts the turn: sequential talk at the middle of each gap (no word in
  a pause is lost); short flickers fold into a neighbour; talk overlapping another speaker's
  for ≥ 0.48 s becomes one piece per speaker, marked for separation.
- **SepFormer** (`speechbrain/sepformer-whamr16k`, noisy reverberant 16 kHz, Apache-2.0)
  splits an overlapped piece into two streams; each stream goes to the speaker whose activity
  its energy follows. A third simultaneous speaker gets the raw mix.

## API

- `GET /health` — 200 once both models are resident; 503 while loading (or failed).
- `POST /v1/diarize?session=<conversation id>&offset_ms=<turn start>` — body: the turn's
  raw 16 kHz s16le mono PCM. Turns of one session must arrive in order; a gap is filled with
  silence (a jump over `DIAR_MAX_GAP_S`, 10 s, moves the clock instead), a resent span is not
  fed twice, an unknown session starts its timeline at `offset_ms` (after a restart numbering
  starts over for that conversation). `epoch` is the api's per-sitting token: a new sitting
  whose offset is behind the session's timeline restarts the session (its audio is new speech).

  ```json
  {"speakers": 4, "pieces": [
    {"speaker": 1, "startMs": 0, "endMs": 2100, "overlap": false, "audio": null},
    {"speaker": 2, "startMs": 1700, "endMs": 4000, "overlap": true, "audio": "<base64 s16le>"}
  ]}
  ```

  Times are relative to the turn. `audio` is the separated stream for an overlapped piece,
  else `null` (the api slices its own PCM). `[]` = no speech heard.

## The api side

`API_DIARIZER_URL` turns it on (`api/src/api/stt/diarize.py`, `StreamingTranscriber._finalize`).
Every turn is posted (silence too: it keeps the timeline). A one-speaker turn decodes exactly as
before and gets a label; a multi-speaker turn becomes one `caption.final` per piece. Any
diarizer error or a reply slower than `API_DIARIZER_TIMEOUT_SECONDS` (3 s) captions the turn
whole and unlabelled, and the conversation skips the diarizer for 30 s, so a hung service
costs one timeout per 30 s rather than one per caption.

## Access control

`/v1/diarize` takes `Authorization: Bearer <DIAR_API_KEY>`: the api sends its
`API_DIARIZER_KEY`, and both come from the same secret (XERK-3005). Without the key anyone who
reaches the pod and knows a conversation id could feed that conversation's speaker cache
(re-numbering its speakers) or read its speaker activity. Unset, every turn is refused (503) and
the api captions unlabelled. `/health` stays open for probes. The ArgoCD NetworkPolicy admitting
only the api pod is a second layer, not the only one.

A turn the api has hung up on (it gives up after its timeout) is dropped: the server checks for
the disconnect once it holds the session lock and between model steps, and answers 499. A turn
dropped before it was fed is a gap the next turn fills, as for an api timeout.

## Config (env)

| var | default | |
|---|---|---|
| `SORTFORMER_MODEL` | `nvidia/diar_streaming_sortformer_4spk-v2` | HF id or a `.nemo` path |
| `SEPARATOR_MODEL` | `speechbrain/sepformer-whamr16k` | empty disables separation |
| `DIAR_CHUNK_LEN` / `DIAR_CHUNK_RC` / `DIAR_FIFO_LEN` / `DIAR_SPKCACHE_UPDATE` / `DIAR_SPKCACHE_LEN` | 124 / 1 / 124 / 124 / 188 | model card's 10 s streaming setup (see Results) |
| `DIAR_MAX_SEPARATE_S` | 12 | longer overlapped pieces come back as the mix |
| `DIAR_SESSION_TTL_S` / `DIAR_MAX_SESSIONS` | 1800 / 256 | in-memory session cache |
| `DIAR_MIN_SEPARATE_OVERLAP_S` | 1.5 | separate only talk shared this long |
| `DIAR_MAX_GAP_S` | 10 | longer timeline jumps move the clock instead of feeding silence |
| `DIAR_API_KEY` | none (refuse every turn) | shared bearer secret; equals the api's `API_DIARIZER_KEY` |
| `DIAR_VRAM_CLAIM_GIB` | 0 (off) | torch memory fraction = the pod's DRA claim |

## Build, test, evaluate

- Image: `diarizer/Dockerfile` (context = repo root), published as `ghcr.io/xerktech/tenir-diarizer`
  by `release.yml`; deployed by hand-pinned ArgoCD `ai/tenir/diarizer.yaml`.
- Tests (scripted model, no torch): `pip install -r requirements-test.txt && pytest`.
- Accuracy: `scripts/diarize_eval/` (README there) — synthetic sessions with true speakers,
  run through the api's own transcriber against the real STT.
