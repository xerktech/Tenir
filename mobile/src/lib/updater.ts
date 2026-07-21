/**
 * In-app auto-update for the Android client (XERK-63) — the same model the Turma
 * Android app uses (its XERK-11): poll the **public** GitHub releases, find the
 * newest published `tenir-android-v<x.y.z>.apk`, and, when it's newer than what's
 * installed, offer a one-tap download + install through the system package
 * installer. A stopgap self-updater for a self-hosted, sideloaded app.
 *
 * This file is the pure half — picking among release assets + comparing versions,
 * plus the anonymous GitHub fetch — kept framework-free so it unit-tests under
 * vitest. The download + install (and reading the installed version) is the native
 * `AppUpdater` module; the UI glue is `useUpdater` + `UpdateBanner`.
 *
 * Why the asset FILENAME carries the version: the release pipeline
 * (`.github/scripts/manifest.js`) makes every release self-contained — a component
 * unchanged in a release still carries its own APK forward onto that release under
 * its ORIGINAL name (`tenir-android-v<x>.apk`). So the version baked into the
 * filename is the component's real version, and we compare THAT against the
 * installed `versionName` — never the release TAG, which runs ahead of a carried
 * component (the same reason Turma's updater compares the filename, not the tag).
 */

/** Owner/repo whose releases carry the Android APK. Public, so no auth is needed. */
export const RELEASES_URL = "https://api.github.com/repos/xerktech/Tenir/releases?per_page=20";

const APK_NAME = /^tenir-android-v(\d+\.\d+\.\d+)\.apk$/;

/** Parse the semver out of a `tenir-android-v<x.y.z>.apk` asset name, else null. */
export function apkAssetVersion(name: string): string | null {
  const m = APK_NAME.exec(name.trim());
  return m ? m[1] : null;
}

/**
 * Compare two dotted-numeric versions: <0 if a<b, 0 if equal, >0 if a>b.
 * Missing/short components read as 0 (`0.4` === `0.4.0`); a non-numeric component
 * (a dev/placeholder version) reads as 0 rather than throwing.
 */
export function compareVersions(a: string, b: string): number {
  const pa = a.trim().split(".");
  const pb = b.trim().split(".");
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = Number.parseInt(pa[i] ?? "", 10) || 0;
    const y = Number.parseInt(pb[i] ?? "", 10) || 0;
    if (x !== y) return x - y;
  }
  return 0;
}

/** An APK newer than what's installed, ready to offer to the user. */
export interface AvailableUpdate {
  version: string;
  downloadUrl: string;
}

/** Minimal projection of a GitHub release the picker needs, decoupled from the wire shape. */
export interface ReleaseAssetView {
  name: string;
  downloadUrl: string;
}
export interface ReleaseView {
  draft: boolean;
  prerelease: boolean;
  assets: ReleaseAssetView[];
}

/**
 * Given the parsed release list and the installed `versionName`, return the newest
 * publishable APK strictly newer than installed, or null if none exists / we're
 * already current. Draft and prerelease entries are skipped, and EVERY APK across
 * the recent releases is considered (not only the single "latest" release) so a
 * carried-forward asset can't hide a build.
 */
export function latestApkUpdate(releases: ReleaseView[], installed: string): AvailableUpdate | null {
  let best: AvailableUpdate | null = null;
  for (const r of releases) {
    if (r.draft || r.prerelease) continue;
    for (const a of r.assets) {
      const v = apkAssetVersion(a.name);
      if (v === null) continue;
      if (best === null || compareVersions(v, best.version) > 0) {
        best = { version: v, downloadUrl: a.downloadUrl };
      }
    }
  }
  if (best === null) return null;
  return compareVersions(best.version, installed) > 0 ? best : null;
}

interface GhAsset {
  name?: string;
  browser_download_url?: string;
}
interface GhRelease {
  draft?: boolean;
  prerelease?: boolean;
  assets?: GhAsset[];
}

/** Fetch + normalize the recent releases from the public GitHub API. */
export async function fetchReleases(fetchImpl: typeof fetch = fetch): Promise<ReleaseView[]> {
  const resp = await fetchImpl(RELEASES_URL, {
    headers: { Accept: "application/vnd.github+json" },
  });
  if (!resp.ok) throw new Error(`GitHub HTTP ${resp.status}`);
  const body = (await resp.json()) as GhRelease[];
  return body.map((r) => ({
    draft: r.draft === true,
    prerelease: r.prerelease === true,
    assets: (r.assets ?? []).map((a) => ({
      name: a.name ?? "",
      downloadUrl: a.browser_download_url ?? "",
    })),
  }));
}

/**
 * The whole read-only check: fetch the public releases and return the newest APK
 * strictly newer than [installed], or null. Throws on a network / HTTP failure —
 * callers stay quiet on failure (an update check should never turn into a nag).
 */
export async function checkForUpdate(
  installed: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AvailableUpdate | null> {
  const releases = await fetchReleases(fetchImpl);
  return latestApkUpdate(releases, installed);
}
