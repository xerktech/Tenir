# CLAUDE.md

Working conventions for Claude Code in this repository.

(Permissions and commit/PR attribution are configured in `.claude/settings.json`.)

## Conventions

- **Always use a PR**: Never push changes directly to the default branch. For
  every change, work on a feature branch and open a pull request.
- **Contextual branch names**: Name branches after the work they carry, not with
  random words. Use a short `type/slug` form — e.g. `feat/rag-cues`,
  `fix/cue-dedup`, `docs/branch-naming` — so the branch is self-describing.

## Auth (built-in + optional OIDC)

- **Built-in username/password auth is always on and is never removed.** Every session and
  recording is scoped to the logged-in user's household by a signed bearer token.
- **Authentik OIDC is a second backend, opt-in and off by default** (`API_OIDC_ENABLED`, default
  false). When on, the API accepts *both* built-in tokens *and* Authentik access tokens — never
  OIDC-only, so the operator can always log in during an IdP outage. Enabling/disabling is a
  config-only, reversible change; no doc may claim built-in auth must be removed.
- **Contract lives in three docs, keep them in sync:** design contract `docs/auth-oidc.md`,
  operator enable/migrate/rollback runbook `docs/oidc-runbook.md`, Authentik-side deploy
  `docs/authentik-oidc.md`. The `API_OIDC_*` / `API_AUTH_ADMIN_EMAIL` vars are defined in
  `api/src/api/config.py`, wired in `docker-compose.yml`, and documented in `.env.example` — a new
  or changed OIDC var touches all four. See also `.claude/rules/authentik.md`.

## Product design & cross-platform parity

- **Web and Android in parity**: The web UI and the Android app are two front
  ends onto the same product. Keep them as similar as possible and at perfect
  feature parity — any feature added, changed, or removed on one platform ships
  the equivalent on the other in the same effort. Neither is allowed to drift
  ahead. When platform constraints force a difference, make it a deliberate,
  documented exception rather than an accidental gap.
- **Follow Turma's design language**: Model Tenir's design and appearance
  closely on the Turma app. Match its layout, component patterns, typography,
  spacing, iconography, and overall visual style so the two clearly read as
  products from the same company.
- **Keep Tenir's own colors**: Follow Turma in everything visual *except* color
  — Tenir keeps its separate color scheme. Apply Turma's structure and styling
  with Tenir's palette; don't adopt Turma's colors.

## Tests & code coverage

Tests are a required part of every change, not a follow-up. CI is per-component:
each component owns one **PR-gate** workflow under `.github/workflows/` (e.g.
`api.yml`, `web.yml`, `veiller.yml`) that runs its checks on PRs touching its
dir (or the shared workspace; `veiller/` is self-contained and gates only on
its own dir). Publishing is unified — one `release.yml` cuts a single
`v<MAJOR>.<MINOR>.<PATCH>` tag carrying all components on push to `main` (see
`RELEASING.md` and `.github/scripts/`). A change is not done until its checks are
green.

- **Add and update tests with the code**: Any new behavior ships with tests that
  exercise it, and any change to existing behavior updates the affected tests in
  the same PR. Don't open a PR that adds or changes logic without touching tests.
- **Cover bug fixes**: A bug fix includes a regression test that fails before the
  fix and passes after it.
- **Maintain coverage**: The API enforces a minimum line coverage of **85%**
  via `--cov-fail-under` (configured in `api/pyproject.toml`). New code
  must keep coverage at or above this bar — raise the threshold when you can, never
  lower it to make CI pass. Generated contract code is excluded from the metric.
- **Keep the suite green and fast**: Run `pytest` in `api/` (it reports
  coverage by default) before pushing. Don't skip, `xfail`, or delete tests to get
  a passing run.

### Running tests locally

```bash
# API (Python) — runs tests with coverage and the 85% gate
cd api && pip install -e '.[dev]' && pytest

# Clients (TS) — type-check, test and build every workspace (even, mobile, web)
npm install && npm run typecheck && npm run test && npm run build
```

### Testing against the real models (bypass LiteLLM)

The GPU model servers run on the host **`maxai.xerktech.com`** (its IP drifts —
currently `10.10.10.26`) and can be hit **directly**, bypassing the LiteLLM
gateway — useful for exercising real
model behaviour (e.g. cue/translation accuracy) instead of only unit tests or
the stub. Both expose an OpenAI-compatible API:

- **`maxai.xerktech.com:9402`** — cue/summary/translation LLM: `qwen3.8-27b`
  served by SGLang (NVFP4 + DFlash speculative decoding; `GET /v1/models`,
  `POST /v1/chat/completions`). Point
  `OpenAICueGenerator(endpoint="http://maxai.xerktech.com:9402/v1", model="qwen3.8-27b", api_key="")`
  (or `OpenAITranslator(...)`) straight at it to drive the real model. Thinking
  is toggled via `chat_template_kwargs.enable_thinking` (on by default for cues,
  off by default for translations — see `api/src/api/config.py`). The model id
  is `qwen3.8-27b` (a wrong id 404s on /chat/completions, which looks like a
  missing route). `GET /v1/models` returns 200 here and is a reliable liveness
  probe (`/health` also returns 200, occasionally slow to first respond). The
  authoritative production check is still a real completion through the LiteLLM
  proxy for `qwen3.8-27b-dflash`. (The retired port **8890** fronted SGLang behind
  a proxy that hung `GET /v1/models` and `/health` at ~21s even when healthy and
  only passed `POST /v1/chat/completions` — the XERK-680 confusion; 8890 is now
  dead on every route (XERK-681), so use 9402.)
- **`10.10.10.22:9401`** — Parakeet STT (`GET /health`). The host IP drifts
  (currently `maxai.xerktech.com` resolves to `10.10.10.26`); prefer the DNS name
  and don't trust a hard-coded IP here.

These are the same servers the gateway routes to (the `qwen3.8-27b-dflash` alias);
talking to them directly skips the gateway alias and auth so you can iterate on
prompts/params without the full stack.
