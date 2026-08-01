import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Vitest runs with the workspace root (mobile/) as cwd, so resolve from it.
const readText = (rel: string) =>
  readFileSync(resolve(process.cwd(), rel)).toString("utf8");

const ICON = "android/app/src/main/res/drawable/ic_launcher.xml";
const NOTIFICATION_ICON = "android/app/src/main/res/drawable/ic_notification.xml";
const MIC_SERVICE =
  "android/app/src/main/java/com/tenir/pcmaudio/MicForegroundService.kt";

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

// XERK-165: the session notification uses the Tenir mark instead of Android's
// stock ic_btn_speak_now microphone. A status-bar small icon is drawn from the
// alpha channel and tinted by the system, so it must be a transparent-background
// silhouette of the glyph — not the full-colour launcher icon (whose opaque dark
// tile would render as a solid white square).
describe("Android notification icon", () => {
  it("is a transparent silhouette of the Tenir glyph, not the coloured launcher tile", () => {
    const xml = readText(NOTIFICATION_ICON);
    expect(xml).toContain("<vector");
    // White-alpha glyph so the system tint has a full shape to colour...
    expect(xml).toContain("#FFFFFFFF");
    // ...and none of the launcher's opaque tile / teal gradient, which would
    // otherwise fill the whole icon bounds.
    expect(xml).not.toContain("#0E1116");
    expect(xml).not.toContain("<gradient");
    // Shares the launcher glyph's geometry (same "T" path in the 108 viewport).
    expect(xml).toContain('android:viewportWidth="108"');
    expect(xml).toContain("M25.31,31.64");
    // Scaled up within the fixed status-bar slot so the mark fills the icon
    // bounds (less transparent padding) instead of rendering small.
    expect(xml).toContain("<group");
    expect(xml).toContain('android:scaleX="1.5"');
    expect(xml).toContain('android:scaleY="1.5"');
  });

  it("is wired as the session notification's small icon", () => {
    const service = readText(MIC_SERVICE);
    expect(service).toContain(".setSmallIcon(R.drawable.ic_notification)");
    // The stock Android microphone icon is gone.
    expect(service).not.toContain("android.R.drawable.ic_btn_speak_now");
  });
});
