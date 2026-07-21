/**
 * Typed access to the `AppUpdater` native module (XERK-63) — the Android half of
 * the in-app updater (see `android/.../update/AppUpdaterModule.kt`). It reads the
 * installed version and downloads + installs a newer APK; the pure check logic
 * lives in `lib/updater.ts` and the UI glue in `lib/useUpdater.ts`.
 *
 * Android-only: the module is only registered in the Android app, so on iOS (and
 * under the vitest/jsdom test runner) `appUpdaterAvailable` is false and the
 * helpers reject — callers gate on it and simply never offer an update.
 */

import { NativeEventEmitter, NativeModules, Platform } from "react-native";

/** Status string resolved by `downloadAndInstall`. */
export type InstallStatus = "installing" | "needs_permission";

interface AppUpdaterNative {
  getInstalledVersion(): Promise<string>;
  downloadAndInstall(url: string, version: string): Promise<InstallStatus>;
}

const native: AppUpdaterNative | null =
  (NativeModules.AppUpdater as AppUpdaterNative | undefined) ?? null;

/** True only where the native module is present (the Android app). */
export const appUpdaterAvailable = Platform.OS === "android" && native !== null;

/** Emits `AppUpdater.progress` ({ version, pct }) during a download; null when unavailable. */
export const appUpdaterEvents: NativeEventEmitter | null = native
  ? new NativeEventEmitter(NativeModules.AppUpdater)
  : null;

export interface DownloadProgress {
  version: string;
  pct: number;
}

/** The installed app's `versionName` (e.g. "0.1.5"). */
export function getInstalledVersion(): Promise<string> {
  if (native === null) return Promise.reject(new Error("AppUpdater unavailable"));
  return native.getInstalledVersion();
}

/** Download the APK at [url] and hand it to the system installer. */
export function downloadAndInstall(url: string, version: string): Promise<InstallStatus> {
  if (native === null) return Promise.reject(new Error("AppUpdater unavailable"));
  return native.downloadAndInstall(url, version);
}
