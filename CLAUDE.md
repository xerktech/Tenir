# CLAUDE.md

Working conventions for Claude Code in this repository.

(Permissions and commit/PR attribution are configured in `.claude/settings.json`.)

## Conventions

- **Always use a PR**: Never push changes directly to the default branch. For
  every change, work on a feature branch and open a pull request.
- **Contextual branch names**: Name branches after the work they carry, not with
  random words. Use a short `type/slug` form — e.g. `feat/rag-cues`,
  `fix/cue-dedup`, `docs/branch-naming` — so the branch is self-describing.

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
`api.yml`, `web.yml`) that runs its checks on PRs touching its dir (or the shared
workspace). Publishing is unified — one `release.yml` cuts a single
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
