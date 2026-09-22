import type { ReactNode } from "react";

// The one status-badge component. Four tones carry the meaning everywhere:
// red = blocks a decision or is a security risk; amber = stale or needs a
// refresh; green = passed its gate; grey = informational.
export type BadgeTone = "red" | "amber" | "green" | "grey";

export function Badge({ tone, sm, title, className, children }: {
  tone: BadgeTone;
  sm?: boolean;
  title?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span
      className={`badge badge-${tone}${sm ? " sm" : ""}${className ? ` ${className}` : ""}`}
      title={title}>
      {children}
    </span>
  );
}
