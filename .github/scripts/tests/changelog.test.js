"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const CL = require("../changelog.js");

const entry = (o) => Object.assign({ title: "x", url: null, number: null, author: null, components: [] }, o);

test("buildEntries matches merge-commit and squash oids to PRs; orphans synthesize from subject", () => {
  const commits = [
    { oid: "m1", subject: "Merge pull request #201 from x/y" }, // merge-commit shape
    { oid: "s1", subject: "Squashed title (#202)" }, // squash shape (oid IS the mergeCommit)
    { oid: "d1", subject: "direct hotfix to main" }, // orphan: no PR
  ];
  const prs = [
    { number: 201, title: "Tighten cue dedup", url: "http://x/201", author: "malc", mergeCommitOid: "m1" },
    { number: 202, title: "Add web settings pane", url: "http://x/202", author: "malc", mergeCommitOid: "s1" },
  ];
  const componentsByOid = { m1: [], s1: ["api"], d1: ["api", "even"] };
  const entries = CL.buildEntries({ commits, prs, componentsByOid });
  assert.equal(entries[0].number, 201);
  assert.equal(entries[0].title, "Tighten cue dedup");
  assert.equal(entries[1].number, 202);
  assert.deepEqual(entries[1].components, ["api"]);
  // orphan: kept, synthesized from the commit subject, no PR metadata
  assert.equal(entries[2].number, null);
  assert.equal(entries[2].title, "direct hotfix to main");
  assert.deepEqual(entries[2].components, ["api", "even"]);
});

test("groupByComponent groups by heading, [] into Other; no double-counting", () => {
  const groups = CL.groupByComponent([
    entry({ title: "even thing", components: ["even"] }),
    entry({ title: "api thing", components: ["api", "api"] }),
    entry({ title: "chore", components: [] }),
  ]);
  assert.deepEqual(Object.keys(groups), ["API", "Even", "Other"]);
  assert.equal(groups.API.length, 1); // not double-counted within one entry
});

test("a multi-component entry appears under each heading it touches", () => {
  const groups = CL.groupByComponent([entry({ title: "shared", components: ["api", "even"] })]);
  assert.equal(groups.API.length, 1);
  assert.equal(groups.Even.length, 1);
});

test("a removed component name (web/sidecar-gpu) has no heading and lands in Other", () => {
  const groups = CL.groupByComponent([entry({ title: "stale", components: ["web", "sidecar-gpu"] })]);
  assert.deepEqual(Object.keys(groups), ["Other"]);
});

test("an entry matching no component is never dropped (lands in Other)", () => {
  const groups = CL.groupByComponent([entry({ title: "root README tweak", components: [] })]);
  assert.deepEqual(Object.keys(groups), ["Other"]);
});

test("renderEntryLine formats PR and orphan entries", () => {
  assert.equal(
    CL.renderEntryLine(entry({ title: "Add flag", number: 42, url: "http://x/42", author: "malc" })),
    "- Add flag ([#42](http://x/42)) @malc",
  );
  assert.equal(CL.renderEntryLine(entry({ title: "direct push", number: null })), "- direct push");
});

test("sanitizeTitle collapses newlines/whitespace so a title can't break markdown", () => {
  assert.equal(CL.sanitizeTitle("first line\nsecond"), "first line");
  assert.equal(CL.sanitizeTitle("  a   b  "), "a b");
  const line = CL.renderEntryLine(entry({ title: "weird | title\nwith break", number: 1 }));
  assert.ok(!line.includes("\n"));
});

test("renderComponentTable reads rebuilt/carried status straight from the manifest", () => {
  const manifest = {
    components: {
      api: { version: "0.1.1", kind: "image", ref: "ghcr.io/x/tenir:0.1.1", built: true },
      "parakeet-stt": { version: "0.1.0", kind: "image", ref: "ghcr.io/x/tenir-parakeet-stt:0.1.0", built: false },
      even: { version: "0.1.0", kind: "evenhub", package_id: "com.tenir.local", built: false },
      mobile: { version: "0.1.1", kind: "asset", asset: "tenir-android-v0.1.1.apk", version_code: 10001, built: true },
    },
  };
  const table = CL.renderComponentTable(manifest);
  assert.match(table, /API \(image, incl\. web UI\) \| 0\.1\.1 \| rebuilt/);
  assert.match(table, /Parakeet STT \(image\) \| 0\.1\.0 \| carried/);
  assert.match(table, /Even \(Even Hub\) \| 0\.1\.0 \| carried \| `com\.tenir\.local` \(Even Hub portal\)/);
  assert.match(table, /code 10001/);
  assert.ok(!table.includes("tenir-web"));
  assert.ok(!table.includes("sidecar"));
});

test("renderReleaseNotes handles an empty range", () => {
  const notes = CL.renderReleaseNotes({ version: "0.1.1", groups: {}, manifest: { components: {} } });
  assert.match(notes, /## Tenir v0\.1\.1/);
  assert.match(notes, /_No component changes in range\._/);
});

test("insertSection prepends under the marker and is idempotent for the same version", () => {
  const base = `# Changelog\n\n${CL.CHANGELOG_MARKER}\n`;
  const secA = CL.renderChangelogSection({ version: "0.1.0", date: "2026-07-19", groups: { Even: [entry({ title: "a", components: ["even"] })] } });
  const once = CL.insertSection(base, secA);
  assert.match(once, /## 0\.1\.0 — 2026-07-19/);

  const secB = CL.renderChangelogSection({ version: "0.2.0", date: "2026-08-01", groups: { API: [entry({ title: "b", components: ["api"] })] } });
  const two = CL.insertSection(once, secB);
  // newest first: 0.2.0 above 0.1.0
  assert.ok(two.indexOf("## 0.2.0") < two.indexOf("## 0.1.0"));

  // re-inserting 0.2.0 must not duplicate it
  const three = CL.insertSection(two, secB);
  const count = (three.match(/## 0\.2\.0 —/g) || []).length;
  assert.equal(count, 1);
});
