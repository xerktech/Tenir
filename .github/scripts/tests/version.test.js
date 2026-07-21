// Unit tests for version.js (node:test, built-in — matches the repo's
// zero-npm-dependency stance). Run by release-scripts.yml:
//   node --test .github/scripts/tests/*.test.js

"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const V = require("../version.js");

test("parseBase accepts MAJOR.MINOR, rejects everything else", () => {
  assert.deepEqual(V.parseBase("0.1"), { major: 0, minor: 1 });
  assert.deepEqual(V.parseBase(" 12.7\n"), { major: 12, minor: 7 });
  assert.throws(() => V.parseBase("0.1.1"));
  assert.throws(() => V.parseBase("0"));
  assert.throws(() => V.parseBase("x.y"));
  assert.throws(() => V.parseBase(""));
});

test("parseTag parses v-prefixed and bare triples, null otherwise", () => {
  assert.deepEqual(V.parseTag("v0.1.9"), { major: 0, minor: 1, patch: 9 });
  assert.deepEqual(V.parseTag("0.1.9"), { major: 0, minor: 1, patch: 9 });
  assert.equal(V.parseTag("even-latest"), null);
  assert.equal(V.parseTag("mobile-latest"), null);
  assert.equal(V.parseTag("v0.1"), null);
  assert.equal(V.parseTag("nonsense"), null);
});

test("nextPatch: empty -> 0, max+1 on the matching line, numeric not lexical", () => {
  assert.equal(V.nextPatch([], { major: 0, minor: 1 }), 0);
  assert.equal(V.nextPatch(["v0.1.0", "v0.1.1"], { major: 0, minor: 1 }), 2);
  // numeric: v0.1.10 > v0.1.9 (lexical would pick 9 -> 10)
  assert.equal(V.nextPatch(["v0.1.9", "v0.1.10"], { major: 0, minor: 1 }), 11);
  // ignores other minor lines
  assert.equal(V.nextPatch(["v0.0.99", "v0.2.5"], { major: 0, minor: 1 }), 0);
  // ignores non-semver rolling tags
  assert.equal(V.nextPatch(["even-latest", "mobile-latest"], { major: 0, minor: 1 }), 0);
  // ignores malformed
  assert.equal(V.nextPatch(["v0.1.x", "v0.1.2"], { major: 0, minor: 1 }), 3);
});

test("bumpBase resets the lower fields correctly", () => {
  assert.deepEqual(V.bumpBase({ major: 0, minor: 1 }, "patch"), { major: 0, minor: 1 });
  assert.deepEqual(V.bumpBase({ major: 0, minor: 1 }, "minor"), { major: 0, minor: 2 });
  assert.deepEqual(V.bumpBase({ major: 0, minor: 1 }, "major"), { major: 1, minor: 0 });
});

test("androidVersionCode packs readably and monotonically", () => {
  assert.equal(V.androidVersionCode({ major: 0, minor: 1, patch: 0 }), 10000);
  assert.equal(V.androidVersionCode({ major: 0, minor: 1, patch: 7 }), 10007);
  assert.equal(V.androidVersionCode({ major: 1, minor: 2, patch: 15 }), 1020015);
});

test("androidVersionCode is strictly increasing across a semver-sorted sweep", () => {
  const seq = [
    { major: 0, minor: 1, patch: 32 },
    { major: 0, minor: 2, patch: 0 },
    { major: 0, minor: 2, patch: 9999 },
    { major: 0, minor: 99, patch: 9999 },
    { major: 1, minor: 0, patch: 0 },
    { major: 1999, minor: 99, patch: 9999 },
  ];
  let prev = -1;
  for (const v of seq) {
    const code = V.androidVersionCode(v);
    assert.ok(code > prev, `${V.format(v)} -> ${code} must exceed prev ${prev}`);
    assert.ok(code <= 2147483647, `${code} must fit in Int`);
    prev = code;
  }
});

test("androidVersionCode throws at each field's budget, never wraps silently", () => {
  assert.throws(() => V.androidVersionCode({ major: 0, minor: 1, patch: 10000 }));
  assert.throws(() => V.androidVersionCode({ major: 0, minor: 100, patch: 0 }));
  assert.throws(() => V.androidVersionCode({ major: 2000, minor: 0, patch: 0 }));
});

test("assertStrictlyGreatest rejects a regression against legacy namespaces", () => {
  const legacy = ["even-v0.1.0", "mobile-v0.1.0", "v0.1.2"];
  assert.doesNotThrow(() => V.assertStrictlyGreatest({ major: 0, minor: 1, patch: 3 }, legacy));
  // 0.1.2 already exists -> not strictly greater
  assert.throws(() => V.assertStrictlyGreatest({ major: 0, minor: 1, patch: 2 }, legacy));
  // 0.1.0 is at/below a published legacy tag -> the regression the guard blocks
  assert.throws(() => V.assertStrictlyGreatest({ major: 0, minor: 1, patch: 0 }, ["even-v0.1.5"]));
});
