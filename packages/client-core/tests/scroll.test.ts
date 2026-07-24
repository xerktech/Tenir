/**
 * Live-transcript auto-scroll geometry (XERK-103): the shared "stick to the
 * bottom only while already there" rule every frontend follows.
 */

import { describe, expect, it } from "vitest";

import { STICK_THRESHOLD_PX, isPinnedToBottom } from "../src/scroll";

describe("isPinnedToBottom", () => {
  it("is pinned when scrolled exactly to the bottom", () => {
    // scrollTop === scrollHeight - clientHeight → 0px from the bottom.
    expect(isPinnedToBottom({ scrollTop: 800, scrollHeight: 1000, clientHeight: 200 })).toBe(true);
  });

  it("stays pinned within the slack threshold", () => {
    // A few px shy of the bottom still counts as following the feed.
    expect(
      isPinnedToBottom({
        scrollTop: 800 - STICK_THRESHOLD_PX,
        scrollHeight: 1000,
        clientHeight: 200,
      }),
    ).toBe(true);
  });

  it("releases once scrolled up past the threshold to re-read earlier text", () => {
    expect(
      isPinnedToBottom({
        scrollTop: 800 - STICK_THRESHOLD_PX - 1,
        scrollHeight: 1000,
        clientHeight: 200,
      }),
    ).toBe(false);
  });

  it("is pinned when the content is shorter than the box (nothing to scroll)", () => {
    expect(isPinnedToBottom({ scrollTop: 0, scrollHeight: 120, clientHeight: 400 })).toBe(true);
  });

  it("honours a custom threshold", () => {
    // 1000 - 700 - 200 = 100px from the bottom.
    const g = { scrollTop: 700, scrollHeight: 1000, clientHeight: 200 };
    expect(isPinnedToBottom(g, 50)).toBe(false);
    expect(isPinnedToBottom(g, 150)).toBe(true);
  });
});
