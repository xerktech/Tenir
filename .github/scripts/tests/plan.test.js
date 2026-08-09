// plan.js's OUTPUTS, which nothing tested — so the CHANGELOG bug it carries
// could walk straight back in (XERK-236).
//
// plan.js is a script, not a module: it reads VERSION and shells out to git.
// Run it as a subprocess in a scratch git repo with real tags, and read the
// key=value pairs it writes to $GITHUB_OUTPUT.

"use strict";

const assert = require("node:assert");
const { test } = require("node:test");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const PLAN = path.join(__dirname, "..", "plan.js");

function runPlan(releaseType, baseVersion = "0.6", tags = ["v0.6.0", "v0.6.1"]) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tenir-plan-"));
  const git = (...args) =>
    execFileSync("git", args, { cwd: dir, stdio: ["ignore", "pipe", "pipe"] });
  git("init", "-q", "-b", "main");
  git("config", "user.email", "t@t.test");
  git("config", "user.name", "t");
  fs.writeFileSync(path.join(dir, "VERSION"), baseVersion);
  fs.mkdirSync(path.join(dir, "api"), { recursive: true });
  fs.writeFileSync(path.join(dir, "api", "x.py"), "x = 1\n");
  git("add", "-A");
  git("commit", "-qm", "seed");
  for (const t of tags) git("tag", t);
  // A commit after the last tag so the diff range is non-empty.
  fs.writeFileSync(path.join(dir, "api", "x.py"), "x = 2\n");
  git("add", "-A");
  git("commit", "-qm", "later work");

  const outFile = path.join(dir, "gh-output");
  fs.writeFileSync(outFile, "");
  execFileSync(process.execPath, [PLAN], {
    cwd: dir,
    env: { ...process.env, RELEASE_TYPE: releaseType, GITHUB_OUTPUT: outFile },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const out = {};
  for (const line of fs.readFileSync(outFile, "utf8").split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) out[line.slice(0, i)] = line.slice(i + 1);
  }
  fs.rmSync(dir, { recursive: true, force: true });
  return out;
}

test("a patch release stays on its line, and closes the line it is on", () => {
  const out = runPlan("patch");
  assert.equal(out.version, "0.6.2");
  assert.equal(out.base_major, "0");
  assert.equal(out.base_minor, "6");
  // For a patch the two agree — the line being opened IS the line being closed.
  assert.equal(out.prev_base_major, "0");
  assert.equal(out.prev_base_minor, "6");
});

test("a minor release opens the NEXT line but closes the CURRENT one", () => {
  // This is the whole bug: release.yml fed base_* to the CHANGELOG rollup, so
  // the start tag was v0.7.0 — the line being opened, which does not exist yet
  // — the range came back empty and every rollup rendered "_No changes._".
  const out = runPlan("minor");
  assert.equal(out.version, "0.7.0");
  assert.equal(out.base_major, "0");
  assert.equal(out.base_minor, "7"); // VERSION gets this
  assert.equal(out.prev_base_major, "0");
  assert.equal(out.prev_base_minor, "6"); // the rollup gets this
});

test("a major release closes the previous minor line", () => {
  const out = runPlan("major");
  assert.equal(out.version, "1.0.0");
  assert.equal(out.base_major, "1");
  assert.equal(out.base_minor, "0");
  assert.equal(out.prev_base_major, "0");
  assert.equal(out.prev_base_minor, "6");
});

test("the rollup's start tag is one that actually exists", () => {
  // The property that matters, stated directly: `v<prev_base>.0` must be a real
  // tag, or changelog-cli falls back to an empty range and says "_No changes._".
  for (const type of ["patch", "minor", "major"]) {
    const out = runPlan(type);
    assert.equal(
      `v${out.prev_base_major}.${out.prev_base_minor}.0`,
      "v0.6.0",
      `${type}: rollup must start at the existing tag`,
    );
  }
});
