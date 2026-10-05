import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Source-level check (the RN screens aren't rendered in this jsdom suite, mirroring
// translation.test.ts). Guards XERK-1498: while speech-to-text runs behind real time
// the Live screen says so, with the same "captions delayed" pill as web and the lens.
const readText = (rel: string) => readFileSync(resolve(process.cwd(), rel)).toString("utf8");

describe("captions delayed (XERK-1498)", () => {
  it("Live: shows the captions-delayed pill while the session reports a lag", () => {
    const src = readText("src/screens/Live.tsx");
    expect(src).toMatch(
      /state\.running && state\.captionsDelayed && <Badge tone="neutral">captions delayed<\/Badge>/,
    );
  });
});
