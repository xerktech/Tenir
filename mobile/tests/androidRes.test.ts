import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Android resource XML is only parsed by AAPT during the release APK build —
// none of the PR-gate jobs touch it, so a malformed file sails through review
// and breaks `release.yml` instead. This guard failed the way v0.1.6's release
// did: a comment containing "--" (from a CSS variable name), which XML forbids.

const RES_DIR = resolve(process.cwd(), "android/app/src/main");

function xmlFilesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return xmlFilesUnder(path);
    return name.endsWith(".xml") ? [path] : [];
  });
}

describe("Android resource XML", () => {
  const files = xmlFilesUnder(RES_DIR);

  it("finds the resource files", () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it("never contains '--' inside XML comments (AAPT rejects it at release)", () => {
    for (const file of files) {
      const source = readFileSync(file, "utf8");
      for (const comment of source.matchAll(/<!--([\s\S]*?)-->/g)) {
        expect(comment[1], `${file}: ${comment[0].trim()}`).not.toContain("--");
      }
    }
  });
});
