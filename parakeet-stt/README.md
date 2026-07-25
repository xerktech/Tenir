# parakeet-stt — live STT server

A tiny FastAPI wrapper putting **NVIDIA Parakeet** (`nvidia/parakeet-tdt-0.6b-v3`,
NeMo) behind the OpenAI **`POST /v1/audio/transcriptions`** surface the api speaks
to via the LiteLLM gateway. This is Tenir's live STT model.

## Why Parakeet

The previous model, Voxtral, was an instruction-tuned audio *LLM*; on silence or
noise it would *answer* instead of transcribe ("I'm sorry, I couldn't hear
that…") — a whole class of hallucination ([XERK-92](https://xerktech.atlassian.net/browse/XERK-92)).
A dedicated ASR model like Parakeet has no chat objective and structurally can't
do that, tops the open ASR leaderboards on accuracy + speed, and v3 is
multilingual with **automatic language detection** (Spanish, English, ~23 more
European languages). It also returns **word timestamps**, which the vLLM-Voxtral
endpoint didn't.

## Endpoints

- `GET /health` — `200` once the model is resident, `503` while loading.
- `POST /v1/audio/transcriptions` — multipart `file` (+ optional `language`,
  `response_format`, `timestamps`). `timestamps=false` skips word/segment timing for
  callers that only want the text — the api's live *partials*, which are most of the
  request volume ([XERK-115](https://xerktech.atlassian.net/browse/XERK-115)). It
  defaults on, and `verbose_json` always keeps timing. `response_format`:
  - `json` (default) → `{"text", "language", "words"}` (the `words` superset is what
    `api.stt.voxtral.VoxtralEngine` — the generic HTTP transcription engine — reads
    for per-word timing)
  - `text` → plain text
  - `verbose_json` → adds `segments` + `duration` (for LiteLLM's OpenAI transform)

## Build & run

Built by the unified release pipeline (`.github/workflows/release.yml`) → pushed
to `ghcr.io/xerktech/tenir-parakeet-stt`. The dev stack builds it locally
(`docker compose up --build`); the DockerOps `tenir-gpu` stack runs the published
image on port **9401**.

## Routing

Where the api can open a socket straight to this server — the single-host compose
stack can — it does: `API_STT_ENDPOINT=http://parakeet:8000/v1` takes the LiteLLM hop
off the caption hot path, which pays it once per *partial*
([XERK-115](https://xerktech.atlassian.net/browse/XERK-115)). Cues still go through
the gateway.

Unset, the api falls back to the gateway, aliased `parakeet` (`litellm/config.yaml`
for dev; the DB-configured gateway in the homelab) — the right route for a split-host
deploy that can only reach the model that way. Use the **`openai/`** provider: this
server supports `verbose_json`, so the OpenAI transform's `json → verbose_json`
rewrite (which vLLM-Voxtral 400'd on) is fine and carries the word timestamps back.
The api selects the model via `API_STT_MODEL=parakeet`.

## Tests

`pytest tests` — the transport/dispatch layer against a fake model, so NeMo and the
GPU stay out of CI (`.github/workflows/parakeet-stt.yml`).

## First-run checks (guarded in `server.py`, but verify against the model card)

- The per-hypothesis **language** attribute name (v3 auto-LID) — read defensively.
- The **word-timestamp** dict keys from `transcribe(timestamps=True)`.
- The NGC base tag has **Blackwell (sm_120)** kernels — bump `NEMO_IMAGE` if not.
