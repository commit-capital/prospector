/** Element ids the PR page's anchored DiffView gives each file and each
 *  new-side line, so a finding's path:line can scroll to it. */
export const diffFileId = (path: string): string => `diff-${encodeURIComponent(path)}`;
export const diffLineId = (path: string, line: number): string => `${diffFileId(path)}-L${line}`;

/** Scroll to `line` of `path` in the page's diff, or to the file's header when
 *  the line is outside its hunks, and flash it. False when the file is not in
 *  the rendered diff. */
export function jumpToDiff(path: string, line: number | null): boolean {
  const row = line != null ? document.getElementById(diffLineId(path, line)) : null;
  const target = row ?? document.getElementById(diffFileId(path))?.querySelector<HTMLElement>(".difffile-head");
  if (!target) return false;
  target.scrollIntoView({ behavior: "smooth", block: row ? "center" : "start" });
  target.classList.remove("diff-flash");
  void target.offsetWidth;
  target.classList.add("diff-flash");
  window.setTimeout(() => target.classList.remove("diff-flash"), 2200);
  return true;
}
