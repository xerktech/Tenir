/**
 * React binding for the live capture session (master plan §10).
 *
 * Wires the framework-agnostic `CaptureSession` to the device: a real `ApiClient`
 * pointed at the chosen server, the native phone mic, and the per-device session-id
 * store used for resume. The session is *not* stopped when the app is backgrounded —
 * the native recorder keeps streaming under the platform's background-audio entitlement
 * (see `audio/native.ts`) and `ApiClient` reconnects/resumes on its own — so this
 * hook only tears the session down when the screen unmounts or the server URL changes.
 *
 * Because it imports the native mic and `react-native`, it is typechecked but not
 * unit-tested; the behaviour it exposes is covered by the `CaptureSession` tests.
 */

import { ApiClient } from "@tenir/client-core";
import type { CueLevel, MicSource } from "@tenir/contract";
import { useEffect, useMemo, useRef, useState } from "react";

import { deviceAudioSource } from "../audio/native";
import { DEFAULT_MIC_SOURCE, DEFAULT_SOURCE_LANG } from "../config";
import { deviceKeyValue } from "../secureStorage";
import { clearSessionId, loadSessionId, saveSessionId } from "../storage";
import { CaptureSession, type CaptureState } from "@tenir/client-core";

export interface CaptureController {
  state: CaptureState;
  start(): Promise<boolean>;
  stop(): Promise<void>;
  togglePause(): void;
  switchMic(micSource: MicSource): void;
}

export function useCapture(wsUrl: string, cueLevel?: CueLevel): CaptureController {
  // A ref so changing the cue toggle updates what the next session.start sends
  // without recreating the session mid-flight (XERK-81).
  const cueLevelRef = useRef(cueLevel);
  cueLevelRef.current = cueLevel;

  const session = useMemo(() => {
    const kv = deviceKeyValue();
    return new CaptureSession({
      createClient: (handlers) => new ApiClient(wsUrl, handlers),
      audio: deviceAudioSource(),
      loadSessionId: () => loadSessionId(kv),
      saveSessionId: (id) => void saveSessionId(kv, id),
      clearSessionId: () => void clearSessionId(kv),
      defaultMicSource: DEFAULT_MIC_SOURCE,
      sourceLang: DEFAULT_SOURCE_LANG,
      get cueLevel() {
        return cueLevelRef.current;
      },
    });
  }, [wsUrl]);

  const [state, setState] = useState<CaptureState>(() => session.getState());

  useEffect(() => {
    const unsubscribe = session.subscribe(setState);
    return () => {
      unsubscribe();
      void session.stop(); // release mic + socket if the URL changes or the screen unmounts
    };
  }, [session]);

  return {
    state,
    start: () => session.start(),
    stop: () => session.stop(),
    togglePause: () => session.togglePause(),
    switchMic: (micSource) => session.switchMic(micSource),
  };
}
