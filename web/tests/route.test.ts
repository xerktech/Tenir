import { describe, expect, it } from "vitest";

import { tabFromHash, tabToHash } from "../src/lib/route";

const TABS = ["Live", "History", "Status", "Users"] as const;

describe("tabToHash", () => {
  it("names the tab as a lowercase fragment path", () => {
    expect(tabToHash("Live")).toBe("#/live");
    expect(tabToHash("History")).toBe("#/history");
  });
});

describe("tabFromHash", () => {
  it("resolves a fragment back to its tab, case-insensitively", () => {
    expect(tabFromHash("#/history", TABS, "Live")).toBe("History");
    expect(tabFromHash("#/HISTORY", TABS, "Live")).toBe("History");
    // Tolerates a missing slash (a hand-edited URL).
    expect(tabFromHash("#status", TABS, "Live")).toBe("Status");
  });

  it("falls back to the default for empty or unknown fragments", () => {
    expect(tabFromHash("", TABS, "Live")).toBe("Live");
    expect(tabFromHash("#/", TABS, "Live")).toBe("Live");
    expect(tabFromHash("#/settings", TABS, "Live")).toBe("Live");
  });

  it("falls back when the fragment names a tab the user is not offered", () => {
    // A non-admin deep-linking (or refreshing) #/users lands on Live, not a blank page.
    const memberTabs = ["Live", "History", "Status"] as const;
    expect(tabFromHash("#/users", memberTabs, "Live")).toBe("Live");
  });
});
