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
`api.yml`, `web.yml`) that runs its checks on PRs touching its dir (or the
shared workspace). Publishing is unified — one `release.yml` cuts a single
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

### Testing against the real models (via LiteLLM)

Every model Tenir uses is deployed in the cluster from the ArgoCD repo (`ai/tenir/*.yaml`)
and reached only through the in-cluster LiteLLM gateway — there is no standalone GPU host to
hit directly (the old `maxai` box is gone). Use it to exercise real model behaviour (cue /
translation accuracy) instead of only unit tests or the stub.

- **Reach it:** `kubectl port-forward -n ai svc/litellm 4000:4000`, then
  `http://localhost:4000/v1` (OpenAI-compatible). Never commit LiteLLM's public hostname.
  The port-forward hangs silently after ~3–5 min of streaming; long real-time runs need a
  watchdog that restarts it (a hung STT call stalls captions until it times out; the
  WebSocket itself stays up, XERK-1424).
- **Key:** a LiteLLM virtual key; Tenir's own is in the `tenir` pod env:
  `LITELLM_KEY=$(kubectl exec -n ai deploy/tenir -- printenv API_LITELLM_API_KEY)` (don't echo it).
- **Model ids are LiteLLM aliases** (registered in LiteLLM's Postgres, not in git), matching the
  `API_*_MODEL` values in ArgoCD `ai/tenir/deployment.yaml`. `GET /v1/models` lists what is live:
  - `milmmt-46-4b-translate` — translations (`tenir-translator`, MiLMMT-46-4B on vLLM); needs
    `API_TRANSLATION_PROMPT_STYLE=milmmt` (completion prompt on `/v1/completions`).
  - `parakeet` — STT (`tenir-stt`), `/v1/audio/transcriptions`.
  - `qwen3.8-27b-dflash` — cues/summaries. Its in-cluster stack (`ai/tenir/qwen.yaml`) is disabled
    and the alias is not served until XERK-1422 lands, so cue evals have no model right now.
- Drive it in code with e.g.
  `CompletionTranslator(endpoint="http://localhost:4000/v1", model="milmmt-46-4b-translate",
  api_key=KEY)` (`api.translate.completion`; `OpenAITranslator` is the chat-json style);
  the eval harnesses take `--endpoint http://localhost:4000/v1 --api-key "$LITELLM_KEY"`.
- A wrong model id 404s on the completion route, which looks like a missing route — check
  `/v1/models` first.
