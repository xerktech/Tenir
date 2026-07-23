// The path -> component map: the SINGLE source of truth for which components a
// set of changed files touches. Used by BOTH change-detection (which components
// to build vs carry) and changelog grouping (which heading a PR lands under), so
// the build matrix and the changelog can never disagree about what a change is.
// Pure — unit-tested in .github/scripts/tests/changes.test.js.

"use strict";

// The four release components. Tenir is an npm-workspace monorepo whose shared
// packages fan a single change out to several components:
//   - api/**       -> the api image (api/Dockerfile, repo-root build context).
//   - web/**       -> the web SPA is BAKED INTO the api image (a node build
//                     stage compiles @tenir/web and the python stage copies the
//                     dist into /srv/web), so a web change rebuilds the api
//                     image. There is no separate web image.
//   - contract/**  -> `make gen` writes BOTH the Pydantic models (api) and the
//                     TS types (packages/contract, which every client builds
//                     on), so a contract edit rebuilds the api image and both
//                     device clients.
//   - packages/**  -> the shared client-core/contract TS the web SPA (inside
//                     the api image) and the two device clients compile against.
//   - root package(-lock).json -> the workspace manifest/lockfile the clients
//                     (and the api image's web build stage) install from.
// Over-building a shared change wastes runner time; under-building ships a
// manifest that lies. We take the former.
const COMPONENTS = ["api", "parakeet-stt", "even", "mobile"];

// Directory prefixes. The prefixes are disjoint top-level dirs, but each maps to
// one OR MORE components (the fan-out above). Keep the mapping explicit rather
// than derived.
const PREFIX_MAP = [
  { prefix: "api/", components: ["api"] },
  { prefix: "contract/", components: ["api", "even", "mobile"] },
  { prefix: "packages/", components: ["api", "even", "mobile"] },
  { prefix: "web/", components: ["api"] },
  { prefix: "parakeet-stt/", components: ["parakeet-stt"] },
  { prefix: "even/", components: ["even"] },
  { prefix: "mobile/", components: ["mobile"] },
];

// Repo-root files (no dir prefix) that still touch components: the npm workspace
// manifest + lockfile the clients (and the api image's SPA stage) install from.
const FILE_MAP = {
  "package.json": ["api", "even", "mobile"],
  "package-lock.json": ["api", "even", "mobile"],
};

// Which components a single changed path touches. Anything matching no prefix or
// file (VERSION, CHANGELOG.md, .github/**, README.md, docker-compose.yml,
// schema.sql, Makefile, ...) returns [] and is surfaced as "Other" in the
// changelog — never dropped, never a build.
function componentsForPath(p) {
  const s = String(p).replace(/^\.?\/+/, "");
  for (const { prefix, components } of PREFIX_MAP) {
    if (s.startsWith(prefix)) return components.slice();
  }
  if (FILE_MAP[s]) return FILE_MAP[s].slice();
  return [];
}

// Map a list of changed paths to a {component: bool} record over ALL components.
// forceAll (first release, minor/major, or the explicit dispatch input) marks
// everything changed regardless of the diff.
function detectChanges(paths, opts) {
  const forceAll = !!(opts && opts.forceAll);
  const changed = {};
  for (const c of COMPONENTS) changed[c] = forceAll;
  if (forceAll) return changed;
  for (const p of paths) {
    for (const c of componentsForPath(p)) changed[c] = true;
  }
  return changed;
}

// The distinct source paths a release can trigger on. release.yml's push:main
// filter has to restate these as globs (a trigger can't call into JS); this is
// what its test asserts against, so the two can't drift apart silently. Dir
// prefixes become `<prefix>**`; the two root files are listed verbatim.
function componentTriggerPaths() {
  return [...PREFIX_MAP.map(({ prefix }) => `${prefix}**`), ...Object.keys(FILE_MAP)];
}

module.exports = { COMPONENTS, componentsForPath, detectChanges, componentTriggerPaths };
