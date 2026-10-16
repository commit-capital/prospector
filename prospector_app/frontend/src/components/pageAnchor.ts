/** Element ids the PR page gives its Checks panel, each check row, and (in
 *  its anchored DiffView) each file and new-side line, so a summary chip or a
 *  finding's path:line can scroll to it. */
export const CHECKS_ID = "pr-checks";
export const checkRowId = (key: string): string => `check-${key}`;
export const diffFileId = (path: string): string => `diff-${encodeURIComponent(path)}`;
export const diffLineId = (path: string, line: number): string => `${diffFileId(path)}-L${line}`;

/** Scroll `target` into view and flash it. */
export function flashTo(target: HTMLElement, block: ScrollLogicalPosition = "start"): void {
  target.scrollIntoView({ behavior: "smooth", block });
  target.classList.remove("anchor-flash");
  void target.offsetWidth;
  target.classList.add("anchor-flash");
  window.setTimeout(() => target.classList.remove("anchor-flash"), 2200);
}

/** Scroll to `line` of `path` in the page's diff, or to the file's header when
 *  the line is outside its hunks, and flash it. False when the file is not in
 *  the rendered diff. */
export function jumpToDiff(path: string, line: number | null): boolean {
  const row = line != null ? document.getElementById(diffLineId(path, line)) : null;
  const target = row ?? document.getElementById(diffFileId(path))?.querySelector<HTMLElement>(".difffile-head");
  if (!target) return false;
  flashTo(target, row ? "center" : "start");
  return true;
}
