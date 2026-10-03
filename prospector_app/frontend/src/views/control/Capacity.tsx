import type { CapacityAccount, CapacityState, CapacityWindow } from "../../api";
import { Panel } from "./Panel";
import { ageText, along, fmt, localTime, percent } from "./format";

/** One usage window as a bar, with a marker at the line unattended work may
 *  start under. The fill turns amber at the marker and red at the limit. */
function CapacityBar({ label, win, marker, markerLabel, markerTitle }: {
  label: string;
  win: CapacityWindow | null;
  marker: number | null;
  markerLabel: string;
  markerTitle?: string;
}) {
  if (win == null) {
    return (
      <div className="capacity-row">
        <span className="capacity-row-label">{label}</span>
        <span className="muted small">no reading</span>
      </div>
    );
  }
  const tone = win.utilization >= 1 ? " full" : marker != null && win.utilization >= marker ? " over" : "";
  const edge = marker == null ? "" : marker > 0.85 ? " edge-right" : marker < 0.15 ? " edge-left" : "";
  return (
    <div className="capacity-row">
      <span className="capacity-row-label">{label}</span>
      <div className={`capacity-bar${marker != null ? " marked" : ""}`} role="meter" aria-label={`${label} window used`}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(win.utilization * 100)}>
        <div className="capacity-track">
          <div className={`capacity-fill${tone}`} style={{ width: along(win.utilization) }} />
        </div>
        {marker != null && (
          <div className={`capacity-marker${edge}`} style={{ left: along(marker) }} title={markerTitle}>
            <span className="capacity-marker-label">{markerLabel}</span>
          </div>
        )}
      </div>
      <span className="capacity-row-value">{percent(win.utilization)} · resets {localTime(win.resets_at)}</span>
    </div>
  );
}

function spendToday(a: CapacityAccount): number {
  return Object.values(a.spend_today_by_lane).reduce((sum, usd) => sum + usd, 0);
}

function AccountCard({ account: a }: { account: CapacityAccount }) {
  const spend = Object.entries(a.spend_today_by_lane).sort((x, y) => y[1] - x[1]);
  const used = spendToday(a);
  const budget = a.policy.daily_budget_usd;
  const split = a.policy.day_cap !== a.policy.night_cap;
  const period = a.cap_now === a.policy.day_cap ? "day" : "night";
  const capLabel = a.cap_now == null ? "" : `cap ${percent(a.cap_now)}${split ? ` (${period})` : ""}`;
  const capTitle = split && a.next_boundary
    ? `The ${period} cap holds until ${localTime(a.next_boundary)}` : undefined;
  return (
    <div className="capacity-account">
      <div className="capacity-head">
        <b>{a.label}</b>
        {a.this_machine && <span className="chip chip-muted sm">this machine</span>}
        {a.billing === "api" && <span className="chip chip-muted sm">API key</span>}
        <span className="muted small">
          {a.machines.length > 0 ? a.machines.join(", ") : "no worker machine on it"}
        </span>
      </div>
      {a.billing === "subscription" ? (
        <>
          <CapacityBar label="5-hour" win={a.reading?.five_hour ?? null} marker={a.cap_now}
            markerLabel={capLabel} markerTitle={capTitle} />
          <CapacityBar label="weekly" win={a.reading?.seven_day ?? null} marker={a.pacing_line}
            markerLabel="pace" markerTitle="The weekly share unattended work may reach by now" />
        </>
      ) : budget == null ? (
        <div className="muted small">
          No daily budget set — unattended AI work stays off until one is set on its machine's Setup tab.
        </div>
      ) : (
        <div className="capacity-row">
          <span className="capacity-row-label">budget</span>
          <div className="capacity-bar">
            <div className="capacity-track">
              <div className={`capacity-fill${used >= budget ? " over" : ""}`}
                style={{ width: along(budget > 0 ? used / budget : 1) }} />
            </div>
          </div>
          <span className="capacity-row-value">${used.toFixed(2)} of ${budget.toFixed(2)} today</span>
        </div>
      )}
      <div className="small">
        {a.decision.allowed
          ? <span className="capacity-ok">● Unattended AI work may start</span>
          : (
            <span className="capacity-paused">
              Paused — {a.decision.reason}
              {a.decision.retry_at && ` · resumes ~${localTime(a.decision.retry_at)}`}
            </span>
          )}
      </div>
      <div className="capacity-meta muted small">
        <span title={a.reading ? `read by ${a.reading.by} at ${fmt(a.reading.at)}` : undefined}>
          {a.reading && a.reading_age_seconds != null
            ? `reading ${ageText(a.reading_age_seconds)} old` : "no reading yet"}
        </span>
        <span>·</span>
        {spend.length > 0
          ? spend.map(([lane, usd]) => (
            <span key={lane} className="chip chip-muted sm" title={`Today's unattended ${lane} agent spend`}>
              {lane} ${usd.toFixed(2)}
            </span>
          ))
          : <span>no background AI use today</span>}
      </div>
    </div>
  );
}

/** One account as a line: its windows, whether background work may start,
 *  and today's spend. */
function capacitySummary(a: CapacityAccount): string {
  const parts: string[] = [a.label];
  if (a.billing === "subscription") {
    if (a.reading?.five_hour) parts.push(`5h ${percent(a.reading.five_hour.utilization)}`);
    if (a.reading?.seven_day) parts.push(`weekly ${percent(a.reading.seven_day.utilization)}`);
  }
  parts.push(a.decision.allowed ? "background AI on" : "background AI paused");
  parts.push(`$${spendToday(a).toFixed(2)} today`);
  return parts.join(" · ");
}

/** Every AI account the machines run under, with the lines unattended agent
 *  work starts under, whether it may start now, and today's spend. */
export function Capacity({ state }: { state: CapacityState | null }) {
  const summary = state == null ? "loading…"
    : state.accounts.length === 0 ? "no AI account known yet"
    : state.accounts.map(capacitySummary).join("  |  ");
  return (
    <Panel id="capacity" title="AI capacity" foldable defaultOpen={false} summary={summary}>
      {state == null ? <div className="muted small">Loading…</div>
        : state.accounts.length === 0
          ? <p className="muted small" style={{ margin: 0 }}>
              No AI account known yet — a machine reports its account once its Claude CLI is signed in.
            </p>
          : <div className="capacity-grid">{state.accounts.map((a) => <AccountCard key={a.key} account={a} />)}</div>}
    </Panel>
  );
}
