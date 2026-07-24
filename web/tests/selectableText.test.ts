import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Vitest runs with the workspace root (web/) as cwd. jsdom does not compute
// stylesheet rules, so this guards XERK-104 at the source: conversation and
// history transcript text must opt into `user-select: text` so it can be copied
// (in parity with the mobile `selectable` prop and the even client's CSS).
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("selectable transcript text (XERK-104)", () => {
  const css = readText("src/styles.css");

  it("makes the live transcript, history segments and cue bodies selectable", () => {
    // One rule lists all four conversation-text surfaces and sets both the
    // standard and the -webkit- prefixed property to `text`.
    expect(css).toMatch(
      /\.transcript li,\s*\.item,\s*\.cue-card-body,\s*\.modal-body\s*\{\s*user-select:\s*text;\s*-webkit-user-select:\s*text;\s*\}/,
    );
  });
});
