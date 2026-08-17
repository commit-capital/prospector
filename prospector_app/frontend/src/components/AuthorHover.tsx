import type { ReactNode } from "react";
import type { AuthorStats } from "../api";
import { InfoTip } from "./InfoTip";
import { TrustedAuthorName } from "./TrustedAuthor";

function pctOf(rate: number | null | undefined): number | null {
  if (rate == null) return null;
  return Math.round(rate <= 1 ? rate * 100 : rate);
}

function authorBody(stats: AuthorStats, trusted: boolean): ReactNode {
  const pct = pctOf(stats.merge_rate);
  const rows: Array<[string, ReactNode]> = [];
  if (pct != null) rows.push(["Merge rate", `${pct}%`]);
  if (stats.total != null) rows.push(["Total PRs", stats.total]);
  if (stats.merged != null) rows.push(["Merged", stats.merged]);
  if (stats.open != null) rows.push(["Open", stats.open]);
  if (stats.closed_unmerged != null) rows.push(["Closed unmerged", stats.closed_unmerged]);
  if (stats.comments != null) rows.push(["Comments", stats.comments]);
  if (stats.issues_filed != null) rows.push(["Issues filed", stats.issues_filed]);
  if (stats.issues_resolved != null) rows.push(["Issues resolved", stats.issues_resolved]);
  return (
    <div className="tip-rows">
      <div className="tip-head">
        @<TrustedAuthorName author={stats.handle} trusted={trusted} />
      </div>
      {rows.map(([label, value]) => (
        <div key={label} className="tip-row"><span>{label}</span><b>{value}</b></div>
      ))}
    </div>
  );
}

export function AuthorHover({
  author,
  trusted = false,
  stats,
  fallback = "—",
  children,
}: {
  author: string | null | undefined;
  trusted?: boolean;
  stats?: AuthorStats | null;
  fallback?: string;
  children?: ReactNode;
}): ReactNode {
  const label = children ?? <TrustedAuthorName author={author} trusted={trusted} fallback={fallback} />;
  if (!stats) return label;
  return (
    <InfoTip body={authorBody(stats, trusted)} cue={false} focusable={false}>
      <span>{label}</span>
    </InfoTip>
  );
}
