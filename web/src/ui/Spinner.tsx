export function Spinner({ label = "Loading…" }: { label?: string }): JSX.Element {
  return (
    <span className="spinner-row" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}
    </span>
  );
}
