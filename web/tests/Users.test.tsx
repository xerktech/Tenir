import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UsersPanel } from "../src/panels/Users";
import { ToastProvider } from "../src/lib/toast";

const list = vi.fn();
const create = vi.fn();
const remove = vi.fn();

vi.mock("@tenir/client-core", () => ({
  ApiError: class ApiError extends Error {},
  users: {
    list: () => list(),
    create: (u: string, p: string, r: string) => create(u, p, r),
    remove: (id: string) => remove(id),
  },
}));

const ME = { userId: "me", username: "ada", household: "lab", role: "admin" };

beforeEach(() => {
  list.mockReset();
  create.mockReset();
  remove.mockReset();
  vi.restoreAllMocks();
});

function renderPanel() {
  return render(
    <ToastProvider>
      <UsersPanel me={ME} />
    </ToastProvider>,
  );
}

describe("UsersPanel", () => {
  it("renders the household roster", async () => {
    list.mockResolvedValue([
      { userId: "me", username: "ada", role: "admin", isEnvAdmin: false },
      { userId: "env", username: "root", role: "admin", isEnvAdmin: true },
      { userId: "u2", username: "bob", role: "member", isEnvAdmin: false },
    ]);
    renderPanel();
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());
    expect(screen.getByText("ada")).toBeInTheDocument();
    // The env admin and the signed-in user are both flagged as un-removable.
    expect(screen.getByText("env admin")).toBeInTheDocument();
    expect(screen.getByText("you")).toBeInTheDocument();
  });

  it("disables removal of self and the env admin, allows it for others", async () => {
    list.mockResolvedValue([
      { userId: "me", username: "ada", role: "admin", isEnvAdmin: false },
      { userId: "env", username: "root", role: "admin", isEnvAdmin: true },
      { userId: "u2", username: "bob", role: "member", isEnvAdmin: false },
    ]);
    renderPanel();
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());
    const removeButtons = screen.getAllByRole("button", { name: "Remove" });
    // ada (self) and root (env admin) locked; bob removable.
    expect(removeButtons[0]).toBeDisabled();
    expect(removeButtons[1]).toBeDisabled();
    expect(removeButtons[2]).toBeEnabled();
  });

  it("adds a user via the form", async () => {
    list.mockResolvedValue([]);
    create.mockResolvedValue({ userId: "u3", username: "cleo", household: "lab", role: "member" });
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getByText("No users yet.")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("username"), "cleo");
    await user.type(screen.getByPlaceholderText("at least 8 characters"), "longpassword");
    await user.click(screen.getByRole("button", { name: "Add user" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith("cleo", "longpassword", "member"));
  });

  it("rejects a too-short password without calling the api", async () => {
    list.mockResolvedValue([]);
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getByText("No users yet.")).toBeInTheDocument());

    await user.type(screen.getByPlaceholderText("username"), "cleo");
    await user.type(screen.getByPlaceholderText("at least 8 characters"), "short");
    await user.click(screen.getByRole("button", { name: "Add user" }));

    expect(create).not.toHaveBeenCalled();
    expect(await screen.findByText("Password must be at least 8 characters")).toBeInTheDocument();
  });

  it("removes a user after the two-step arm-then-confirm", async () => {
    list.mockResolvedValue([{ userId: "u2", username: "bob", role: "member", isEnvAdmin: false }]);
    remove.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());

    // First click arms the control (no browser dialog, nothing removed yet)…
    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(remove).not.toHaveBeenCalled();
    // …the second click commits.
    await user.click(screen.getByRole("button", { name: "Confirm remove" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("u2"));
  });

  it("does not remove on a single (arming) click", async () => {
    list.mockResolvedValue([{ userId: "u2", username: "bob", role: "member", isEnvAdmin: false }]);
    const user = userEvent.setup();
    renderPanel();
    await waitFor(() => expect(screen.getByText("bob")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Remove" }));
    expect(remove).not.toHaveBeenCalled();
    // The armed button names the commitment instead of firing it.
    expect(screen.getByRole("button", { name: "Confirm remove" })).toBeInTheDocument();
  });
});
