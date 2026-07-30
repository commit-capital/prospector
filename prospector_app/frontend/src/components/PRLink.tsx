import type { ReactNode } from "react";
import { usePRFlyout } from "../usePRFlyout";

/** A PR reference that opens the detail flyout (instead of navigating away). */
export function PRLink({ n, children, className, title }: { n: number; children?: ReactNode; className?: string; title?: string }) {
  const { openPR } = usePRFlyout();
  return (
    <a
      href={`/prs/${n}`}
      className={className}
      title={title}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openPR(n); }}
    >
      {children ?? `#${n}`}
    </a>
  );
}
