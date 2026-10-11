/**
 * SetupScreen's boot-failure state (XERK-1591): the heading/body come from
 * client-core's shared `describeBootFailure` (web renders the same copy), and the
 * card offers Retry + Log out. The RN UI kit is swapped for plain DOM stand-ins so
 * the screen renders under jsdom; client-core is the real module.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, NetworkError, ServerUnavailableError } from "@tenir/client-core";

vi.mock("../src/ui/components", () => {
  const Box = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    Screen: Box,
    Card: Box,
    Row: Box,
    Muted: ({ children }: { children?: ReactNode }) => <p>{children}</p>,
    Heading: ({ children }: { children?: ReactNode }) => <h1>{children}</h1>,
    Button: ({ title, onPress }: { title: string; onPress: () => void }) => (
      <button onClick={onPress}>{title}</button>
    ),
    Field: ({ label }: { label: string }) => <label>{label}</label>,
  };
});
vi.mock("../src/lib/notify", () => ({ useNotify: () => vi.fn() }));

import { SetupScreen } from "../src/screens/Setup";

function renderSetup(props: { unreachable?: boolean; error?: unknown; onRetry?: () => void; onSignOut?: () => void }) {
  return render(
    <SetupScreen initialServerUrl="tenir.example.com" onConnect={async () => {}} {...props} />,
  );
}

describe("SetupScreen boot-failure state (XERK-1591)", () => {
  it("words a 5xx as a server error, with the detail line", () => {
    renderSetup({ unreachable: true, error: new ApiError(500, "boom") });
    expect(screen.getByRole("heading", { name: "Your server hit an error" })).toBeTruthy();
    expect(screen.getByText(/answered with an error/)).toBeTruthy();
    expect(screen.getByText(/Server error \(500\): boom/)).toBeTruthy();
    expect(screen.queryByText(/offline or restarting/)).toBeNull();
  });

  it.each([new NetworkError("down"), new ServerUnavailableError("db down")])(
    "words a transport failure / 503 as unreachable",
    (error) => {
      renderSetup({ unreachable: true, error });
      expect(screen.getByRole("heading", { name: "Can't reach your server" })).toBeTruthy();
      expect(screen.getByText(/offline or restarting/)).toBeTruthy();
    },
  );

  it("offers Retry and Log out, each wired to its handler", () => {
    const onRetry = vi.fn();
    const onSignOut = vi.fn();
    renderSetup({ unreachable: true, error: new NetworkError("down"), onRetry, onSignOut });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    fireEvent.click(screen.getByRole("button", { name: "Log out" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it("is the plain setup screen when the server isn't unreachable", () => {
    renderSetup({ unreachable: false, onSignOut: vi.fn() });
    expect(screen.getByRole("heading", { name: "Set up Tenir" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Log out" })).toBeNull();
  });
});
