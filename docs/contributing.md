# Contributing

## Repo layout

```
api/             FastAPI + WebSocket api (auth, sessions, history) — its Dockerfile
                 also builds the web SPA and serves it from the same container
even/            Even G2 glasses client (Vite + TS) — live capture + lens render
web/             self-hosted React SPA — history, live capture, status, users
mobile/          React Native app (Android) — phone-mic capture + history
packages/
  contract/      generated WS TS types — single source of truth
  client-core/   shared REST + WS client, auth, config, capture state machine
contract/        JSON Schema source of truth (TS + Pydantic generated from here)
docs/            platform references
scripts/         manual end-to-end smoke test against a running stack (not CI)
docker-compose.yml  single-host stack (app + Postgres + LiteLLM + Voxtral STT)
vllm-stt/        Dockerfile for the patched Voxtral STT image
schema.sql       Postgres init schema, applied on first boot
```

## Conventions

- Every change goes through a **feature branch and a pull request** — never push
  directly to the default branch.
- Name branches with a short `type/slug` form (e.g. `feat/session-export`,
  `fix/ws-resume`).

## Tests

Tests ship with every change. CI is per-component (`.github/workflows/`); a
change is not done until it is green.

```bash
# API (Python) — runs with the 85% coverage gate
cd api && pip install -e '.[dev]' && pytest

# Clients (TS) — typecheck, test, and build every workspace
npm install && npm run typecheck && npm run test && npm run build
```

## The WS contract

`contract/ws-messages.schema.json` is the single source of truth for the
WebSocket messages. After editing it, regenerate both outputs and commit them:

```bash
make gen        # TS (packages/contract) + Pydantic (api/src/api/contract)
```

CI's contract-drift job fails if the generated files don't match the schema.

## Releases

Unified: one `v<MAJOR>.<MINOR>.<PATCH>` tag carrying all components on push to
`main` — see `RELEASING.md` and `.github/scripts/`.
