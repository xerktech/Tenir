import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DevicesPanel } from "../src/panels/Devices";
import { ToastProvider } from "../src/lib/toast";

const list = vi.fn();
const revoke = vi.fn();

vi.mock("@tenir/client-core", () => ({
  ApiError: class ApiError extends Error {},
  devices: {
    list: () => list(),
    revoke: (id: string) => revoke(id),
  },
}));

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

function renderPanel() {
  return render(
    <ToastProvider>
      <DevicesPanel />
    </ToastProvider>,
  );
}

describe("DevicesPanel (XERK-2594)", () => {
  it("lists paired capture devices", async () => {
    list.mockResolvedValue([PC, { ...PC, id: "d2", name: "spare" }]);
    renderPanel();
    await waitFor(() => expect(screen.getByText("MEETING-PC")).toBeInTheDocument());
    expect(screen.getByText("spare")).toBeInTheDocument();
  });

  it("says how to pair when there are none", async () => {
    list.mockResolvedValue([]);
    renderPanel();
    await waitFor(() => expect(screen.getByText("No capture devices.")).toBeInTheDocument());
  });

  it("revokes only after the second click, then reloads", async () => {
    list.mockResolvedValueOnce([PC]).mockResolvedValueOnce([]);
    revoke.mockResolvedValue(undefined);
    renderPanel();
    const button = await screen.findByRole("button", { name: "Revoke" });
    await userEvent.click(button);
    expect(revoke).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm revoke" }));
    expect(revoke).toHaveBeenCalledWith("d1");
    await waitFor(() => expect(screen.getByText("No capture devices.")).toBeInTheDocument());
  });

  it("shows a retry when the list fails", async () => {
    list.mockRejectedValueOnce(new Error("boom")).mockResolvedValueOnce([PC]);
    renderPanel();
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.getByText("MEETING-PC")).toBeInTheDocument());
  });
});
