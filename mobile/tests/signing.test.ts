import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Vitest runs with the workspace root (mobile/) as cwd, so resolve from it.
const readText = (rel: string) =>
  readFileSync(resolve(process.cwd(), rel)).toString("utf8");

const KEYSTORE = "android/app/tenir-release.keystore";
const BUILD_GRADLE = "android/app/build.gradle";

// Regression guard for XERK-60 ("Tenir app can't be updated — have to uninstall
// and reinstall every time"). The cause was release APKs signed with an ephemeral
// key: release.yml never passed signing secrets, so build.gradle fell back to the
// debug config, whose ~/.android/debug.keystore is auto-generated (random) per CI
// runner. Every release got a different certificate, so Android refused the update.
//
// The fix signs every release with a STABLE, committed keystore. These tests fail
// if that stable identity is removed or the debug fallback returns.
describe("Android release signing (XERK-60: updatable installs)", () => {
  it("commits a stable release keystore for a fixed signing identity", () => {
    // A committed keystore is what keeps the signing certificate identical across
    // builds — the precondition for installing an update in place.
    expect(existsSync(resolve(process.cwd(), KEYSTORE))).toBe(true);
  });

  it("is a PKCS12 keystore (readable by the JDK 17 build)", () => {
    // PKCS12 files start with the DER SEQUENCE tag 0x30; a legacy JKS would start
    // with the magic 0xFEEDFEED. Assert PKCS12 so the committed key stays loadable.
    const head = readFileSync(resolve(process.cwd(), KEYSTORE)).subarray(0, 4);
    expect(head[0]).toBe(0x30);
    expect([head[0], head[1], head[2], head[3]]).not.toEqual([
      0xfe, 0xed, 0xfe, 0xed,
    ]);
  });

  it("signs release builds with the stable release key, never the debug fallback", () => {
    const gradle = readText(BUILD_GRADLE);
    // The release buildType must use signingConfigs.release unconditionally.
    expect(gradle).toMatch(
      /release\s*\{[^}]*signingConfig\s+signingConfigs\.release/s,
    );
    // And must NOT resort to the (per-runner random) debug config for release.
    expect(gradle).not.toContain("? signingConfigs.release : signingConfigs.debug");
  });

  it("defaults the release signingConfig to the committed keystore + alias", () => {
    const gradle = readText(BUILD_GRADLE);
    // Committed keystore is the default storeFile; a -P upload key can override it.
    expect(gradle).toContain('"tenir-release.keystore"');
    expect(gradle).toContain('TENIR_UPLOAD_STORE_FILE');
    expect(gradle).toContain('"tenir"'); // keyAlias baked into the keystore
  });
});
