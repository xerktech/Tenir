/**
 * The arm-then-confirm state machine behind destructive buttons (Turma's
 * two-step pattern, shared with the web SPA's `ConfirmButton`): the first
 * `fire()` arms the control, a second within the window confirms, and an
 * untouched armed state quietly expires. Framework-agnostic so it unit-tests
 * without a React renderer; `components.tsx` wires it to a `Pressable`.
 */

export interface ConfirmArmer {
  /** Arm on the first call; confirm (and disarm) on the second. */
  fire(): void;
  /** Cancel an armed state without confirming (e.g. on unmount). */
  disarm(): void;
  readonly armed: boolean;
}

export function createConfirmArmer(opts: {
  /** Fired by the second (confirming) `fire()`. */
  onConfirm: () => void;
  /** Observes armed-state changes (drives the component's rendering). */
  onChange: (armed: boolean) => void;
  /** How long the armed state lasts before quietly expiring. */
  disarmAfterMs?: number;
}): ConfirmArmer {
  const disarmAfterMs = opts.disarmAfterMs ?? 4000;
  let armed = false;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const clear = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  };
  const setArmed = (next: boolean) => {
    armed = next;
    opts.onChange(next);
  };

  return {
    fire() {
      if (!armed) {
        clear();
        setArmed(true);
        timer = setTimeout(() => setArmed(false), disarmAfterMs);
        return;
      }
      clear();
      setArmed(false);
      opts.onConfirm();
    },
    disarm() {
      clear();
      if (armed) setArmed(false);
    },
    get armed() {
      return armed;
    },
  };
}
