/**
 * Stylesheet contract tests (no DOM in bun test, so assert on the CSS text).
 *
 * XERK-216 regression: the page swaps every view by toggling the `hidden`
 * attribute, but author `display:` rules (e.g. `#login { display: flex }`)
 * override the UA's built-in `[hidden] { display: none }`. Without an explicit
 * guard rule a successful login cleared the password yet left the login card
 * on screen. The guard must be `!important` so no display rule can beat it.
 *
 * XERK-237: the rest of this file guards the Even-parity port. The phone page
 * is meant to be indistinguishable from `even/index.html`'s, so the things that
 * silently drifted last time — the design tokens, the light palette, the
 * bundled faces — are asserted rather than trusted.
 */

import { describe, expect, it } from "bun:test";

import { readFileSync } from "node:fs";

const css = readFileSync(new URL("./index.css", import.meta.url), "utf8");
const html = readFileSync(new URL("./index.html", import.meta.url), "utf8");

describe("index.css", () => {
  it("hides [hidden] elements regardless of their display rules", () => {
    expect(css).toMatch(/\[hidden\]\s*\{[^}]*display:\s*none\s*!important/);
  });

  // The Lumen tokens upstream's stylesheet is built on. A palette invented
  // locally is exactly how the two front ends drifted apart before.
  it("carries the upstream Lumen token set", () => {
    for (const token of [
      "--space-1", "--space-8",
      "--text-xs", "--text-2xl",
      "--font-sans", "--font-display",
      "--radius-sm", "--radius-lg", "--shadow-sm", "--shadow-md",
      "--bg", "--surface", "--surface-raised", "--border", "--border-strong",
      "--text", "--text-muted",
      "--accent", "--accent-strong", "--accent-ink",
      "--focus-ring", "--badge-fill", "--badge-border",
      "--danger", "--danger-ink", "--danger-border", "--danger-wash",
      "--nav-veil",
    ]) {
      expect(css).toContain(`${token}:`);
    }
  });

  it("uses upstream's accent and surface values, not a local palette", () => {
    expect(css).toContain("#3FD9C9"); // dark accent
    expect(css).toContain("#0E1116"); // dark bg
    expect(css).toContain("#0E8C7E"); // light accent
    expect(css).toContain("#F7F9FB"); // light bg
  });

  // Upstream follows `prefers-color-scheme`; this host also reports its own
  // choice. Both paths must paint the light palette, or the page is dark-only
  // on a light phone — which is how it shipped before XERK-237.
  it("has a light palette on both the media query and the host scheme", () => {
    expect(css).toMatch(/@media \(prefers-color-scheme: light\)/);
    expect(css).toMatch(/:root\[data-theme="light"\]/);
    // The dark default must not be overridden by a stale data-theme.
    expect(css).toMatch(/:root:not\(\[data-theme="dark"\]\)/);
  });

  // The faces are bundled, not assumed: the WebView loads from file:// with no
  // network, so an unbundled font silently falls back to the system sans.
  it("bundles the Inter and Space Grotesk faces the design system names", () => {
    expect(css).toContain('font-family: "Inter"');
    expect(css).toContain('font-family: "Space Grotesk"');
    for (const weight of ["400", "500", "600"]) {
      expect(css).toContain(`inter-latin-${weight}-normal.woff2`);
    }
    for (const weight of ["500", "600"]) {
      expect(css).toContain(`space-grotesk-latin-${weight}-normal.woff2`);
    }
  });

  it("pads every host-inset edge with the safe-area vars", () => {
    for (const v of ["--mentra-safe-top", "--mentra-safe-bottom", "--mentra-safe-left", "--mentra-safe-right"]) {
      expect(css).toContain(v);
    }
  });
});

describe("index.html", () => {
  // The structural pieces that were missing or renamed before XERK-237. Each
  // one is something a wearer sees; a rename here is a visible divergence.
  it("carries upstream's shell, wordmark and icon navigation", () => {
    for (const marker of [
      'class="wordmark"',
      'class="wordmark-dot"',
      'class="bar"',
      'class="content"',
      'class="nav-tabs"',
      'class="nav-item',
      "<svg", // the mic / clock tab icons
      "Signed in as",
      ">Log out<",
      ">Log in<",
    ]) {
      expect(html).toContain(marker);
    }
  });

  it("keeps the login error on the toast, as upstream does", () => {
    expect(html).toMatch(/class="toast err" id="login-error"/);
    expect(html).toMatch(/class="toast err" id="app-toast"/);
  });

  it("has the history audio player and the cue-detail popup", () => {
    expect(html).toContain('id="history-audio"');
    expect(html).toContain('id="history-audio-el"');
    expect(html).toContain('id="history-cue-popup"');
    expect(html).toContain('id="history-cue-popup-source"');
  });

  // Upstream renders the song card ABOVE the cue card; the port had them the
  // other way round, so a song and a cue swapped places on screen.
  it("renders the song card above the cue card", () => {
    expect(html.indexOf('id="session-song"')).toBeLessThan(html.indexOf('id="session-cue"'));
  });
});
