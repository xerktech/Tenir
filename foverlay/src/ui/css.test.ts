/**
 * Stylesheet contract tests (no DOM in bun test, so assert on the CSS text).
 *
 * XERK-216 regression: the page swaps every view by toggling the `hidden`
 * attribute, but author `display:` rules (e.g. `#login { display: flex }`)
 * override the UA's built-in `[hidden] { display: none }`. Without an explicit
 * guard rule a successful login cleared the password yet left the login card
 * on screen. The guard must be `!important` so no display rule can beat it.
 */

import { describe, expect, it } from "bun:test";

import { readFileSync } from "node:fs";

const css = readFileSync(new URL("./index.css", import.meta.url), "utf8");

describe("index.css", () => {
  it("hides [hidden] elements regardless of their display rules", () => {
    expect(css).toMatch(/\[hidden\]\s*\{[^}]*display:\s*none\s*!important/);
  });
});
