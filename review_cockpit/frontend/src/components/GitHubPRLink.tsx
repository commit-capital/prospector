import type { ReactNode } from "react";
import { useRepoMeta } from "../RepoMetaContext";

/** The PR number as a blue link straight to GitHub, opened in a new tab. In a
 *  list row the surrounding cell stops the click from bubbling, so the link
 *  opens GitHub while clicking the row opens the in-app sidebar (#193). */
export function GitHubPRLink({ n, url, children, className }: {
  n: number;
  url?: string | null;
  children?: ReactNode;
  className?: string;
}) {
  const { prUrl } = useRepoMeta();
  return (
    <a
      href={url || prUrl(n)}
      target="_blank"
      rel="noreferrer"
      className={`gh-pr-link${className ? ` ${className}` : ""}`}
      title="Open this PR on GitHub ↗"
    >
      {children ?? `#${n}`}
    </a>
  );
}
