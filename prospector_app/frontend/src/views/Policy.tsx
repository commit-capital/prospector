import { useEffect, useState } from "react";
import { NavLink } from "react-router";
import { api, type SetupReadiness, type WorkerFlags } from "../api";
import { IDENTITY_ROLES, isOn, SWITCHES, switchId } from "../autonomy";
import { useExec } from "../ExecContext";

/** The autonomy disclosure the header's mode cluster links to: what this
 *  deployment does without asking, and under which identity — read-only, with
 *  Setup as the one place the switches are changed. */
export default function Policy() {
  const { botLogin, dryRun, livePossible, liveError, pushIdentity, login, storeWriteBlock } = useExec();
  const [readiness, setReadiness] = useState<SetupReadiness | null>(null);
  const [flags, setFlags] = useState<WorkerFlags | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.setupReadiness()
      .then((r) => { setReadiness(r.readiness); setFlags(r.flags); })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  return (
    <div className="pad policy">
      <h2>📜 Policy</h2>
      <p className="muted">
        What this deployment does without asking, and under which identity.
        The switches are per machine and change on the{" "}
        <NavLink to="/setup">Setup</NavLink> page.
      </p>

      <section className="setup-card">
        <h3>Mode</h3>
        <p>
          This tab is in{" "}
          {dryRun
            ? <><span className="chip chip-amber sm">dry run</span> — every action previews; nothing is posted or pushed.</>
            : <><span className="chip chip-red sm">live</span> — actions post upstream and push to PR branches.</>}
        </p>
        {storeWriteBlock
          ? <p className="muted small">⛔ {storeWriteBlock}</p>
          : !livePossible && (
            <p className="muted small">
              Live mode is unavailable on this machine — no {botLogin} token
              {liveError ? ` (${liveError})` : ""}.
            </p>
          )}
      </section>

      <section className="setup-card">
        <h3>Identities</h3>
        <table className="rows">
          <tbody>
            <tr>
              <td><b>{botLogin}</b></td>
              <td className="muted small">{IDENTITY_ROLES.bot} — the bot App every gated executor write runs as</td>
            </tr>
            <tr>
              <td><b>{pushIdentity?.login ?? "not configured"}</b></td>
              <td className="muted small">
                {IDENTITY_ROLES.push} — a GitHub user over its own SSH key
                {pushIdentity?.available ? "" : " (not held on this machine, so unattended pushes run elsewhere or refuse)"}
              </td>
            </tr>
            <tr>
              <td><b>{login ?? "—"}</b></td>
              <td className="muted small">{IDENTITY_ROLES.operator} — the operator's own login, used for every read</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section className="setup-card">
        <h3>What runs without asking{readiness ? ` on ${readiness.host}` : ""}</h3>
        {error && <p className="chip chip-red sm">{error}</p>}
        {!flags && !error && <p className="muted small">reading this machine…</p>}
        {flags && (
          <table className="rows">
            <tbody>
              {SWITCHES.map((s) => (
                <tr key={switchId(s)}>
                  <td>
                    <span className={`chip sm ${isOn(flags, s) ? "chip-green" : "chip-muted"}`}>
                      {isOn(flags, s) ? "on" : "off"}
                    </span>
                  </td>
                  <td>{s.label}</td>
                  <td className="muted small">{s.hint}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="muted small">
          Whatever is on, an autofixed PR still faces the per-PR merge gate
          unchanged, and executor writes — dry-runs included — appear in the
          Activity log.
        </p>
      </section>
    </div>
  );
}
