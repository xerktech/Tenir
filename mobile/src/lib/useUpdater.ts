/**
 * `useUpdater` (XERK-63) — drives the in-app update banner, mirroring the Turma
 * Android client's UpdateViewModel.
 *
 * On mount (Android only) it reads the installed version, checks the public GitHub
 * releases (`lib/updater.checkForUpdate`) and, if a newer APK exists, surfaces a
 * dismissible offer. Tapping it downloads + installs through the native
 * `AppUpdater` module, with live download progress. It stays QUIET on a check
 * failure (offline, rate-limit) — an update banner should never become an error
 * nag — and resurfaces the offer next launch. A dismissed version stays hidden
 * until a still-newer one appears.
 *
 * Everywhere the native module is absent (iOS, tests) the hook is inert: it never
 * leaves the `hidden` state, so nothing renders.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { AvailableUpdate } from "./updater";
import { checkForUpdate } from "./updater";
import type { DownloadProgress } from "../native/appUpdater";
import {
  appUpdaterAvailable,
  appUpdaterEvents,
  downloadAndInstall,
  getInstalledVersion,
} from "../native/appUpdater";

export type UpdateState =
  | { kind: "hidden" }
  | { kind: "available"; version: string }
  | { kind: "downloading"; version: string; pct: number | null }
  | { kind: "installing"; version: string; needsPermission: boolean }
  | { kind: "failed"; version: string };

export interface Updater {
  state: UpdateState;
  /** The banner's action button: download + install, or retry a failure. */
  act: () => void;
  /** Hide the current offer for this session (until a newer version turns up). */
  dismiss: () => void;
}

export function useUpdater(): Updater {
  const [state, setState] = useState<UpdateState>({ kind: "hidden" });
  const pending = useRef<AvailableUpdate | null>(null);
  const dismissed = useRef<string | null>(null);

  // One check on mount. Quiet on any failure — leave the banner hidden.
  useEffect(() => {
    if (!appUpdaterAvailable) return;
    let live = true;
    (async () => {
      try {
        const installed = await getInstalledVersion();
        const update = await checkForUpdate(installed);
        if (!live || update === null || update.version === dismissed.current) return;
        pending.current = update;
        setState((prev) => (prev.kind === "hidden" ? { kind: "available", version: update.version } : prev));
      } catch {
        // Offline / rate-limited / no releases yet — say nothing.
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  // Live download progress from the native module.
  useEffect(() => {
    if (appUpdaterEvents === null) return;
    const sub = appUpdaterEvents.addListener("AppUpdater.progress", (p: DownloadProgress) => {
      setState((prev) =>
        prev.kind === "downloading" ? { kind: "downloading", version: p.version, pct: p.pct } : prev,
      );
    });
    return () => sub.remove();
  }, []);

  const start = useCallback((update: AvailableUpdate) => {
    setState({ kind: "downloading", version: update.version, pct: null });
    downloadAndInstall(update.downloadUrl, update.version)
      .then((status) =>
        setState({
          kind: "installing",
          version: update.version,
          needsPermission: status === "needs_permission",
        }),
      )
      .catch(() => setState({ kind: "failed", version: update.version }));
  }, []);

  const act = useCallback(() => {
    const update = pending.current;
    if (update === null) return;
    // From "installing" a re-tap re-fires the installer (e.g. after granting the
    // install permission, or if the user backed out of the system installer).
    start(update);
  }, [start]);

  const dismiss = useCallback(() => {
    setState((prev) => {
      if (prev.kind !== "hidden" && "version" in prev) dismissed.current = prev.version;
      return { kind: "hidden" };
    });
  }, []);

  return { state, act, dismiss };
}
