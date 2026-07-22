import { fireEvent, render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConfirmButton } from "../../src/ui/ConfirmButton";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("ConfirmButton", () => {
  it("arms on the first click instead of confirming", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton confirmLabel="Confirm delete" onConfirm={onConfirm}>
        Delete
      </ConfirmButton>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));

    // Armed: the label names the commitment, nothing has been deleted yet.
    expect(onConfirm).not.toHaveBeenCalled();
    const armed = screen.getByRole("button", { name: "Confirm delete" });
    expect(armed).toHaveClass("btn-danger", "armed");
  });

  it("confirms on the second click", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton confirmLabel="Confirm delete" onConfirm={onConfirm}>
        Delete
      </ConfirmButton>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    expect(onConfirm).toHaveBeenCalledTimes(1);
    // Confirming disarms — the button is back at rest.
    expect(screen.getByRole("button", { name: "Delete" })).not.toHaveClass("armed");
  });

  it("quietly disarms after the arming window expires", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton confirmLabel="Confirm delete" onConfirm={onConfirm}>
        Delete
      </ConfirmButton>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    act(() => vi.advanceTimersByTime(4000));

    // The accidental click expired; a later click arms again, not confirms.
    const resting = screen.getByRole("button", { name: "Delete" });
    expect(resting).not.toHaveClass("armed");
    fireEvent.click(resting);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("does nothing while disabled", () => {
    const onConfirm = vi.fn();
    render(
      <ConfirmButton confirmLabel="Confirm delete" onConfirm={onConfirm} disabled>
        Delete
      </ConfirmButton>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
  });
});
