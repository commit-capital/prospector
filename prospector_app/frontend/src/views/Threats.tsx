import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type ThreatDetail } from "../api";
import { PRLink } from "../components/PRLink";
import { useRepoMeta } from "../RepoMetaContext";

// How often the view re-reads the threat state, so a PR closed upstream or a
// new incident shows up without a reload.
const POLL_MS = 60_000;

const STATE_CHIP: Record<string, string> = {
  open: "chip-red", merged: "chip-red", closed: "chip-muted",
};

export default function Threats() {
  const { prUrl } = useRepoMeta();
  const [detail, setDetail] = useState<ThreatDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => api.threats()
      .then((d) => { if (!cancelled) { setDetail(d); setErr(null); } })
      .catch((e: unknown) => { if (!cancelled) setErr(e instanceof Error ? e.message : String(e)); });
    void load();
    const timer = window.setInterval(load, POLL_MS);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, []);

  const malicious = detail?.flagged.filter((f) => f.verdict === "malicious") ?? [];
  return (
    <div className="activity threats">
      <div className="detail-head">
        <h1>⛔ Threats</h1>
        <p className="muted">
          What the deterministic threat scan found in incoming PR diffs: supply-chain attack patterns
          (obfuscated self-decoders, smuggled capabilities, build-config require injection) and authors on
          the blocklist. A malicious PR can never merge here. Close it on GitHub, check the author&apos;s
          other work, and report the account. A worker scans every new or pushed-to PR within minutes.
        </p>
      </div>
      {err && <div className="error">Failed to load threats: {err}</div>}
      {!detail && !err && <div className="muted">Loading…</div>}
      {detail && (
        <>
          <h2>Open flagged PRs</h2>
          {detail.flagged.length === 0 ? (
            <div className="callout">No open PR is flagged.</div>
          ) : (
            <table className="grid compact">
              <thead><tr>
                <th>PR</th><th>Verdict</th><th>Author</th><th>Signatures</th><th>First noticed</th>
              </tr></thead>
              <tbody>
                {detail.flagged.map((f) => (
                  <tr key={f.pr}>
                    <td className="mono">
                      <PRLink n={f.pr} />{" "}
                      <a className="muted small" href={f.url ?? prUrl(f.pr)} target="_blank" rel="noreferrer"
                        title="Open on GitHub">↗</a>
                      {f.title && <div className="muted small">{f.title.slice(0, 80)}</div>}
                    </td>
                    <td><span className={`chip ${f.verdict === "malicious" ? "chip-red" : "chip-yellow"}`}>
                      {f.verdict}</span></td>
                    <td className="mono">{f.author ?? "—"}</td>
                    <td className="mono small">{f.signatures.join(", ") || "—"}</td>
                    <td className="small">{f.noticed ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {malicious.length > 0 && (
            <p className="muted small">
              {malicious.length} malicious · each is a hard merge block and is listed first on Home.
            </p>
          )}

          <h2>Leaked credentials to rotate</h2>
          {detail.secrets.length === 0 ? (
            <div className="callout">No open rotate-secret item.</div>
          ) : (
            <>
              <table className="grid compact">
                <thead><tr><th>PR</th><th>What</th><th>Evidence</th></tr></thead>
                <tbody>
                  {detail.secrets.map((it) => (
                    <tr key={it.id}>
                      <td className="mono"><PRLink n={it.pr} /></td>
                      <td>
                        {it.summary}
                        {it.fixture && <> <span className="chip chip-muted sm">likely fixture</span></>}
                      </td>
                      <td className="mono small"><code>{it.evidence || "—"}</code></td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="muted small">
                Rotate a real key at its provider. Closing or merging the PR does not invalidate a pushed
                secret. Mark each one handled in <Link to="/security/actions">Action items</Link>.
              </p>
            </>
          )}

          <h2>Blocked authors</h2>
          {detail.actors.length === 0 ? (
            <div className="callout">No author is blocked.</div>
          ) : (
            <table className="grid compact">
              <thead><tr><th>Author</th><th>Why</th><th>Blocked</th><th>Incidents</th><th>Still open</th></tr></thead>
              <tbody>
                {detail.actors.map((a) => (
                  <tr key={a.login}>
                    <td className="mono">{a.login}</td>
                    <td className="small">{a.reason ?? "—"}</td>
                    <td className="small">{a.added ?? "—"}</td>
                    <td className="mono small">
                      {a.incidents.map((n, i) => <span key={n}>{i > 0 && ", "}<PRLink n={n} /></span>)}
                    </td>
                    <td className="mono small">
                      {a.open_prs.length === 0 ? <span className="muted">none</span>
                        : a.open_prs.map((n, i) => <span key={n}>{i > 0 && ", "}<PRLink n={n} /></span>)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h2>Incident log</h2>
          {detail.incidents.length === 0 ? (
            <div className="callout">No incident recorded.</div>
          ) : (
            <table className="grid compact">
              <thead><tr><th>PR</th><th>State</th><th>Author</th><th>Signatures</th><th>First noticed</th></tr></thead>
              <tbody>
                {detail.incidents.map((inc) => (
                  <tr key={inc.pr}>
                    <td className="mono">
                      <PRLink n={inc.pr} />
                      {inc.title && <div className="muted small">{inc.title.slice(0, 80)}</div>}
                    </td>
                    <td>
                      <span className={`chip ${STATE_CHIP[inc.state ?? ""] ?? "chip-muted"}`}
                        title={inc.state === "merged" ? "This flagged PR merged — audit what it shipped." : undefined}>
                        {inc.state ?? "not in store"}
                      </span>
                    </td>
                    <td className="mono">{inc.author ?? "—"}</td>
                    <td className="mono small">{inc.signatures.join(", ") || "—"}</td>
                    <td className="small">{inc.noticed ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
