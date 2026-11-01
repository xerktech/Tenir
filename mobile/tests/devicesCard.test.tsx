/**
 * Settings → Devices card (XERK-2594), the web Devices tab's Android twin. The RN UI kit
 * is swapped for plain DOM stand-ins so the card renders under jsdom.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const list = vi.fn();
const revoke = vi.fn();

vi.mock("@tenir/client-core", () => ({
  ApiError: class ApiError extends Error {},
  devices: { list: () => list(), revoke: (id: string) => revoke(id) },
}));
vi.mock("react-native", () => ({
  Text: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
}));
vi.mock("../src/ui/ThemeContext", () => ({ useTheme: () => ({ colors: { text: "#000" } }) }));
vi.mock("../src/lib/notify", () => ({ useNotify: () => vi.fn() }));
vi.mock("../src/ui/components", () => {
  const Box = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  return {
    Card: Box,
    Row: Box,
    ListItem: Box,
    Label: Box,
    Muted: Box,
    Spinner: () => <span>loading</span>,
    EmptyState: ({ title }: { title: string }) => <p>{title}</p>,
    Button: ({ title, onPress }: { title: string; onPress: () => void }) => (
      <button onClick={onPress}>{title}</button>
    ),
    // One press confirms here; the arm-then-confirm machine has its own tests (confirm.test.ts).
    ConfirmButton: ({ title, onConfirm }: { title: string; onConfirm: () => void }) => (
      <button onClick={onConfirm}>{title}</button>
    ),
  };
});

import { DevicesCard } from "../src/screens/DevicesCard";

const PC = {
  id: "d1",
  name: "MEETING-PC",
  createdAt: "2026-11-01T00:00:00+00:00",
  expiresAt: "2027-11-01T00:00:00+00:00",
};

beforeEach(() => {
  list.mockReset();
  revoke.mockReset();
});

describe("DevicesCard (XERK-2594)", () => {
  it("lists devices and revokes one, then reloads", async () => {
    list.mockResolvedValueOnce([PC]).mockResolvedValueOnce([]);
    revoke.mockResolvedValue(undefined);
    render(<DevicesCard />);
    await waitFor(() => expect(screen.getByText(/MEETING-PC/)).toBeTruthy());
    fireEvent.click(screen.getByText("Revoke"));
    expect(revoke).toHaveBeenCalledWith("d1");
    await waitFor(() => expect(screen.getByText("No capture devices.")).toBeTruthy());
  });

  it("offers a retry when the list fails", async () => {
    list.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce([PC]);
    render(<DevicesCard />);
    await waitFor(() => expect(screen.getByText("Could not load devices")).toBeTruthy());
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => expect(screen.getByText(/MEETING-PC/)).toBeTruthy());
  });
});
