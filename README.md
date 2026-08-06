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
  captions from self-hosted STT: one offline model (NVIDIA Parakeet) drives both
  the low-latency live partials (a bounded trailing-window re-decode on a cadence)
  and the accurate stored transcript (a whole-turn decode on each finished turn)
- **Live translations** — a turn spoken in another language is transcribed as
  spoken and auto-translated to English (`API_TRANSLATION_BACKEND`): the
  glasses show the translation in the cue box, web/Android/the phone app show
  it under the original turn — see [`docs/translations.md`](docs/translations.md)
- **Music ID** — when a song is playing, recognize the track and show its
  **time-synced lyrics** in the cue box, auto-scrolling as the song plays, titled
  `ARTIST - SONG NAME` (`API_MUSIC_BACKEND`): recognition via shazamio (Shazam's
  global catalog), synced lyrics from LRCLIB — see
  [`docs/music-id.md`](docs/music-id.md)
- **Recorded, stored sessions** — every session is persisted as a conversation:
  transcript segments in Postgres, full audio retained on disk; browse, search,
  replay, export and delete from the UI
- **One app container** — the api serves the WebSocket capture endpoint, the
  REST surface, *and* the built web UI on a single origin (`:8080`)
- **Multi-user auth** — login issues a bearer token; every session and
  conversation is scoped to the user's household
- **Clients** — the web UI, an Even G2 glasses app (live captions on the lens,
  with dedicated Session + History pages on the phone, either surface starting
  and stopping a session), an Android app
  (phone-mic capture + history), and a Veiller miniapp (`veiller/`) — the
  same G2 client packaged for the Veiller app's bundled-miniapp runtime

## Quick start

```bash
# Backend (single-host stack — app + Postgres + LiteLLM + Parakeet STT)
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
Toolkit for the STT servers; everything else is CPU-only.

| Service | Port | Role |
|---|---|---|
| `app` | 8080 | ONE container: FastAPI api (`/health`, `/ws`, auth + history REST) **and** the built web UI, served same-origin — no CORS, no separate web container. Built from `api/Dockerfile` (repo-root context) |
| `postgres-tenir` | 5432 | plain Postgres — transcripts, users (`schema.sql` applied on first boot) |
| `litellm` | 4000 | LiteLLM gateway — the OpenAI-compatible front door the api uses for STT finals + cues; master-key auth, routing in `litellm/config.yaml` |
| `parakeet` | 9401 | OpenAI-compatible NVIDIA Parakeet STT — decodes both live **partials** and accurate **finals** (multilingual + auto language detection), built from `parakeet-stt/Dockerfile` |

Retained audio lives on a bind mount (`API_AUDIO_DIR`, the "disk" audio
backend). Smoke check once up: `curl localhost:8080/health`, then open
`http://localhost:8080`.

### STT (partials and finals)

One model, `parakeet`, serves both caption flavours (`API_STT_BACKEND=parakeet`):

- **Partials** re-decode a bounded trailing window of the in-flight turn on a
  cadence (`API_STT_PARTIAL_INTERVAL_MS`) so the live band stays responsive
  regardless of turn length.
- **Finals** decode the whole finished turn (`API_STT_ENDPOINT`, else the gateway)
  for the accurate stored transcript.

A two-model "hybrid" variant (a separate NVIDIA Nemotron server streaming the
partials) was evaluated and removed once the re-decode partial path was fast
enough to match it without the extra GPU server and caption churn — see
[`docs/stt-rtx4060-benchmark.md`](docs/stt-rtx4060-benchmark.md).

### The LiteLLM gateway

Finals and cues reach their model through a single **LiteLLM gateway**: one base
URL + one key (`API_LITELLM_ENDPOINT`, `API_LITELLM_API_KEY`) instead of a
per-model endpoint. Routing lives in [`litellm/config.yaml`](litellm/config.yaml):
the alias the api sends (`API_STT_MODEL`, default `parakeet`) fans out to the
real model the parakeet server serves. The caption hot path can also post straight
to the model server (`API_STT_ENDPOINT`), bypassing the gateway. To split hosts,
run the gateway + model servers on the GPU box and point the endpoints at it — no
code changes, just env.

### Configuration

Secrets come from `.env` (see `.env.example`); non-secret config is baked into
compose. Key envs on the app container:

```
API_AUTH_SECRET        bearer-token signing secret (boot refuses the default)
API_AUTH_ADMIN_*       bootstrap admin (username / password / household)
API_AUTH_TOKEN_TTL_SECONDS  token lifetime (default 30d); tokens auto-renew on use,
                       so this is the max idle time before a device must re-login
API_LITELLM_ENDPOINT   OpenAI-compatible base URL for STT finals + cues (…/v1)
API_LITELLM_API_KEY    gateway key
API_STT_BACKEND        parakeet (prod) | stub (dev/CI)
API_STT_ENDPOINT       direct /v1 route to the STT model (else falls back to the gateway)
API_PERSISTENCE_BACKEND  postgres | memory | off
API_AUDIO_BACKEND      disk | memory | off      (+ API_AUDIO_DIR)
```

See [`docs/contributing.md`](docs/contributing.md) for repo layout, testing and
the contract workflow.
