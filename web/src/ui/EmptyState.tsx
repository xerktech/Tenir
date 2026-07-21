export function EmptyState({ title, hint }: { title: string; hint?: string }): JSX.Element {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {hint && <p className="empty-hint">{hint}</p>}
    </div>
  );
}
