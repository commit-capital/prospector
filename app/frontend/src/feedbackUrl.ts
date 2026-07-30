import type { FeedbackTarget } from "./api";

/** GitHub's prefilled new-issue URL, or null when the backend has no feedback
 *  repo configured. When title/userBody are provided (from the AI generator),
 *  both are embedded in the URL. The context footer (branch / worktree / page
 *  URL) is always appended to the body. */
export function buildIssueUrl(
  target: FeedbackTarget,
  pageUrl: string,
  title?: string,
  userBody?: string,
): string | null {
  if (!target.repo) return null;
  const params = new URLSearchParams();
  if (target.labels?.length) params.set("labels", target.labels.join(","));
  if (target.assignee) params.set("assignees", target.assignee);
  if (title) params.set("title", title);
  const where = [
    target.branch && `branch \`${target.branch}\``,
    target.worktree && `worktree \`${target.worktree}\``,
    pageUrl && `page ${pageUrl}`,
  ].filter(Boolean).join(" · ");
  const footer = where ? `\n\n---\n<sub>⛏️ Filed from Prospector · ${where}</sub>` : "";
  const body = (userBody || "") + footer;
  if (body) params.set("body", body);
  return `https://github.com/${target.repo}/issues/new?${params.toString()}`;
}
