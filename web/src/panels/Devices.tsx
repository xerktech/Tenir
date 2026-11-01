/**
 * Screen-capture devices (XERK-2594): the signed-in user's paired capture PCs, each with
 * its own device token. Revoking one cuts that PC off from /ws/screen at once and closes
 * its open socket, without logging out any other device. Pairing happens on the PC itself
 * (`tenir-capture --pair`), so there is no "add" here. Mirrors the Android Settings card.
 */

import { devices, type Device } from "@tenir/client-core";

import { useAsync } from "../lib/hooks";
import { errText, useNotify } from "../lib/toast";
import { Button, ConfirmButton, EmptyState, Spinner } from "../ui";

export function DevicesPanel(): JSX.Element {
  const notify = useNotify();
  const { data, loading, error, reload } = useAsync(() => devices.list());

  // Two-step confirm in place (Turma's pattern), as on the Users panel.
  const revoke = (d: Device) => () => {
    devices
      .revoke(d.id)
      .then(() => {
        notify(`Revoked ${d.name}`);
        reload();
      })
      .catch((e) => notify(errText(e), "err"));
  };

  return (
    <section>
      <h2>Devices</h2>
      <p className="muted">
        Screen-capture PCs paired to your account. Revoke one to disconnect it right away;
        pair it again with <code>tenir-capture --pair</code>.
      </p>
      {loading && <Spinner />}
      {!loading && error != null && (
        <div>
          <EmptyState title="Could not load devices" hint={errText(error)} />
          <Button onClick={reload}>Retry</Button>
        </div>
      )}
      {error == null && data?.length === 0 && (
        <EmptyState
          title="No capture devices."
          hint="Run tenir-capture --pair on the meeting PC to add one."
        />
      )}
      {data?.map((d) => (
        <div key={d.id} className="item row">
          <span className="grow">
            {d.name}
            <span className="muted">
              {" "}
              · paired {new Date(d.createdAt).toLocaleDateString()} · expires{" "}
              {new Date(d.expiresAt).toLocaleDateString()}
            </span>
          </span>
          <ConfirmButton confirmLabel="Confirm revoke" onConfirm={revoke(d)}>
            Revoke
          </ConfirmButton>
        </div>
      ))}
    </section>
  );
}
