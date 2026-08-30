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

# The floating python:3.11-slim base can trail Debian security updates. Upgrade
# the OpenSSL packages Trivy flags (CVE-2026-14456: libssl3t64/openssl/
# openssl-provider-legacy) so the image-scan gate stays green; a no-op once the
# base image itself ships the fix.
RUN apt-get update \
    && apt-get install -y --no-install-recommends --only-upgrade \
       libssl3t64 openssl openssl-provider-legacy \
    && rm -rf /var/lib/apt/lists/*

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

# The idempotent schema, shipped so the api can self-heal an existing Postgres
# data dir on boot (SqlConversationStore applies it on pool open). Postgres itself
# only runs schema.sql on a FRESH volume via docker-entrypoint-initdb.d, so a DB
# predating an additive table (e.g. `cues`) would never gain it otherwise. Lands
# at the workdir (/srv/schema.sql), where find_schema_file() looks first.
COPY schema.sql ./schema.sql

# The built web UI, served statically by the api (API_WEB_DIR defaults here).
COPY --from=webbuild /app/web/dist /srv/web

# Drop the setuptools build tree and the second copy of the source the wheel
# build leaves at the workdir: `import api` resolves from site-packages, so
# these are dead weight and extra scan surface in the runtime image.
RUN rm -rf /srv/build /srv/src /srv/tenir_api.egg-info

# NOTE: this image still runs as root, deliberately, for now. Dropping to a
# normal user is the right end state, but API_AUDIO_DIR is a pre-existing
# root-owned bind mount on every deployed host: switching the runtime uid makes
# the api unable to write retained audio until an operator chowns that directory
# by hand, and a silent failure there loses recordings. Doing it safely needs an
# entrypoint that fixes ownership before dropping privileges, plus a release
# note — tracked as a follow-up in qa.md rather than smuggled into a QA pass
# (XERK-236). The /ready probe now catches exactly this class of breakage.

EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
