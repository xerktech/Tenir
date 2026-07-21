"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const M = require("../manifest.js");

const ALL_CHANGED = {
  api: true,
  "vllm-stt": true,
  even: true,
  mobile: true,
};

function firstRelease() {
  return M.buildManifest({
    version: "0.1.0",
    tag: "v0.1.0",
    commit: "aaa",
    releasedAt: "2026-07-19T00:00:00Z",
    changed: ALL_CHANGED,
    prevManifest: null,
    androidVersionCode: 10000,
    owner: "xerktech",
  });
}

test("first release: every component fresh and built", () => {
  const m = firstRelease();
  assert.equal(m.schema, 1);
  assert.equal(m.components.api.ref, "ghcr.io/xerktech/tenir:0.1.0");
  assert.equal(m.components["vllm-stt"].ref, "ghcr.io/xerktech/tenir-vllm-stt:0.1.0");
  assert.equal(m.components.even.asset, "tenir-even-v0.1.0.ehpk");
  assert.equal(m.components.mobile.asset, "tenir-android-v0.1.0.apk");
  assert.equal(m.components.mobile.version_code, 10000);
  for (const c of Object.values(m.components)) assert.equal(c.built, true);
});

test("removed components (web, sidecar-gpu) are not manifest components", () => {
  const m = firstRelease();
  assert.equal(m.components.web, undefined);
  assert.equal(m.components["sidecar-gpu"], undefined);
  assert.throws(() => M.freshComponent("web", "0.1.0", "v0.1.0", {}));
  assert.throws(() => M.freshComponent("sidecar-gpu", "0.1.0", "v0.1.0", {}));
});

test("carried image keeps its OLDER version and ref; never retagged to the new version", () => {
  const prev = firstRelease();
  const m = M.buildManifest({
    version: "0.1.1",
    tag: "v0.1.1",
    commit: "bbb",
    releasedAt: "2026-07-20T00:00:00Z",
    changed: { api: false, "vllm-stt": true, even: false, mobile: false },
    prevManifest: prev,
    androidVersionCode: 10001,
  });
  assert.equal(m.components["vllm-stt"].version, "0.1.1"); // rebuilt
  assert.equal(m.components["vllm-stt"].built, true);
  assert.equal(m.components.api.version, "0.1.0"); // carried, older
  assert.equal(m.components.api.ref, "ghcr.io/xerktech/tenir:0.1.0");
  assert.equal(m.components.api.built, false);
});

test("carried asset keeps its name/version but re-points release_tag to the new release", () => {
  const prev = firstRelease();
  const m = M.buildManifest({
    version: "0.1.1",
    tag: "v0.1.1",
    commit: "bbb",
    releasedAt: "2026-07-20T00:00:00Z",
    changed: { api: true, "vllm-stt": true, even: false, mobile: false },
    prevManifest: prev,
    androidVersionCode: 10001,
  });
  const e = m.components.even;
  assert.equal(e.version, "0.1.0"); // still the build it actually is
  assert.equal(e.asset, "tenir-even-v0.1.0.ehpk"); // name describes the bits
  assert.equal(e.release_tag, "v0.1.1"); // but it now also lives on this release
  assert.equal(e.built, false);
});

test("unchanged component absent from prev manifest throws (never emit a hole)", () => {
  const prev = firstRelease();
  delete prev.components.even;
  assert.throws(() =>
    M.buildManifest({
      version: "0.1.1",
      tag: "v0.1.1",
      commit: "bbb",
      releasedAt: "2026-07-20T00:00:00Z",
      changed: { api: true, "vllm-stt": true, even: false, mobile: true },
      prevManifest: prev,
      androidVersionCode: 10001,
    }),
  );
});

test("carryPlan emits copy-asset only for carried assets, not images or built ones", () => {
  const prev = firstRelease();
  const m = M.buildManifest({
    version: "0.1.1",
    tag: "v0.1.1",
    commit: "bbb",
    releasedAt: "2026-07-20T00:00:00Z",
    changed: { api: true, "vllm-stt": false, even: false, mobile: false },
    prevManifest: prev,
    androidVersionCode: 10001,
  });
  const plan = M.carryPlan(m, prev);
  const components = plan.map((a) => a.component).sort();
  // vllm-stt carried but it's an image -> no copy action. even/mobile are carried assets.
  assert.deepEqual(components, ["even", "mobile"]);
  const evenAction = plan.find((a) => a.component === "even");
  assert.deepEqual(evenAction, {
    component: "even",
    action: "copy-asset",
    asset: "tenir-even-v0.1.0.ehpk",
    from_release: "v0.1.0",
    to_release: "v0.1.1",
  });
});

test("carryPlan is empty when everything was rebuilt", () => {
  const prev = firstRelease();
  const m = M.buildManifest({
    version: "0.1.1",
    tag: "v0.1.1",
    commit: "bbb",
    releasedAt: "2026-07-20T00:00:00Z",
    changed: ALL_CHANGED,
    prevManifest: prev,
    androidVersionCode: 10001,
  });
  assert.deepEqual(M.carryPlan(m, prev), []);
});
