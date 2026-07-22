/**
 * Two-step destructive button (Turma's arm-then-confirm pattern): the first
 * click arms it — the outline danger button fills solid and shows the confirm
 * label — and only a second click within the arming window commits. It disarms
 * itself after `disarmAfterMs`, so an accidental click quietly expires instead
 * of leaving a live trigger behind. Replaces `window.confirm` dialogs.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";

import { Button } from "./Button";

export interface ConfirmButtonProps {
  /** Resting label, e.g. "Delete". */
  children: ReactNode;
  /** Armed label naming the commitment, e.g. "Confirm delete". */
  confirmLabel: string;
  /** Fired by the second (confirming) click. */
  onConfirm: () => void;
  disabled?: boolean;
  /** How long the armed state lasts before quietly expiring. */
  disarmAfterMs?: number;
}

export function ConfirmButton({
  children,
  confirmLabel,
  onConfirm,
  disabled,
  disarmAfterMs = 4000,
}: ConfirmButtonProps): JSX.Element {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  };
  useEffect(() => clear, []);

  const click = () => {
    if (!armed) {
      setArmed(true);
      clear();
      timer.current = setTimeout(() => setArmed(false), disarmAfterMs);
      return;
    }
    clear();
    setArmed(false);
    onConfirm();
  };

  return (
    <Button variant="danger" className={armed ? "armed" : ""} disabled={disabled} onClick={click}>
      {armed ? confirmLabel : children}
    </Button>
  );
}
