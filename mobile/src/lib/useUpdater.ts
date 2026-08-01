/**
 * `useUpdater` (XERK-63) — drives the in-app update banner, mirroring the Turma
 * Android client's UpdateViewModel.
 *
 * On mount (Android only) it reads the installed version, checks the public GitHub
 * releases (`lib/updater.checkForUpdate`) and, if a newer APK exists, surfaces a
 * dismissible offer. It keeps checking while the app lives (XERK-167) — again
 * whenever the app returns to the foreground and on an interval while it stays
 * open — so picking up an update never requires relaunching. Checks are throttled
 * to `CHECK_THROTTLE_MS` (Turma's cadence), and a re-check can only move a
 * hidden/available banner (`applyCheckResult`), never an in-flight
 * download/install. Tapping the offer downloads + installs through the native
 * `AppUpdater` module, with live download progress. It stays QUIET on a check
 * failure (offline, rate-limit) — an update banner should never become an error
 * nag — and simply tries again next cycle. A dismissed version stays hidden
 * until a still-newer one appears.
 *
 * Everywhere the native module is absent (iOS, tests) the hook is inert: it never
 * leaves the `hidden` state, so nothing renders.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { AppState } from "react-native";

import type { AvailableUpdate, UpdateState } from "./updater";
import { applyCheckResult, checkForUpdate, CHECK_THROTTLE_MS, shouldCheck } from "./updater";
import type { DownloadProgress } from "../native/appUpdater";
import {
  appUpdaterAvailable,
  appUpdaterEvents,
  downloadAndInstall,
  getInstalledVersion,
} from "../native/appUpdater";

export type { UpdateState } from "./updater";

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
  const mounted = useRef(true);
  const checking = useRef(false);
  const lastCheckAt = useRef(0);

  // One complete check, throttled so every trigger (mount, foreground, interval)
  // can call it unconditionally. Quiet on any failure — leave the banner as-is.
  const runCheck = useCallback(async () => {
    if (!appUpdaterAvailable) return;
    if (checking.current || !shouldCheck(lastCheckAt.current, Date.now())) return;
    checking.current = true;
    lastCheckAt.current = Date.now();
    try {
      const installed = await getInstalledVersion();
      const update = await checkForUpdate(installed);
      if (!mounted.current) return;
      // Keep the pending offer for act()/Retry — a null result doesn't revoke it.
      if (update !== null && update.version !== dismissed.current) pending.current = update;
      setState((prev) => applyCheckResult(prev, update, dismissed.current));
    } catch {
      // Offline / rate-limited / no releases yet — say nothing, retry next cycle.
    } finally {
      checking.current = false;
    }
  }, []);

  // Check on mount, when the app comes back to the foreground, and periodically
  // while it stays open (XERK-167). The throttle inside runCheck dedupes them.
  useEffect(() => {
    mounted.current = true;
    if (!appUpdaterAvailable) return;
    void runCheck();
    const sub = AppState.addEventListener("change", (status) => {
      if (status === "active") void runCheck();
    });
    const timer = setInterval(() => {
      if (AppState.currentState === "active") void runCheck();
    }, CHECK_THROTTLE_MS);
    return () => {
      mounted.current = false;
      sub.remove();
      clearInterval(timer);
    };
  }, [runCheck]);

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
