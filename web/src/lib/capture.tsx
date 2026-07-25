/**
 * App-level live-capture context (XERK-111).
 *
 * The capture session used to be created *inside* the Live panel, so switching
 * to another dashboard tab unmounted the panel and its `useCapture` cleanup tore
 * the session down — a live recording ended the moment you left the page. This
 * provider hoists the single `CaptureSession` (via `useCapture`) up to the
 * persistent dashboard shell, above the tab switch, so navigating between tabs no
 * longer stops the recording; it keeps running in the background and the Live
 * panel simply re-attaches to the same session when you come back. The session is
 * only released when the whole dashboard unmounts (logout / server change).
 */

import { wsFromHttpBase } from "@tenir/client-core";
import { createContext, useContext, type ReactNode } from "react";

import { getServerUrl } from "../config";
import { useCapture, type CaptureController } from "./useCapture";

export interface CaptureContextValue {
  controller: CaptureController;
}

const CaptureContext = createContext<CaptureContextValue | null>(null);

export function CaptureProvider({ children }: { children: ReactNode }): JSX.Element {
  const controller = useCapture(wsFromHttpBase(getServerUrl()));

  return (
    <CaptureContext.Provider value={{ controller }}>{children}</CaptureContext.Provider>
  );
}

/** Read the shared capture session; throws if used outside a `CaptureProvider`. */
export function useCaptureContext(): CaptureContextValue {
  const ctx = useContext(CaptureContext);
  if (!ctx) throw new Error("useCaptureContext must be used within a CaptureProvider");
  return ctx;
}
