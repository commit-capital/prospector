import type { ControlOverview, OverviewTimelineItem } from "../../api";
import { FlyoutIssueLink } from "../../components/FlyoutIssueLink";
import { PRLink } from "../../components/PRLink";

const LANE_LABEL: Record<string, string> = {
  security: "Security", verify: "Verify", design: "Design", autofix: "Autofix",
  "issue-fix": "Issue fix", cluster: "Clustering", background: "Background",
};

function clock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function Line({ t }: { t: OverviewTimelineItem }) {
  const when = t.when === "now" ? "now" : t.when === "upcoming" && t.lane !== "background" ? "next" : clock(t.at);
  return (
    <div className={`tl-row tl-${t.when}${t.bad ? " activity-bad" : ""}`}>
      <span className="tl-when">{when}</span>
      <span className="tl-lane">{LANE_LABEL[t.lane] ?? t.lane}</span>
      <span className="tl-text">
        {t.pr != null ? <><PRLink n={t.pr} />{t.text.replace(/^#\d+/, "")}</>
          : t.issue != null ? <><FlyoutIssueLink n={t.issue} />{t.text.replace(/^#\d+/, "")}</>
            : t.text}
        {t.host && <span className="muted"> · {t.host}</span>}
      </span>
    </div>
  );
}

/** What runs next, what runs now, and what ran in the past day, in one list. */
export function Timeline({ overview }: { overview: ControlOverview }) {
  const upcoming = overview.timeline.filter((t) => t.when === "upcoming");
  const current = overview.timeline.filter((t) => t.when === "now");
  const past = overview.timeline.filter((t) => t.when === "past");
  return (
    <section className="cpanel" aria-label="Timeline">
      <div className="cpanel-head"><h3 className="cpanel-title">Timeline</h3>
        <span className="cpanel-meta">next hour · now · past 24 hours</span></div>
      <div className="cpanel-body timeline">
        {[...upcoming].reverse().map((t, i) => <Line key={`u${i}`} t={t} />)}
        <div className="tl-divider" role="separator">now</div>
        {current.map((t, i) => <Line key={`n${i}`} t={t} />)}
        {past.length === 0 ? <div className="muted small">Nothing ended in the past day.</div>
          : past.map((t, i) => <Line key={`p${i}`} t={t} />)}
      </div>
    </section>
  );
}
