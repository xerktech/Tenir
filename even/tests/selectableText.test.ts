import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// jsdom does not compute stylesheet rules, so this guards XERK-104 at the
// source: the phone-side conversation and history transcript text must opt into
// `user-select: text` so it can be long-pressed and copied inside the Even Hub
// WebView (which can default the document to non-selectable). In parity with the
// web client's CSS and the mobile app's `selectable` prop.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("selectable transcript text (XERK-104)", () => {
  const html = readText("index.html");

  it("makes the session transcript, history segments and cue bodies selectable", () => {
    // One rule lists all four conversation-text surfaces and sets both the
    // standard and the -webkit- prefixed property to `text`.
    expect(html).toMatch(
      /\.transcript li,\s*\.item,\s*\.session-cue-body,\s*\.cue-popup-body\s*\{\s*user-select:\s*text;\s*-webkit-user-select:\s*text;\s*\}/,
    );
  });
});
