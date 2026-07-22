import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NavIcon, type NavPage } from "../../src/ui/NavIcon";

const PAGES: NavPage[] = ["Live", "History", "Status", "Users"];

describe("NavIcon", () => {
  it("renders a decorative currentColor svg for every page", () => {
    for (const page of PAGES) {
      const { container } = render(<NavIcon page={page} />);
      const svg = container.querySelector("svg");
      expect(svg, page).not.toBeNull();
      // Decorative: hidden from assistive tech so the nav button keeps its text name.
      expect(svg).toHaveAttribute("aria-hidden", "true");
      // Inherits the nav item's colour rather than a hard-coded fill.
      expect(svg).toHaveAttribute("stroke", "currentColor");
      // Carries no text, so it never leaks into a button's accessible name.
      expect(svg?.textContent).toBe("");
    }
  });

  it("honours a custom size", () => {
    const { container } = render(<NavIcon page="Live" size={28} />);
    const svg = container.querySelector("svg");
    expect(svg).toHaveAttribute("width", "28");
    expect(svg).toHaveAttribute("height", "28");
  });
});
