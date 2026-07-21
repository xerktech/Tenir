# Tenir api (`api/`)

FastAPI + WebSocket backend for recorded, stored STT sessions — and, in the
built image, the server for the web UI too (one container, one origin).

A client opens `/ws` with a bearer token, sends `session.start`, streams raw
PCM, and receives `caption.partial` / `caption.final` frames. Every finalized
turn is persisted to the conversation store as it lands; the full session audio
is retained and flushed to the audio store on `session.end`. The history REST
API serves the stored sessions back.

## Layout

```
src/api/
  main.py          FastAPI app: /health /ready /status /metrics, /ws, static web UI
  session.py       per-connection session: STT seam in, captions out, persistence
  registry.py      live-session registry (resume + graceful shutdown)
  protocol.py      WS frame parse/serialize over the generated contract models
  config.py        env-driven settings (API_*)
  metrics.py       process-local counters/latency summaries (/metrics)
  readiness.py     real-backend reachability (/ready)
  status.py        per-component red/yellow/green cache (/status)
  history.py       conversations REST: list/search/get/export/audio/delete
  auth/            multi-user auth: tokens, users (memory + Postgres), router
  contract/        Pydantic models generated from contract/ws-messages.schema.json
  persistence/     conversation store (memory + Postgres) and audio store
                   (memory + local disk), WAV helpers
  stt/             streaming STT seam: stub (model-free) and voxtral
                   (OpenAI-compatible /audio/transcriptions via LiteLLM)
```

## Running

```bash
pip install -e '.[dev]'    # + '.[persistence]' for the Postgres store
pytest                     # 85% coverage gate
uvicorn api.main:app --port 8080
```

Defaults are model-free and in-memory (`API_STT_BACKEND=stub`,
`API_PERSISTENCE_BACKEND=memory`, `API_AUDIO_BACKEND=memory`), so the api boots
anywhere with no GPU or database. Auth is always on: set `API_AUTH_SECRET` and
the `API_AUTH_ADMIN_*` bootstrap admin to log in.

## Key configuration (env, `API_` prefix)

| Var | Default | Meaning |
|---|---|---|
| `API_AUTH_SECRET` | (insecure) | bearer-token HMAC secret; boot refuses the default |
| `API_AUTH_ADMIN_USERNAME/_PASSWORD/_HOUSEHOLD` | — | bootstrap admin seeded at startup |
| `API_STT_BACKEND` | `stub` | `stub` (model-free) / `voxtral` |
| `API_STT_MODEL` | `voxtral` | model alias sent to the gateway (litellm/config.yaml) |
| `API_LITELLM_ENDPOINT` / `API_LITELLM_API_KEY` | `http://litellm:4000/v1` / "" | OpenAI-compatible base URL + key for STT |
| `API_PERSISTENCE_BACKEND` | `memory` | `memory` / `postgres` / `off` |
| `API_DATABASE_URL` | compose default | Postgres DSN for the `postgres` backend |
| `API_AUDIO_BACKEND` | `memory` | `memory` / `disk` / `off` (+ `API_AUDIO_DIR`) |
| `API_SESSION_RESUME_GRACE_SECONDS` | `30` | how long a dropped session stays resumable |
| `API_WEB_DIR` | `/srv/web` | built web SPA served at `/` when the dir exists |
| `API_STATUS_STT_URL` | "" | optional direct STT health URL; empty = mirror the gateway probe |

The WS message contract is generated — edit
`contract/ws-messages.schema.json` and run `make gen` at the repo root; never
hand-edit `src/api/contract/messages.py`.
