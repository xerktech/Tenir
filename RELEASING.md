# Releasing Tenir

One release publishes **all four components** under a single
`v<MAJOR>.<MINOR>.<PATCH>` tag: the **api** image (which carries the built web
UI), the **vLLM STT** image, the Even **`.ehpk`**, and the Android **`.apk`**.
Driven by `.github/workflows/release.yml`; the logic lives in
`.github/scripts/` (see its README).

## How the version is decided

- The root `VERSION` file holds `MAJOR.MINOR` only.
- `PATCH` is derived from the existing `v<M>.<m>.<p>` tags — the max on the
  current line, plus one — and is **never committed**. That keeps the auto-patch
  path read-only against the repo, so it can't re-trigger itself.
- A minor/major bump is a deliberate manual act (see below) that edits `VERSION`.

There is no committed version-sync into the manifests. The two artifacts that
version their *installs* by content are stamped at build time by `release.yml`:
the Even `app.json` `version` (Even Hub keys sideloads on it) and the Android
`versionName` + packed `versionCode` (see `.github/scripts/version.js`). The
`package.json`/`pyproject.toml`/FastAPI-banner versions are ordinary dev metadata
and are not release-managed.

## Android signing

Every release APK is signed with the committed, stable release keystore
(`mobile/android/app/tenir-release.keystore`, alias `tenir`). This is deliberate:
Android refuses an update whose signing certificate differs from the installed
one, so a *stable* key is what lets a sideloaded install be **updated in place**
rather than uninstalled and reinstalled. For a self-hosted, sideloaded app that
shared key is the app's signing identity, not a Play Store upload secret, so it
is checked in on purpose (the `*.keystore` ignore rule has an explicit exception
for it in `mobile/.gitignore`).

If you ever distribute through the Play Store, generate a private upload keystore
and set `TENIR_UPLOAD_STORE_FILE` / `TENIR_UPLOAD_STORE_PASSWORD` /
`TENIR_UPLOAD_KEY_ALIAS` / `TENIR_UPLOAD_KEY_PASSWORD` as repository secrets;
`release.yml` passes them as `-P` props and `build.gradle` prefers them over the
committed default — no code change needed. Note that switching signing keys
(including the first release after this change, which moves off the old
per-runner debug key) requires one final uninstall/reinstall, because the
certificate itself changes; every update after that installs in place.

## Patch releases (automatic)

Every merge to `main` that touches a component's source cuts a patch release — a
merge that touches only docs or the release machinery does not, since every
component would be carried and the release would publish nothing new. The
trigger's path filter (kept in lockstep with `.github/scripts/changes.js` by
`changes.test.js`) is:

```
api/**  contract/**  packages/**  web/**  vllm-stt/**  even/**  mobile/**
package.json  package-lock.json
```

`plan` diffs the merge against the previous release tag and decides, per
component, **build or carry**:

- **Changed** components are rebuilt at the new version.
- **Unchanged** components are **carried**: their prior artifact is published in
  the new release *at its own prior version*, not rebuilt. A mobile-only merge
  builds the new `.apk` and copies the previous `.ehpk` (and references the
  previous api/vllm-stt image tags) onto the release unchanged.

Because Tenir is an npm-workspace monorepo, a single change can fan out: a
`contract/**` edit rebuilds the api image *and* both frontend bundles (it
regenerates both the Pydantic models and the TS types); a `packages/**`,
`web/**` or root-lockfile change rebuilds the api image too, because the api
image bakes in the built web SPA.

The release notes render a **rebuilt vs carried** table from the attached
`manifest.json`, which is the machine-readable source of truth.

Carried **images** are referenced in the manifest at their prior `:version` tag
(we do not retag an unchanged image to the new version — `:0.1.9` pointing at
`0.1.4` bits would be as misleading as renaming a carried asset). `:latest` is
already correct on a carried image, so the Compose stack needs nothing. Carried
**assets** are copied forward under their **original filename**, because Even Hub
and Android version an install by the version baked *inside* the file — the name
must describe the bits.

## Minor / major releases (manual)

Run the **release** workflow from the Actions tab with `release_type: minor` (or
`major`) and `dry_run: false`. This:

1. bumps `VERSION` and commits it,
2. rolls every patch since the last minor into a new `CHANGELOG.md` section
   (grouped by component, from the merged PRs), committed alongside,
3. **force-builds every component** at the new version — a minor is a coherent,
   blessed cut, so nothing is carried (a carried asset would ship a version that
   disagrees with the release).

`CHANGELOG.md` holds only these minor rollups; per-patch notes live on each
release's GitHub page.

## Dry run

`workflow_dispatch` defaults `dry_run: true`. A dry run computes the version and
build matrix against real tags, builds the images with `push: false` and the
assets to Actions artifacts, uploads the composed `manifest.json` + release notes
+ assets as **Actions artifacts**, and creates **no tag, release, or commit**.
Use it to rehearse a release (especially the first one, or after changing the
scripts) as many times as you like.

## The first unified release

Cut `v0.1.0` by hand: run the workflow with `release_type: patch`,
`dry_run: false`. With `VERSION=0.1` and no `v*` tags yet, `plan` force-builds
every component (no previous release to carry from). Watch it; keep
`gh release delete v0.1.0 --cleanup-tag` handy if you need to redo it. The
version guard (`assertStrictlyGreatest`) refuses any version at or below an
existing tag, so a fat-fingered `VERSION` can't regress a shipped channel.

The previous per-component channels (the `:latest`/`:<sha>` image tags and the
rolling `even-latest` / `mobile-latest` prereleases) are superseded by this
pipeline: images still publish `:latest` (plus `:<version>` and `:sha-<sha>`),
and the glasses/android builds now live on the versioned release instead of a
rolling prerelease.

## Known wrinkles

- **Rapid merges leave patch-number gaps.** The release workflow serializes
  (`concurrency` group `tenir-release`, `cancel-in-progress: false`) and
  supersedes older pending runs, so three merges in a burst may yield two
  releases. This is safe: the changelog is range-based (`prev_tag..HEAD`), so the
  surviving release still covers the superseded merges' PRs. Never make the
  changelog event-based.
- **A build that fails after the tag/images would leave partial state.** `publish`
  creates the tag last and only if no targeted build failed; a failed run creates
  no tag, so the next merge's diff still spans the failed range and the components
  just rebuild. The pipeline is self-healing by construction.
