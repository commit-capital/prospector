/** Render any cell value to a string: null/undefined → em dash, objects → JSON,
 *  everything else via String(). Used for the Tables preview + detail cells. */
export function stringify(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** stringify(), truncated to `maxLength` with an ellipsis. */
export function formatCell(value: unknown, maxLength = 100): string {
  const str = stringify(value);
  return str.length > maxLength ? str.slice(0, maxLength) + "…" : str;
}
