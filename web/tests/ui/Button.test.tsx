import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "../../src/ui/Button";

describe("Button", () => {
  it("defaults to the secondary variant and renders children", () => {
    render(<Button>Save</Button>);
    const btn = screen.getByRole("button", { name: "Save" });
    expect(btn).toHaveClass("btn", "btn-secondary");
  });

  it("applies the requested variant and forwards native props", () => {
    render(
      <Button variant="primary" disabled>
        Go
      </Button>,
    );
    const btn = screen.getByRole("button", { name: "Go" });
    expect(btn).toHaveClass("btn-primary");
    expect(btn).toBeDisabled();
  });
});
