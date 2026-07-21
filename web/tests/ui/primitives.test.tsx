import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Badge, Card, EmptyState, IconButton, Spinner } from "../../src/ui";

describe("presentational primitives", () => {
  it("Card renders children inside .card", () => {
    render(<Card>hello</Card>);
    expect(screen.getByText("hello")).toHaveClass("card");
  });

  it("Badge defaults to the accent tone", () => {
    render(<Badge>self</Badge>);
    expect(screen.getByText("self")).toHaveClass("badge-accent");
  });

  it("Spinner shows a status with its label", () => {
    render(<Spinner label="Loading…" />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("Loading…");
  });

  it("EmptyState shows the title", () => {
    render(<EmptyState title="No conversations yet" hint="Captured talks land here." />);
    expect(screen.getByText("No conversations yet")).toBeInTheDocument();
    expect(screen.getByText("Captured talks land here.")).toBeInTheDocument();
  });

  it("IconButton exposes its accessible label", () => {
    render(<IconButton label="Close">x</IconButton>);
    expect(screen.getByRole("button", { name: "Close" })).toHaveClass("icon-btn");
  });
});
