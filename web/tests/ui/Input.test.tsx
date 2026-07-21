import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Field } from "../../src/ui/Field";
import { Input, Textarea } from "../../src/ui/Input";

describe("Input primitives", () => {
  it("renders an input with the .input class and forwards placeholder", () => {
    render(<Input placeholder="Search" />);
    expect(screen.getByPlaceholderText("Search")).toHaveClass("input");
  });

  it("renders a textarea with .input and .textarea", () => {
    render(<Textarea placeholder="Body" />);
    expect(screen.getByPlaceholderText("Body")).toHaveClass("input", "textarea");
  });

  it("Field shows the label and the error in preference to the hint", () => {
    render(
      <Field label="Title" hint="optional" error="required">
        <Input />
      </Field>,
    );
    expect(screen.getByText("Title")).toBeInTheDocument();
    expect(screen.getByText("required")).toBeInTheDocument();
    expect(screen.queryByText("optional")).not.toBeInTheDocument();
  });
});
