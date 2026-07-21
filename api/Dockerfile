# tenir — ONE container carrying the api AND the built web UI.
#
# Build context is the REPO ROOT (see docker-compose.yml): the web SPA imports
# the source-only workspace packages @tenir/client-core and @tenir/contract, so
# the whole npm workspace is installed for the vite build, and the resulting
# static bundle is baked into the Python image at /srv/web where the api serves
# it (api.main mounts it at "/").

# ----- build stage: vite-build the web SPA -----------------------------------
FROM node:24-bookworm-slim AS webbuild
WORKDIR /app

# Install with the lockfile first, copying only the workspace manifests so this
# layer caches until a package.json/lock changes. Every workspace listed in the
# root package.json (packages/*, even, mobile, web) must be present for `npm ci`.
COPY package.json package-lock.json ./
COPY packages/client-core/package.json packages/client-core/
COPY packages/contract/package.json packages/contract/
COPY even/package.json even/
COPY mobile/package.json mobile/
COPY web/package.json web/
RUN npm ci

# Sources for the packages web actually compiles, then build. No api URL is
# baked in: the SPA defaults to its own origin, which IS the api.
COPY packages/ packages/
COPY web/ web/
RUN npm run build --workspace @tenir/web

# ----- runtime stage: the api serving /ws, REST and the SPA ------------------
FROM python:3.11-slim

# Auto-link the GHCR package to this repo so the Actions token inherits write
# access on push.
LABEL org.opencontainers.image.source="https://github.com/xerktech/tenir" \
      org.opencontainers.image.description="tenir: recorded + stored STT sessions — WebSocket/REST api and the web UI in one container"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# API_EXTRAS optionally bundles the non-GPU extras — e.g. "persistence" for the
# Postgres conversation store.
#
# The project uses a src layout (pyproject: tool.setuptools.packages.find
# where=["src"]), so the source tree must be present for the wheel build's
# egg_info step — copy it in before installing. This installs the `api` package
# into site-packages; WORKDIR /srv + CMD `uvicorn api.main:app` then runs it.
ARG API_EXTRAS=""
COPY api/pyproject.toml ./
COPY api/src ./src
# Install the app, then drop the Python build tooling (setuptools + wheel) from
# the runtime image. pip builds in an isolated env, so these base-image copies
# are unused at runtime and only add CVE surface; removing them keeps the Trivy
# scan clean. Same layer so the files don't linger in the image.
RUN if [ -n "$API_EXTRAS" ]; then \
        pip install --no-cache-dir ".[${API_EXTRAS}]"; \
    else \
        pip install --no-cache-dir .; \
    fi \
    && pip uninstall -y setuptools wheel

# The built web UI, served statically by the api (API_WEB_DIR defaults here).
COPY --from=webbuild /app/web/dist /srv/web

EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
