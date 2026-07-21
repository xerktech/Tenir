# Tenir

**Tenir** — /teh-NEER/ — from the Latin *tenēre*, "to hold, to keep": it holds
onto everything worth remembering.

A self-hosted speech-to-text recorder for the **Even G2** glasses, a browser,
and Android: live captions from a self-hosted STT model, with every session
**recorded and stored** — transcript + full audio — and browsable afterwards.

That's the whole product, on purpose. It is the bare-minimum core the previous,
much larger feature set (speaker identity, RAG cues, translation, summaries,
chat, …) was stripped back to; features return one at a time, slowly.

## What it does

- **Streaming transcription** — phone, browser or glasses mic → real-time
  captions, powered by a self-hosted STT model (Voxtral) behind a LiteLLM
  gateway
- **Recorded, stored sessions** — every session is persisted as a conversation:
  transcript segments in Postgres, full audio retained on disk; browse, search,
  replay, export and delete from the UI
- **One app container** — the api serves the WebSocket capture endpoint, the
  REST surface, *and* the built web UI on a single origin (`:8080`)
- **Multi-user auth** — login issues a bearer token; every session and
  conversation is scoped to the user's household
- **Clients** — the web UI (also the phone surface for the Even app), an Even G2
  glasses app (live captions on the lens), and an Android app (phone-mic capture
  + history)

## Quick start

```bash
# Backend (single-host stack — app + Postgres + LiteLLM + Voxtral STT)
cp .env.example .env           # set API_AUTH_SECRET + bootstrap admin
docker compose up --build      # app (api + web UI) on :8080
curl localhost:8080/health

# Frontends (install once from repo root)
npm install

# Web UI in dev (the built UI is already served by the app container)
VITE_API_HTTP=http://localhost:8080 npm run dev --workspace @tenir/web

# Even G2 glasses app
VITE_API_WS=ws://localhost:8080/ws npm run dev --workspace tenir-even   # :5173
npx @evenrealities/evenhub-simulator -g http://localhost:5173

# Android app
npm run typecheck --workspace tenir-mobile
npm run test --workspace tenir-mobile
```

## Deployment

One compose file at the repo root. The GPU host needs the NVIDIA Container
Toolkit for the STT server; everything else is CPU-only.

| Service | Port | Role |
|---|---|---|
| `app` | 8080 | ONE container: FastAPI api (`/health`, `/ws`, auth + history REST) **and** the built web UI, served same-origin — no CORS, no separate web container. Built from `api/Dockerfile` (repo-root context) |
| `postgres-tenir` | 5432 | plain Postgres — transcripts, users (`schema.sql` applied on first boot) |
| `litellm` | 4000 | LiteLLM gateway — the OpenAI-compatible front door the api uses for STT; master-key auth, routing in `litellm/config.yaml` |
| `vllm-stt` | 9400 | OpenAI-compatible Voxtral STT, built from `vllm-stt/Dockerfile`, behind the gateway |

Retained audio lives on a bind mount (`API_AUDIO_DIR`, the "disk" audio
backend). Smoke check once up: `curl localhost:8080/health`, then open
`http://localhost:8080`.

### The LiteLLM gateway

The api reaches the STT server through a single **LiteLLM gateway**: one base
URL + one key (`API_LITELLM_ENDPOINT`, `API_LITELLM_API_KEY`) instead of a
per-model endpoint. Routing lives in [`litellm/config.yaml`](litellm/config.yaml):
the alias the api sends (`API_STT_MODEL`, default `voxtral`) fans out to the
real model vllm-stt serves. To split hosts, run the gateway + STT server on the
GPU box and point `API_LITELLM_ENDPOINT` at it — no code changes, just env.

### Configuration

Secrets come from `.env` (see `.env.example`); non-secret config is baked into
compose. Key envs on the app container:

```
API_AUTH_SECRET        bearer-token signing secret (boot refuses the default)
API_AUTH_ADMIN_*       bootstrap admin (username / password / household)
API_LITELLM_ENDPOINT   OpenAI-compatible base URL for STT (…/v1)
API_LITELLM_API_KEY    gateway key
API_STT_BACKEND        voxtral (prod) | stub (model-free dev/CI)
API_PERSISTENCE_BACKEND  postgres | memory | off
API_AUDIO_BACKEND      disk | memory | off      (+ API_AUDIO_DIR)
```

See [`docs/contributing.md`](docs/contributing.md) for repo layout, testing and
the contract workflow.
