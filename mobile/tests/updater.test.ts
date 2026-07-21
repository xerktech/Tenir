import { describe, expect, it, vi } from "vitest";

import {
  apkAssetVersion,
  checkForUpdate,
  compareVersions,
  fetchReleases,
  latestApkUpdate,
  RELEASES_URL,
  type ReleaseView,
} from "../src/lib/updater";

describe("apkAssetVersion", () => {
  it("parses the version out of a tenir-android-v<x.y.z>.apk name", () => {
    expect(apkAssetVersion("tenir-android-v0.1.5.apk")).toBe("0.1.5");
    expect(apkAssetVersion("  tenir-android-v12.3.4.apk  ")).toBe("12.3.4");
  });

  it("rejects anything that isn't a release APK asset", () => {
    expect(apkAssetVersion("tenir-even-v0.1.5.ehpk")).toBeNull();
    expect(apkAssetVersion("manifest.json")).toBeNull();
    expect(apkAssetVersion("tenir-android-v0.1.apk")).toBeNull(); // needs three parts
    expect(apkAssetVersion("tenir-android-vX.Y.Z.apk")).toBeNull();
  });
});

describe("compareVersions", () => {
  it("orders dotted-numeric versions", () => {
    expect(compareVersions("0.1.5", "0.1.4")).toBeGreaterThan(0);
    expect(compareVersions("0.1.4", "0.1.5")).toBeLessThan(0);
    expect(compareVersions("0.2.0", "0.1.9")).toBeGreaterThan(0);
    expect(compareVersions("1.0.0", "0.9.9")).toBeGreaterThan(0);
  });

  it("treats missing/short components as 0", () => {
    expect(compareVersions("0.4", "0.4.0")).toBe(0);
    expect(compareVersions("0.4.1", "0.4")).toBeGreaterThan(0);
  });

  it("treats a non-numeric component as 0 rather than throwing", () => {
    expect(compareVersions("0.1.1", "0.1.x")).toBeGreaterThan(0);
    expect(compareVersions("dev", "0.0.0")).toBe(0);
  });
});

function release(assetNames: string[], opts: Partial<ReleaseView> = {}): ReleaseView {
  return {
    draft: opts.draft ?? false,
    prerelease: opts.prerelease ?? false,
    assets: assetNames.map((name) => ({
      name,
      downloadUrl: `https://example.com/${name}`,
    })),
  };
}

describe("latestApkUpdate", () => {
  it("returns the newest APK when it is strictly newer than installed", () => {
    const releases = [release(["tenir-android-v0.1.5.apk", "manifest.json"])];
    expect(latestApkUpdate(releases, "0.1.4")).toEqual({
      version: "0.1.5",
      downloadUrl: "https://example.com/tenir-android-v0.1.5.apk",
    });
  });

  it("returns null when already current or newer", () => {
    const releases = [release(["tenir-android-v0.1.5.apk"])];
    expect(latestApkUpdate(releases, "0.1.5")).toBeNull();
    expect(latestApkUpdate(releases, "0.2.0")).toBeNull();
  });

  it("skips draft and prerelease releases", () => {
    const releases = [
      release(["tenir-android-v0.2.0.apk"], { draft: true }),
      release(["tenir-android-v0.1.9.apk"], { prerelease: true }),
      release(["tenir-android-v0.1.5.apk"]),
    ];
    expect(latestApkUpdate(releases, "0.1.4")?.version).toBe("0.1.5");
  });

  it("considers APKs across all recent releases, not just the newest tag", () => {
    // A newer release can CARRY an older APK forward, or omit the mobile asset;
    // the newest APK may live on an earlier release. Every one is considered.
    const releases = [
      release(["tenir-even-v0.2.0.ehpk"]), // newest tag, no APK
      release(["tenir-android-v0.1.6.apk"]),
      release(["tenir-android-v0.1.5.apk"]),
    ];
    expect(latestApkUpdate(releases, "0.1.5")?.version).toBe("0.1.6");
  });

  it("returns null when no release carries an APK", () => {
    expect(latestApkUpdate([release(["manifest.json"])], "0.1.0")).toBeNull();
    expect(latestApkUpdate([], "0.1.0")).toBeNull();
  });
});

describe("fetchReleases / checkForUpdate", () => {
  function fakeFetch(status: number, body: unknown): typeof fetch {
    return vi.fn(async () => ({
      ok: status >= 200 && status < 300,
      status,
      json: async () => body,
    })) as unknown as typeof fetch;
  }

  it("hits the public releases endpoint and normalizes the wire shape", async () => {
    const f = fakeFetch(200, [
      {
        draft: false,
        prerelease: false,
        assets: [{ name: "tenir-android-v0.1.5.apk", browser_download_url: "https://dl/apk" }],
      },
    ]);
    const releases = await fetchReleases(f);
    expect(f).toHaveBeenCalledWith(RELEASES_URL, expect.anything());
    expect(releases).toEqual([
      { draft: false, prerelease: false, assets: [{ name: "tenir-android-v0.1.5.apk", downloadUrl: "https://dl/apk" }] },
    ]);
  });

  it("tolerates releases missing optional fields", async () => {
    const releases = await fetchReleases(fakeFetch(200, [{}]));
    expect(releases).toEqual([{ draft: false, prerelease: false, assets: [] }]);
  });

  it("throws on a non-2xx response", async () => {
    await expect(fetchReleases(fakeFetch(403, {}))).rejects.toThrow("GitHub HTTP 403");
  });

  it("returns the available update end to end", async () => {
    const f = fakeFetch(200, [
      {
        assets: [{ name: "tenir-android-v0.1.5.apk", browser_download_url: "https://dl/apk" }],
      },
    ]);
    expect(await checkForUpdate("0.1.4", f)).toEqual({
      version: "0.1.5",
      downloadUrl: "https://dl/apk",
    });
    expect(await checkForUpdate("0.1.5", f)).toBeNull();
  });
});
