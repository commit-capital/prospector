import type { ReactNode } from "react";
import { usePRFlyout } from "../usePRFlyout";

/** A PR reference that opens the detail flyout (instead of navigating away). */
export function PRLink({ n, children, className }: { n: number; children?: ReactNode; className?: string }) {
  const { openPR } = usePRFlyout();
  return (
    <a
      href={`/prs/${n}`}
      className={className}
      onClick={(e) => { e.preventDefault(); e.stopPropagation(); openPR(n); }}
    >
      {children ?? `#${n}`}
    </a>
  );
}
