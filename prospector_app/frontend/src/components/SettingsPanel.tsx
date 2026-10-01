import { useEffect, useState } from "react";
import { api, type SettingRow, type SettingsReport } from "../api";

/** Every setting the code reads (pipeline/settings_registry.py), as this machine
 *  runs it: the automation's behavior and this machine's sandbox are editable
 *  here; the worker switches are set in the work-queue card above; the
 *  deployment's values come from the setup wizard; the advanced ones are shown
 *  so nothing is hidden. `refresh` changes whenever the page's worker switches
 *  do, so their rows here stay current. */
export function SettingsPanel({ refresh }: { refresh: unknown }) {
  const [report, setReport] = useState<SettingsReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(0);

  useEffect(() => {
    let live = true;
    api.setupSettings()
      .then((r) => { if (live) setReport(r); })
      .catch((e: unknown) => { if (live) setError(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [refresh, saved]);
  const load = async (): Promise<void> => { setSaved((n) => n + 1); };

  if (!report) {
    return (
      <section className="setup-card">
        <h3>🎛️ Settings</h3>
        {error ? <p className="chip chip-red sm">{error}</p> : <p className="muted small">reading settings…</p>}
      </section>
    );
  }

  const rows = (group: string): SettingRow[] => report.settings.filter((s) => s.group === group);
  const label = (group: string): string => report.groups.find((g) => g.id === group)?.label ?? group;

  return (
    <section className="setup-card">
      <h3>🎛️ Settings</h3>
      <p className="muted small">
        Everything this machine's Prospector reads from its <code>.env</code>, with the value in
        effect. Changes save to this machine's <code>.env</code> and apply at once.
      </p>
      {error && <p className="chip chip-red sm">{error}</p>}
      {(["behavior", "machine"] as const).map((g) => (
        <SettingsTable key={g} title={label(g)} rows={rows(g)} onSaved={load} onError={setError} />
      ))}
      <SettingsTable title={`${label("workers")} — switched in the work-queue card above`}
        rows={rows("workers")} readOnly onSaved={load} onError={setError} />
      <SettingsTable title={`${label("deployment")} — set by the setup wizard`}
        rows={rows("deployment")} readOnly onSaved={load} onError={setError} />
      <details>
        <summary className="muted small">{label("advanced")} ({rows("advanced").length})</summary>
        <SettingsTable title="" rows={rows("advanced")} readOnly onSaved={load} onError={setError} />
      </details>
    </section>
  );
}

function SettingsTable({ title, rows, readOnly = false, onSaved, onError }: {
  title: string;
  rows: SettingRow[];
  readOnly?: boolean;
  onSaved: () => Promise<void>;
  onError: (msg: string | null) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <>
      {title && <h4 style={{ margin: "14px 0 4px" }}>{title}</h4>}
      <table className="rows">
        <tbody>
          {rows.map((s) => (
            <tr key={s.name}>
              <td style={{ width: "45%" }}>
                <b>{s.label}</b>
                <div className="muted small">{s.help}</div>
                <code className="muted small">{s.name}</code>
              </td>
              <td>
                {s.editable && !readOnly
                  ? <SettingControl key={`${s.name}:${s.value}`} s={s} onSaved={onSaved}
                      onError={onError} />
                  : <ShownValue s={s} />}
              </td>
              <td className="muted small">
                default {s.default}
                {s.effective && <> · in effect {s.effective}</>}
                <div><span className={`chip sm ${s.source === "default" ? "" : "chip-amber"}`}>
                  {s.source === "default" ? "default" : `from ${s.source}`}</span></div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}

function ShownValue({ s }: { s: SettingRow }) {
  if (s.kind === "secret") return <span>{s.value ? "set (hidden)" : <span className="muted">not set</span>}</span>;
  if (!s.value) return <span className="muted">—</span>;
  if (s.kind === "bool") return <span>{s.value === "1" ? "on" : "off"}</span>;
  return <code>{s.value}</code>;
}

function SettingControl({ s, onSaved, onError }: {
  s: SettingRow;
  onSaved: () => Promise<void>;
  onError: (msg: string | null) => void;
}) {
  // Keyed on its value by the table, so a saved or reloaded value starts a
  // fresh draft.
  const [draft, setDraft] = useState<string>(s.value);
  const [busy, setBusy] = useState(false);

  async function save(value: string) {
    if (value === s.value) return;
    setBusy(true);
    onError(null);
    try {
      await api.setSetupFlags({ [s.name]: value });
      await onSaved();
    } catch (e) {
      onError(`${s.label}: ${e instanceof Error ? e.message : String(e)}`);
      setDraft(s.value);
    } finally {
      setBusy(false);
    }
  }

  if (s.kind === "bool") {
    const onByDefault = s.default === "on";
    const checked = s.value === "1" || (s.value === "" && onByDefault);
    return (
      <input type="checkbox" checked={checked} disabled={busy}
        onChange={(e) => void save(e.target.checked ? "1" : onByDefault ? "0" : "")} />
    );
  }
  if (s.kind === "choice") {
    return (
      <select value={s.value || s.default} disabled={busy} onChange={(e) => void save(e.target.value)}>
        {s.choices.map((c) => <option key={c} value={c}>{c}</option>)}
      </select>
    );
  }
  const commit = () => void save(draft.trim());
  return (
    <input type={s.kind === "int" ? "number" : "text"} value={draft} disabled={busy}
      min={s.kind === "int" ? s.minimum : undefined}
      max={s.kind === "int" && s.maximum != null ? s.maximum : undefined}
      placeholder={s.default} style={{ width: s.kind === "int" ? 90 : 220 }}
      onChange={(e) => setDraft(e.target.value)} onBlur={commit}
      onKeyDown={(e) => { if (e.key === "Enter") commit(); }} />
  );
}
