/**
 * React binding for web live capture (master plan §4.1).
 *
 * Wires the shared `CaptureSession` to the browser: a real `ApiClient` on the
 * configured server, the browser microphone, and a localStorage resume store.
 * Mirrors the mobile `useCapture`; the session logic itself is covered by the
 * `CaptureSession` tests in client-core, so this thin wiring is typechecked, not
 * separately unit-tested.
 */

import { ApiClient, browserAudioSource, CaptureSession, type CaptureState } from "@tenir/client-core";
import { useEffect, useMemo, useState } from "react";

import * as store from "./captureStore";

export interface CaptureController {
  state: CaptureState;
  start(): Promise<boolean>;
  stop(): Promise<void>;
  togglePause(): void;
}

export function useCapture(wsUrl: string): CaptureController {
  const session = useMemo(
    () =>
      new CaptureSession({
        createClient: (handlers) => new ApiClient(wsUrl, handlers),
        audio: browserAudioSource(),
        loadSessionId: async () => store.loadSessionId(),
        saveSessionId: (id) => store.saveSessionId(id),
        clearSessionId: () => store.clearSessionId(),
        defaultMicSource: "phone-microphone",
      }),
    [wsUrl],
  );

  const [state, setState] = useState<CaptureState>(() => session.getState());

  useEffect(() => {
    const unsubscribe = session.subscribe(setState);
    return () => {
      unsubscribe();
      void session.stop(); // release mic + socket if the URL changes or the panel unmounts
    };
  }, [session]);

  return {
    state,
    start: () => session.start(),
    stop: () => session.stop(),
    togglePause: () => session.togglePause(),
  };
}
