/** Household members — the admin-only add/remove roster (master plan §7). */

import { users, type Principal, type User } from "@tenir/client-core";
import { useState } from "react";

import { useAsync } from "../lib/hooks";
import { errText, useNotify } from "../lib/toast";
import { Badge, Button, ConfirmButton, EmptyState, Field, Input, Spinner } from "../ui";

type Role = "member" | "admin";

export function UsersPanel({ me }: { me: Principal }): JSX.Element {
  const notify = useNotify();
  const { data, loading, reload } = useAsync(() => users.list());
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("member");
  const [busy, setBusy] = useState(false);

  const add = async () => {
    if (!username.trim()) return notify("Enter a username", "err");
    if (password.length < 8) return notify("Password must be at least 8 characters", "err");
    setBusy(true);
    try {
      await users.create(username.trim(), password, role);
      notify(`Added ${username.trim()}`);
      setUsername("");
      setPassword("");
      setRole("member");
      reload();
    } catch (e) {
      notify(errText(e), "err");
    } finally {
      setBusy(false);
    }
  };

  // Removal confirms in place: the danger button arms on the first click and
  // commits on the second (Turma's two-step pattern) — no browser dialog.
  const remove = (u: User) => () => {
    users
      .remove(u.userId)
      .then(() => {
        notify(`Removed ${u.username}`);
        reload();
      })
      .catch((e) => notify(errText(e), "err"));
  };

  return (
    <section>
      <h2>Users</h2>
      <p className="muted">Add or remove members of the {me.household} household.</p>
      <div className="row">
        <Field label="Username" htmlFor="new-user">
          <Input
            id="new-user"
            placeholder="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </Field>
        <Field label="Password" htmlFor="new-pass">
          <Input
            id="new-pass"
            type="password"
            placeholder="at least 8 characters"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>
        <Field label="Role" htmlFor="new-role">
          <select
            id="new-role"
            className="input"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            <option value="member">member</option>
            <option value="admin">admin</option>
          </select>
        </Field>
        <Button variant="primary" disabled={busy} onClick={() => void add()}>
          {busy ? "Adding…" : "Add user"}
        </Button>
      </div>

      {loading && <Spinner />}
      {data?.length === 0 && <EmptyState title="No users yet." hint="Add a household member above." />}
      {data?.map((u) => (
        <UserRow key={u.userId} user={u} isSelf={u.userId === me.userId} onRemove={remove(u)} />
      ))}
    </section>
  );
}

function UserRow({
  user,
  isSelf,
  onRemove,
}: {
  user: User;
  isSelf: boolean;
  onRemove: () => void;
}): JSX.Element {
  // Self and the env-managed bootstrap admin can't be removed — the server refuses
  // both, so disable the control rather than offer a click that 400s/409s.
  const locked = isSelf || user.isEnvAdmin;
  const reason = isSelf ? "you" : user.isEnvAdmin ? "env admin" : undefined;
  return (
    <div className="item row">
      <span className="grow">
        {user.username}
        {user.role === "admin" && <Badge>admin</Badge>}
        {reason && <Badge>{reason}</Badge>}
      </span>
      <ConfirmButton confirmLabel="Confirm remove" disabled={locked} onConfirm={onRemove}>
        Remove
      </ConfirmButton>
    </div>
  );
}
