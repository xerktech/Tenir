/**
 * App-level live-capture context (XERK-111).
 *
 * The capture session used to be created *inside* the Live screen, so switching
 * to another tab unmounted the screen and its `useCapture` cleanup tore the
 * session down — a live recording ended the moment you left the page (even though
 * the native recorder holds a background-audio entitlement, the JS session itself
 * was destroyed). This provider hoists the single `CaptureSession` (via
 * `useCapture`) up to the persistent dashboard, above the tab switch, so moving
 * between tabs no longer stops the recording; it keeps running in the background
 * and the Live screen re-attaches to the same session when you come back. The
 * session is only released when the dashboard unmounts (sign-out / server change).
 */

import { createContext, useContext, type ReactNode } from "react";

import { useCapture, type CaptureController } from "./useCapture";

export interface CaptureContextValue {
  controller: CaptureController;
}

const CaptureContext = createContext<CaptureContextValue | null>(null);

export function CaptureProvider({
  wsUrl,
  children,
}: {
  wsUrl: string;
  children: ReactNode;
}): JSX.Element {
  const controller = useCapture(wsUrl);

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
