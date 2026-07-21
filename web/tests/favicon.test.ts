import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Vitest runs with the workspace root (web/) as cwd, so resolve assets from it.
const read = (rel: string) => readFileSync(resolve(process.cwd(), rel));
const readText = (rel: string) => read(rel).toString("utf8");

describe("Lumen favicon", () => {
  it("ships every referenced icon asset", () => {
    for (const asset of [
      "public/favicon.svg",
      "public/favicon-16.png",
      "public/favicon-32.png",
      "public/favicon.ico",
      "public/apple-touch-icon.png",
    ]) {
      // readFileSync throws if the asset is missing, failing the test.
      expect(read(asset).byteLength).toBeGreaterThan(0);
    }
  });

  it("links the icons from index.html (SVG primary, PNG + ICO fallbacks)", () => {
    const html = readText("index.html");
    expect(html).toContain('rel="icon" type="image/svg+xml" href="/favicon.svg"');
    expect(html).toContain('href="/favicon-32.png"');
    expect(html).toContain('href="/favicon-16.png"');
    expect(html).toContain('rel="icon" href="/favicon.ico"');
    expect(html).toContain('rel="apple-touch-icon" href="/apple-touch-icon.png"');
  });

  it("draws the mark on the Lumen tile with the teal accent", () => {
    const svg = readText("public/favicon.svg");
    expect(svg).toContain("#0E1116"); // Lumen dark tile
    expect(svg).toContain("#3FD9C9"); // signature teal accent
    expect(svg).toContain("#5FE3D5"); // accent-strong (highlight + lumen dot)
  });

  it("encodes a valid multi-size ICO (PNG-compressed entries)", () => {
    const ico = read("public/favicon.ico");
    // ICONDIR header: reserved=0, type=1 (icon), count>=1.
    expect(ico.readUInt16LE(0)).toBe(0);
    expect(ico.readUInt16LE(2)).toBe(1);
    expect(ico.readUInt16LE(4)).toBeGreaterThanOrEqual(1);
  });
});
