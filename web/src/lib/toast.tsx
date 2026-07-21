/** Toast notifications + a shared error-formatting helper for the web SPA. */

import { ApiError } from "@tenir/client-core";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Kind = "ok" | "err";
type NotifyFn = (message: string, kind?: Kind) => void;

const ToastContext = createContext<NotifyFn>(() => undefined);

/** Format any thrown value into a user-facing message (ApiError keeps its status). */
export function errText(err: unknown): string {
  return err instanceof ApiError ? `${err.status}: ${err.message}` : String(err);
}

export function useNotify(): NotifyFn {
  return useContext(ToastContext);
}

export function ToastProvider({ children }: { children: ReactNode }): JSX.Element {
  const [toast, setToast] = useState<{ message: string; kind: Kind } | null>(null);
  const notify = useCallback<NotifyFn>((message, kind = "ok") => {
    setToast({ message, kind });
    window.setTimeout(() => setToast(null), 4000);
  }, []);
  return (
    <ToastContext.Provider value={notify}>
      {children}
      {toast && (
        <div className={`toast ${toast.kind}`} role="status">
          {toast.message}
        </div>
      )}
    </ToastContext.Provider>
  );
}
