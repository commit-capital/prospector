import { Fragment } from "react";

// One active-filter clause. A plain string renders as text; when `onClear` is
// set the clause renders as a chip with a × that clears just that filter.
export interface FilterPart {
  key?: string;
  label: string;
  onClear?: () => void;
}

// A one-line "Showing N <unit>s matching: ..." summary, shared by the PR
// Explorer and Issues tables (#494) — each page builds its own `parts` array
// from its own filter spec (see prFilterParts.ts / issueFilterParts.ts) and this
// component only knows how to join them. Parts that carry `onClear` get a × to
// clear that one filter; plain-string parts render as text.
export function FilterSummary(
  { parts, total, unit = "PR" }:
  { parts: (string | FilterPart)[]; total: number | null; unit?: string },
) {
  if (parts.length === 0) return null;
  const countStr = total !== null ? `${total} ${unit}${total === 1 ? "" : "s"}` : "…";
  return (
    <p className="filter-summary">
      Showing {countStr} matching:{" "}
      {parts.map((p, i) => {
        const part: FilterPart = typeof p === "string" ? { label: p } : p;
        return (
          <Fragment key={part.key ?? i}>
            {i > 0 && " · "}
            {part.onClear ? (
              <span className="fs-chip">
                {part.label}
                <button
                  type="button"
                  className="fs-chip-x"
                  title={`Clear "${part.label}"`}
                  aria-label={`Clear filter: ${part.label}`}
                  onClick={part.onClear}
                >×</button>
              </span>
            ) : (
              part.label
            )}
          </Fragment>
        );
      })}
    </p>
  );
}
