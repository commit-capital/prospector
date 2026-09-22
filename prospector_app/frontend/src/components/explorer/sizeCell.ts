/** The Explorer's compact Size cell: effective LOC and file count in one
 *  string — "+239 · 8f". A noisy diff (mostly generated lines) shows its
 *  effective/raw pair instead of the "+" form, and `noisy` marks the cell
 *  muted. Missing halves drop out; both missing reads "—". */
export function sizeCell(
  effective: number | null,
  raw: number | null,
  files: number | null,
): { text: string; noisy: boolean } {
  const noisy = effective != null && raw != null && raw > 0 && effective / raw < 0.5;
  const loc = effective == null ? "" : noisy ? `${effective}/${raw}` : `+${effective}`;
  const f = files == null ? "" : `${files}f`;
  const text = loc && f ? `${loc} · ${f}` : loc || f || "—";
  return { text, noisy };
}
