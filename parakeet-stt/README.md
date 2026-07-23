# parakeet-stt — evaluation STT server

A tiny FastAPI wrapper putting **NVIDIA Parakeet** (`nvidia/parakeet-tdt-0.6b-v3`,
NeMo) behind the OpenAI **`POST /v1/audio/transcriptions`** surface the api already
speaks — so it can run **side by side with Voxtral** and be A/B'd by flipping a
route, not editing code.

## Why

Voxtral is an instruction-tuned audio *LLM*; on silence/noise it can *answer*
instead of transcribe ("I'm sorry, I couldn't hear that…"), the whole class of
hallucination [XERK-92](https://xerktech.atlassian.net/browse/XERK-92) fought. A
dedicated ASR model like Parakeet has no chat objective and structurally can't do
that, tops the open ASR leaderboards on accuracy + speed, and v3 is multilingual
with **automatic language detection** (Spanish, English, ~23 more European langs).
It also returns **word timestamps**, which the vLLM-Voxtral endpoint didn't.

## Endpoints

- `GET /health` — `200` once the model is resident, `503` while loading.
- `POST /v1/audio/transcriptions` — multipart `file` (+ optional `language`,
  `response_format`). `response_format`:
  - `json` (default) → `{"text", "language", "words"}` (the `words` superset is what
    `api.stt.voxtral.VoxtralEngine` already reads for per-word timing)
  - `text` → plain text
  - `verbose_json` → adds `segments` + `duration` (for LiteLLM's OpenAI transform)

## Build & run

Built by `.github/workflows/parakeet-stt.yml` (manual `workflow_dispatch`, since
this is an evaluation image outside the unified release train) → pushed to
`ghcr.io/xerktech/tenir-parakeet-stt`. The DockerOps `tenir-gpu` stack runs it on
port **9401** next to vLLM-Voxtral (9400).

## Evaluate against Voxtral

The deployed LiteLLM gateway is **DB-configured** (not this repo's dev
`litellm/config.yaml`). To A/B in the homelab:

1. Stand up the container (DockerOps `tenir-gpu.yaml` → `tenir-parakeet`).
2. In the LiteLLM admin, add a `parakeet` model routing to
   `http://10.10.10.22:9401/v1` (mode `audio_transcription`, `openai/` provider —
   this server supports `verbose_json`, so the transform that 400'd on Voxtral is
   fine here).
3. Set `API_STT_MODEL=parakeet` on `tenir-api` (already env-overridable). No code,
   no image rebuild — flip back to `voxtral` to compare.

## First-run checks (guarded in `server.py`, but verify against the model card)

- The per-hypothesis **language** attribute name (v3 auto-LID) — read defensively.
- The **word-timestamp** dict keys from `transcribe(timestamps=True)`.
- The NGC base tag has **Blackwell (sm_120)** kernels — bump `NEMO_IMAGE` if not.
