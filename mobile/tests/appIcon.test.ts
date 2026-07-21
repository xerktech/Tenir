import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Vitest runs with the workspace root (mobile/) as cwd, so resolve from it.
const readText = (rel: string) =>
  readFileSync(resolve(process.cwd(), rel)).toString("utf8");

const ICON = "android/app/src/main/res/drawable/ic_launcher.xml";

describe("Android launcher icon", () => {
  it("is a vector drawable matching the Lumen favicon palette", () => {
    const xml = readText(ICON);
    expect(xml).toContain("<vector");
    expect(xml).toContain("#0E1116"); // Lumen dark tile (matches the favicon)
    expect(xml).toContain("#3FD9C9"); // signature teal accent
    expect(xml).toContain("#5FE3D5"); // accent-strong (T highlight + lumen dot)
  });

  it("is wired as the app icon and round icon in the manifest", () => {
    const manifest = readText("android/app/src/main/AndroidManifest.xml");
    expect(manifest).toContain('android:icon="@drawable/ic_launcher"');
    expect(manifest).toContain('android:roundIcon="@drawable/ic_launcher"');
  });
});
