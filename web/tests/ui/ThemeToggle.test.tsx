import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ThemeToggle } from "../../src/ui/ThemeToggle";

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});
afterEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
});

describe("ThemeToggle", () => {
  it("starts at System and cycles to Light then Dark, reflecting on <html>", async () => {
    const user = userEvent.setup();
    render(<ThemeToggle />);

    const btn = screen.getByRole("button");
    expect(btn).toHaveTextContent("System");

    await user.click(btn);
    expect(btn).toHaveTextContent("Light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");

    await user.click(btn);
    expect(btn).toHaveTextContent("Dark");
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});
