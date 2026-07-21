# Tenir — Web UI (`web/`)

The self-hosted speech-to-text surface: a React + Vite SPA built on
`@tenir/client-core` against the Tenir REST API.

- **Live** — record a session from the browser mic and watch the transcript
  stream in (partial + finalized segments).
- **History & search** — browse/search stored sessions, read segments with
  timing, download retained audio, delete.
- **Status** — per-component system health lights.
- **Users** — admin-only household roster (add/remove members).
- **Login** — auth is always required: log in to your household, then log out
  from the header.

There is no separate web container: the api image builds this SPA and serves it
from the same origin, so the app talks to `window.location.origin` in
production. `VITE_API_HTTP` points local dev at a different api.

## Develop

From the **repo root** (npm workspaces — install once at the top level):

```bash
npm install
npm run dev --workspace @tenir/web      # Vite on :5174 (api http://localhost:8080)
# point it at a different api via VITE_API_HTTP:
VITE_API_HTTP=http://localhost:8080 npm run dev --workspace @tenir/web
```

```bash
npm run typecheck --workspace @tenir/web
npm run test --workspace @tenir/web      # vitest + Testing Library
npm run build --workspace @tenir/web
```

The shared client/contract live in `packages/client-core` and `packages/contract`.
