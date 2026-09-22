// Table-shaped loading placeholder: shimmering row bars where the data will
// land, so the page keeps its layout while it loads.
export function SkeletonRows({ rows = 8, label = "Loading…" }: { rows?: number; label?: string }) {
  return (
    <div className="skeleton-rows" role="status" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => <div key={i} className="skeleton-row" />)}
    </div>
  );
}
