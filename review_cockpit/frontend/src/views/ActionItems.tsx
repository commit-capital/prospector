import { useCallback, useEffect, useState } from "react";
import { api, type ActionItem } from "../api";
import { useRepoMeta } from "../RepoMetaContext";
import { PRLink } from "../components/PRLink";
import { InfoTip } from "../components/InfoTip";
import { useTableSort } from "../useTableSort";

const STATUS_TIP: Record<string, string> = {
  "open": "Still needs handling — in your active checklist.",
  "done": "You handled it — moved out of the open list (saved). Nothing was posted to GitHub.",
  "dismissed": "Won't do — moved out of the open list (saved). Nothing was posted to GitHub.",
};

const KIND_META: Record<string, { icon: string; label: string; tip: string }> = {
  "rotate-secret": { icon: "🔑", label: "Potential secret leaked", tip: "A live-looking credential was committed in this PR's diff. Confirm it: if real, rotate it at the provider and notify upstream (closing/merging the PR does NOT invalidate an already-pushed secret). Dismiss if it's a false positive — e.g. a public record id, not a credential." },
  "salvage-fix": { icon: "🩹", label: "Salvage fix", tip: "This PR is being rejected, but it bundles a legitimate fix worth extracting into a clean first-party PR." },
  "notify-upstream": { icon: "📣", label: "Notify upstream", tip: "Something the upstream maintainers should be told about." },
  "block-actor": { icon: "🚫", label: "Block actor", tip: "An author worth blocking or reviewing." },
  "review": { icon: "👀", label: "Review", tip: "Needs operator review." },
};

const STATUSES = ["open", "done", "dismissed"] as const;

export default function ActionItems() {
  const { prUrl } = useRepoMeta();
  const [items, setItems] = useState<ActionItem[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [status, setStatus] = useState<string>("open");
  const [kind, setKind] = useState<string>("");
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    () => api.actionItems({ status: status || undefined, kind: kind || undefined })
      .then((d) => { setItems(d.items); setCounts(d.counts); }),
    [status, kind],
  );
  useEffect(() => { void load(); }, [load]);

  const setItemStatus = async (id: string, s: string) => {
    setBusy(id);
    await api.setActionItemStatus(id, s);
    setBusy(null);
    load();
  };

  const kinds = Array.from(new Set(items.map((i) => i.kind)));

  // every column sortable (#180); rows are fully loaded, so sort client-side
  const sort = useTableSort<ActionItem>({
    kind: (it) => KIND_META[it.kind]?.label ?? it.kind,
    pr: (it) => it.pr,
    what: (it) => it.summary,
    evidence: (it) => it.evidence ?? "",
    status: (it) => it.status,
  }, { descFirst: ["pr"], tiebreak: (a, b) => a.pr - b.pr });
  const shown = sort.sorted(items);

  return (
    <div className="activity">
      <div className="detail-head">
        <h1>🗂️ Action items</h1>
        <p className="muted">
          Operator follow-ups the triage surfaced that aren't a merge/close decision — rotate a leaked
          credential, salvage a fix out of a rejected PR, notify upstream. Check them off as you handle them.
        </p>
        <p className="muted small">
          This is your private checklist — <b>nothing here is posted to GitHub</b>. <b>✓ done</b> = you handled
          it · <b>dismiss</b> = won't do. Both move it out of the <i>open</i> list and are saved across restarts;
          <b> ↩ reopen</b> brings it back.
        </p>
      </div>

      <div className="board-controls">
        <div className="segmented">
          {STATUSES.map((s) => (
            <button key={s} className={status === s ? "on" : ""} onClick={() => setStatus(s)}>
              {s} <span className="count">{counts[s] ?? 0}</span>
            </button>
          ))}
          <button className={status === "" ? "on" : ""} onClick={() => setStatus("")}>all</button>
        </div>
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">all kinds</option>
          {kinds.map((k) => <option key={k} value={k}>{KIND_META[k]?.label ?? k}</option>)}
        </select>
        <button className="btn-secondary sm" onClick={load}>↻ refresh</button>
      </div>

      <table className="grid compact sortable">
        <thead><tr>
          <th {...sort.thProps("kind")}>Kind{sort.indicator("kind")}</th>
          <th {...sort.thProps("pr")}>PR{sort.indicator("pr")}</th>
          <th {...sort.thProps("what")}>What to do{sort.indicator("what")}</th>
          <th {...sort.thProps("evidence")}>Evidence{sort.indicator("evidence")}</th>
          <th {...sort.thProps("status")}>Status{sort.indicator("status")}</th>
          <th></th>
        </tr></thead>
        <tbody>
          {shown.map((it) => {
            const m = KIND_META[it.kind] ?? { icon: "•", label: it.kind, tip: "" };
            return (
              <tr key={it.id} className={it.status !== "open" ? "row-skipped" : ""}>
                <td><InfoTip entry={m.tip ? { title: m.label, meaning: m.tip } : null} cue={false} focusable={false}><span className="chip chip-muted">{m.icon} {m.label}</span></InfoTip></td>
                <td className="mono">
                  <PRLink n={it.pr} />{" "}
                  <a className="muted small" href={it.pr_url ?? prUrl(it.pr)}
                     target="_blank" rel="noreferrer" title="Open on GitHub">↗</a>
                  {it.pr_title && <div className="muted small">{it.pr_title.slice(0, 60)}</div>}
                  {it.pr_summary && <div className="small" style={{ maxWidth: 280, whiteSpace: "normal" }} title="Diff-grounded agent summary"><i>{it.pr_summary}</i></div>}
                </td>
                <td>
                  {it.summary}
                  {it.detail && <div className="muted small">{it.detail.slice(0, 180)}</div>}
                </td>
                <td className="mono small" style={{ maxWidth: 320, wordBreak: "break-all" }}>
                  {it.evidence ? <code>{it.evidence.slice(0, 120)}</code> : <span className="muted">—</span>}
                </td>
                <td><span className={`chip chip-${it.status === "open" ? "yellow" : it.status === "done" ? "green" : "muted"}`} title={STATUS_TIP[it.status]}>{it.status}</span></td>
                <td>
                  {it.status === "open" ? (
                    <>
                      <button className="btn-secondary sm" disabled={busy === it.id} title="Mark handled — moves it out of the open list (saved). Posts nothing to GitHub." onClick={() => setItemStatus(it.id, "done")}>✓ done</button>{" "}
                      <button className="btn-secondary sm" disabled={busy === it.id} title="Won't do — moves it out of the open list (saved). Posts nothing to GitHub." onClick={() => setItemStatus(it.id, "dismissed")}>dismiss</button>
                    </>
                  ) : (
                    <button className="btn-secondary sm" disabled={busy === it.id} title="Move this back to the open list." onClick={() => setItemStatus(it.id, "open")}>↩ reopen</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {items.length === 0 && <div className="callout">No {status || ""} action items.</div>}
    </div>
  );
}
