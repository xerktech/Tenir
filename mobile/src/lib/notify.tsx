/**
 * Toast notifications for the mobile app — the RN counterpart to the web SPA's toast.
 * A single transient banner pinned to the bottom of the screen; `useNotify()` returns
 * the `notify(message, kind?)` function used across the screens.
 */

import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";
import { StyleSheet, Text, View } from "react-native";

import { colors, space } from "../ui/theme";

type Kind = "ok" | "err";
type NotifyFn = (message: string, kind?: Kind) => void;

const NotifyContext = createContext<NotifyFn>(() => undefined);

export function useNotify(): NotifyFn {
  return useContext(NotifyContext);
}

export function NotifyProvider({ children }: { children: ReactNode }): JSX.Element {
  const [toast, setToast] = useState<{ message: string; kind: Kind } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const notify = useCallback<NotifyFn>((message, kind = "ok") => {
    setToast({ message, kind });
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setToast(null), 4000);
  }, []);

  return (
    <NotifyContext.Provider value={notify}>
      <View style={styles.fill}>
        {children}
        {toast && (
          <View
            accessibilityRole="alert"
            style={[styles.toast, toast.kind === "err" ? styles.err : styles.ok]}
          >
            <Text style={styles.toastText}>{toast.message}</Text>
          </View>
        )}
      </View>
    </NotifyContext.Provider>
  );
}

const styles = StyleSheet.create({
  fill: { flex: 1 },
  toast: {
    position: "absolute",
    left: space.lg,
    right: space.lg,
    bottom: space.xl,
    padding: space.md,
    borderRadius: 8,
    borderWidth: 1,
  },
  ok: { backgroundColor: colors.card, borderColor: colors.accent },
  err: { backgroundColor: colors.card, borderColor: colors.danger },
  toastText: { color: colors.text },
});
