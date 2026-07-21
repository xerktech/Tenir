"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const C = require("../changes.js");

test("componentsForPath maps top-level dirs; shared dirs fan out", () => {
  assert.deepEqual(C.componentsForPath("api/src/api/main.py"), ["api"]);
  // web/** is baked into the api image (no separate web image/component).
  assert.deepEqual(C.componentsForPath("web/src/App.tsx"), ["api"]);
  assert.deepEqual(C.componentsForPath("web/Dockerfile"), ["api"]);
  // contract/** regenerates the Pydantic models (api) AND the TS types (clients).
  assert.deepEqual(C.componentsForPath("contract/ws-messages.schema.json"), ["api", "even", "mobile"]);
  // packages/** is the shared TS the web SPA (in the api image) and the two
  // device clients compile against.
  assert.deepEqual(C.componentsForPath("packages/client-core/src/x.ts"), ["api", "even", "mobile"]);
  assert.deepEqual(C.componentsForPath("even/src/app.ts"), ["even"]);
  assert.deepEqual(C.componentsForPath("mobile/android/app/build.gradle"), ["mobile"]);
  assert.deepEqual(C.componentsForPath("vllm-stt/Dockerfile"), ["vllm-stt"]);
});

test("componentsForPath maps the root workspace manifest/lockfile to api + clients", () => {
  assert.deepEqual(C.componentsForPath("package.json"), ["api", "even", "mobile"]);
  assert.deepEqual(C.componentsForPath("package-lock.json"), ["api", "even", "mobile"]);
  // a nested package.json is owned by its dir, not the root rule
  assert.deepEqual(C.componentsForPath("web/package.json"), ["api"]);
});

test("componentsForPath returns [] for non-component paths (-> Other, never a build)", () => {
  assert.deepEqual(C.componentsForPath("VERSION"), []);
  assert.deepEqual(C.componentsForPath("CHANGELOG.md"), []);
  assert.deepEqual(C.componentsForPath(".github/workflows/release.yml"), []);
  assert.deepEqual(C.componentsForPath("docker-compose.yml"), []);
  assert.deepEqual(C.componentsForPath("./README.md"), []);
});

test("detectChanges unions components across the diff", () => {
  const changed = C.detectChanges(["api/src/api/main.py", "even/src/app.ts", "README.md"], {});
  assert.deepEqual(changed, {
    api: true,
    "vllm-stt": false,
    even: true,
    mobile: false,
  });
});

test("a web-only change rebuilds the api image (the SPA is baked in)", () => {
  const changed = C.detectChanges(["web/src/App.tsx"], {});
  assert.deepEqual(changed, {
    api: true,
    "vllm-stt": false,
    even: false,
    mobile: false,
  });
});

test("detectChanges forceAll marks every component regardless of paths", () => {
  const changed = C.detectChanges([], { forceAll: true });
  for (const c of C.COMPONENTS) assert.equal(changed[c], true);
});

test("detectChanges with no matching paths builds nothing", () => {
  const changed = C.detectChanges(["VERSION", "CHANGELOG.md"], {});
  for (const c of C.COMPONENTS) assert.equal(changed[c], false);
});

// release.yml's `push:` filter has to restate the trigger paths as globs,
// because a workflow trigger can't call into JS — the gate that decides whether
// a merge starts a release at all is the one part of the path->component map
// that lives outside this file. Drift is silent and one-directional: a component
// dir added here but not there just never auto-releases, which looks exactly
// like the release pipeline working fine until someone checks the tags.
test("release.yml's push paths cover exactly the component trigger paths", () => {
  const yml = fs.readFileSync(
    path.join(__dirname, "..", "..", "workflows", "release.yml"),
    "utf8",
  );
  const block = yml.match(/\n {2}push:\n {4}branches: \[main\]\n {4}paths:\n((?: {6}- ".*"\n)+)/);
  assert.ok(block, "release.yml has no push:main paths: block in the expected shape");

  const globs = [...block[1].matchAll(/- "(.*)"/g)].map((m) => m[1]);
  const expected = C.componentTriggerPaths();
  assert.deepEqual(globs.slice().sort(), expected.slice().sort());
});
