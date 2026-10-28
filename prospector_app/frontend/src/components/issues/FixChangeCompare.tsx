import { useMemo, useState } from "react";
import type { IssueFixRun, IssuePR } from "../../api";
import { useRepoMeta } from "../../RepoMetaContext";
import { DiffGrid, type DiffGridHead, type PatchColumn } from "../DiffGrid";
import { DiffView } from "../DiffView";
import { referencedPrs } from "./issueLinkChips";

/** The auto-fix's change, with a toggle per other PR that may fix the issue:
 *  each one ticked on adds its diff as a column of the PR Differ's grid, the
 *  same file lined up across them. With only our change on it renders as the
 *  ordinary diff, line popup and all. */
export function FixChangeCompare({ run, linked }: { run: IssueFixRun; linked: IssuePR[] }) {
  const { prUrl } = useRepoMeta();
  const ours = run.proposal?.pr ?? null;
  const others = useMemo(
    () => referencedPrs(linked, ours).filter((p) => ours == null || String(p.pr) !== String(ours)),
    [linked, ours]);
  const [showFix, setShowFix] = useState(true);
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const fixLabel = `Auto-fix${ours != null ? ` (#${ours})` : ""}`;
  const patches = useMemo<PatchColumn[]>(
    () => showFix ? [{ id: "fix", label: fixLabel, title: run.summary ?? undefined, diffText: run.patch ?? "" }] : [],
    [showFix, fixLabel, run.summary, run.patch]);
  const rows = useMemo(() => Object.fromEntries(others.map((p): [number, DiffGridHead] =>
    [p.pr, { title: p.title ?? undefined, url: prUrl(p.pr), author: p.facts?.author ?? undefined }])),
  [others, prUrl]);

  if (!others.length) return <DiffView diffText={run.patch ?? ""} />;

  const prs = others.map((p) => p.pr).filter((n) => picked.has(n));
  const toggle = (n: number) => setPicked((s) => {
    const next = new Set(s);
    if (next.has(n)) next.delete(n); else next.add(n);
    return next;
  });
  const btn = (on: boolean) => `act-range-btn${on ? " act-range-btn-active" : ""}`;

  return (
    <>
      <div className="fix-compare-bar">
        <span className="muted small">Compare with:</span>
        <div className="act-range-picker">
          <button aria-pressed={showFix} className={btn(showFix)} onClick={() => setShowFix((v) => !v)}
                  title="This auto-fix's change">{fixLabel}</button>
          {others.map((p) => (
            <button key={p.pr} aria-pressed={picked.has(p.pr)} className={btn(picked.has(p.pr))}
                    onClick={() => toggle(p.pr)} title={p.title ?? undefined}>#{p.pr}</button>
          ))}
        </div>
      </div>
      {!showFix && !prs.length ? <div className="muted small">Turn on a change above to see it.</div>
        : showFix && !prs.length ? <DiffView diffText={run.patch ?? ""} />
        : (
          <div className="fix-compare-grid">
            <DiffGrid prs={prs} patches={patches} rows={rows} onRemove={toggle} />
          </div>
        )}
    </>
  );
}
