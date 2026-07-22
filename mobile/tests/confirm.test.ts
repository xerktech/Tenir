import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createConfirmArmer } from "../src/lib/confirm";

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

function harness(disarmAfterMs?: number) {
  const onConfirm = vi.fn();
  const states: boolean[] = [];
  const armer = createConfirmArmer({ onConfirm, onChange: (a) => states.push(a), disarmAfterMs });
  return { onConfirm, states, armer };
}

describe("createConfirmArmer", () => {
  it("arms on the first fire instead of confirming", () => {
    const { onConfirm, armer } = harness();
    armer.fire();
    expect(armer.armed).toBe(true);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("confirms and disarms on the second fire", () => {
    const { onConfirm, states, armer } = harness();
    armer.fire();
    armer.fire();
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(armer.armed).toBe(false);
    expect(states).toEqual([true, false]);
  });

  it("quietly expires after the arming window", () => {
    const { onConfirm, armer } = harness();
    armer.fire();
    vi.advanceTimersByTime(4000);
    expect(armer.armed).toBe(false);
    // The next fire arms again — the stale intent never confirms.
    armer.fire();
    expect(onConfirm).not.toHaveBeenCalled();
    expect(armer.armed).toBe(true);
  });

  it("honours a custom arming window", () => {
    const { armer } = harness(1000);
    armer.fire();
    vi.advanceTimersByTime(999);
    expect(armer.armed).toBe(true);
    vi.advanceTimersByTime(1);
    expect(armer.armed).toBe(false);
  });

  it("disarm() cancels an armed state without confirming", () => {
    const { onConfirm, armer } = harness();
    armer.fire();
    armer.disarm();
    expect(armer.armed).toBe(false);
    vi.advanceTimersByTime(10_000); // the pending timer was cleared with it
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
