import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, type FlaggedPr, type ThreatDetail, type ThreatSecret } from "../api";
import { PRLink } from "../components/PRLink";
import { useRepoMeta } from "../RepoMetaContext";
import type { ThreatFocus } from "../threatBanner";

// How often the view re-reads the threat state, so a PR closed upstream or a
// new incident shows up without a reload — sooner while the backend's PR
// snapshot is still on its first load.
const POLL_MS = 60_000;
const LOADING_POLL_MS = 3_000;

const STATE_CHIP: Record<string, string> = {
  open: "chip-red", merged: "chip-red", closed: "chip-muted",
};

function focusOf(raw: string | null): ThreatFocus | null {
  return raw === "malicious" || raw === "credentials" ? raw : null;
}

function FlaggedTable({ rows }: { rows: FlaggedPr[] }) {
  const { prUrl } = useRepoMeta();
  return (
    <table className="grid compact">
      <thead><tr>
        <th>PR</th><th>Verdict</th><th>Author</th><th>Signatures</th><th>First noticed</th>
      </tr></thead>
      <tbody>
        {rows.map((f) => (
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
  );
}

function SecretsTable({ rows }: { rows: ThreatSecret[] }) {
  return (
    <table className="grid compact">
      <thead><tr><th>PR</th><th>Author</th><th>What</th><th>Evidence</th></tr></thead>
      <tbody>
        {rows.map((it) => (
          <tr key={it.id}>
            <td className="mono"><PRLink n={it.pr} /></td>
            <td className="mono">
              {it.author ?? "—"}
              {it.maintainer && <> <span className="chip chip-muted sm">maintainer</span></>}
            </td>
            <td>
              {it.summary}
              {it.fixture && <> <span className="chip chip-muted sm">likely fixture</span></>}
            </td>
            <td className="mono small"><code>{it.evidence || "—"}</code></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RotateNote() {
  return (
    <p className="muted small">
      Rotate a real key at its provider. Closing or merging the PR does not invalidate a pushed
      secret. Mark each one handled in <Link to="/security/actions">Action items</Link>.
    </p>
  );
}

export default function Threats() {
  const [params] = useSearchParams();
  const focus = focusOf(params.get("show"));
  const [detail, setDetail] = useState<ThreatDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const load = () => {
      api.threats()
        .then((d) => {
          if (cancelled) return;
          setDetail(d);
          setErr(null);
          timer = window.setTimeout(load, d.loading ? LOADING_POLL_MS : POLL_MS);
        })
        .catch((e: unknown) => {
          if (cancelled) return;
          setErr(e instanceof Error ? e.message : String(e));
          timer = window.setTimeout(load, POLL_MS);
        });
    };
    load();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, []);

  const malicious = detail?.flagged.filter((f) => f.verdict === "malicious") ?? [];
  return (
    <div className="activity threats">
      <div className="detail-head">
        <h1>⛔ Threats</h1>
        {focus ? (
          <p className="muted">
            Showing only what the banner names. <Link to="/security/threats">Show every threat</Link>
          </p>
        ) : (
          <p className="muted">
            What the deterministic threat scan found in incoming PR diffs: supply-chain attack patterns
            (obfuscated self-decoders, smuggled capabilities, build-config require injection) and authors on
            the blocklist. A malicious PR can never merge here. Close it on GitHub, check the author&apos;s
            other work, and report the account. A worker scans every new or pushed-to PR within minutes.
          </p>
        )}
      </div>
      {err && <div className="error">Failed to load threats: {err}</div>}
      {(!detail || detail.loading) && !err && (
        <div className="muted">Loading PRs from the shared database…</div>
      )}
      {detail && !detail.loading && focus === "malicious" && (
        <>
          <h2>Open PRs flagged malicious</h2>
          {malicious.length === 0 ? (
            <div className="callout">No open PR is flagged malicious.</div>
          ) : (
            <>
              <FlaggedTable rows={malicious} />
              <p className="muted small">
                Each is a hard merge block. Close it on GitHub, check the author&apos;s other work, and
                report the account.
              </p>
            </>
          )}
        </>
      )}
      {detail && !detail.loading && focus === "credentials" && (
        <>
          <h2>Credentials leaked in maintainers&apos; PRs</h2>
          {detail.secrets.length === 0 ? (
            <div className="callout">No maintainer&apos;s PR holds a credential to rotate.</div>
          ) : (
            <>
              <SecretsTable rows={detail.secrets} />
              <RotateNote />
            </>
          )}
        </>
      )}
      {detail && !detail.loading && !focus && (
        <>
          <h2>Open flagged PRs</h2>
          {detail.flagged.length === 0 ? (
            <div className="callout">No open PR is flagged.</div>
          ) : (
            <FlaggedTable rows={detail.flagged} />
          )}
          {malicious.length > 0 && (
            <p className="muted small">
              {malicious.length} malicious · each is a hard merge block and is listed first on Home.
            </p>
          )}

          <h2>Credentials leaked in maintainers&apos; PRs</h2>
          {detail.secrets.length === 0 ? (
            <div className="callout">No maintainer&apos;s PR holds a credential to rotate.</div>
          ) : (
            <>
              <SecretsTable rows={detail.secrets} />
              <RotateNote />
            </>
          )}

          <h2>Other leaked credentials</h2>
          <p className="muted small">
            Credentials contributors committed in their own PRs, usually their own deployment&apos;s secrets
            rather than the project&apos;s, and anything reading as a test fixture. They stay off the banner
            and out of Slack. Dismiss each in <Link to="/security/actions">Action items</Link>.
          </p>
          {detail.quiet_secrets.length === 0 ? (
            <div className="callout">None open.</div>
          ) : (
            <SecretsTable rows={detail.quiet_secrets} />
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
