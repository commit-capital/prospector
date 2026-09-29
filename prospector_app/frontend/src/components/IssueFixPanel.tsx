import { useEffect, useState } from "react";
import {
  api, type IssueDetail, type IssueFixBody, type IssueFixCandidate, type IssueFixQuestion,
  type IssueFixRun, type IssueFixStatus, type IssueFixThreadEntry,
} from "../api";
import { useExec } from "../ExecContext";
import { useRepoMeta } from "../RepoMetaContext";
import { DiffView } from "./DiffView";

// How often the panel re-reads the issue while its request is queued or running.
const POLL_MS = 15_000;

const STATUS_LABEL: Record<IssueFixStatus, string> = {
  review: "Fix ready for your review",
  question: "Question for you",
  running: "Working",
  reporter: "Waiting on the reporter",
  "pr-open": "PR open",
  failed: "Didn't finish",
  declined: "No fix",
};
const STATUS_TONE: Record<IssueFixStatus, string> = {
  review: "green", question: "yellow", running: "blue", reporter: "muted",
  "pr-open": "purple", failed: "red", declined: "muted",
};

/** The issue's auto-fix status as a chip; nothing when it has no attempt. */
export function FixStatusChip({ status, reason }: { status?: IssueFixStatus | null; reason?: string | null }) {
  if (!status) return null;
  return (
    <span className={`chip chip-${STATUS_TONE[status]} sm`} title={reason ?? undefined}>
      {STATUS_LABEL[status]}
    </span>
  );
}

function when(iso?: string | null): string {
  return iso ? iso.replace("T", " ").slice(0, 16) : "";
}

/** The auto-fix section of an issue's detail: the latest attempt as the
 *  factory recorded it — each agent's account, where they agreed, the change,
 *  the checks, the reviewer — the thread, and the actions that fit it. */
export function IssueFixPanel({ d, onChanged }: { d: IssueDetail; onChanged: () => void }) {
  const run = d.fix_run ?? null;
  const req = d.fix_request ?? null;
  const inFlight = req?.status === "queued" || req?.status === "running";

  useEffect(() => {
    if (!inFlight) return;
    const t = window.setInterval(onChanged, POLL_MS);
    return () => window.clearInterval(t);
  }, [inFlight, onChanged]);

  return (
    <div className="issue-fix">
      <FixBanner d={d} onChanged={onChanged} />
      <FixActions d={d} onChanged={onChanged} />
      {run && <FixRunBody run={run} />}
      {(d.fix_thread?.length ?? 0) > 0 && <FixThread entries={d.fix_thread ?? []} />}
    </div>
  );
}

function FixBanner({ d, onChanged }: { d: IssueDetail; onChanged: () => void }) {
  const { pushToast } = useExec();
  const req = d.fix_request;
  const status = d.fix_status;
  if (req && (req.status === "queued" || req.status === "running")) {
    const cancel = () => api.cancelIssueFix(d.number)
      .then(() => { pushToast("Cancelled", "muted"); onChanged(); })
      .catch((e: Error) => pushToast("Couldn't cancel", "red", { detail: e.message }));
    return (
      <div className="verdict-banner v-unknown">
        <span className="vb-icon">{req.status === "queued" ? "⏳" : "🔄"}</span>
        <div>
          <div className="vb-headline">
            {req.status === "queued" ? "Queued" : "Running"}: {req.action}
            {req.source === "hunter" ? " (picked by the hunter)" : ""}
          </div>
          <div className="vb-detail">
            {req.requested_by && <>asked by {req.requested_by} · </>}
            {req.status === "queued" ? `queued ${when(req.queued_at)}` : `on ${req.host} since ${when(req.started_at)}`}
            {req.guidance && <div className="muted small">“{req.guidance}”</div>}
          </div>
          {req.status === "queued" && (
            <button className="btn-secondary sm" onClick={cancel}>Cancel</button>
          )}
        </div>
      </div>
    );
  }
  if (!status) return null;
  const tone = status === "review" || status === "pr-open" ? "v-green"
    : status === "failed" ? "v-red" : status === "question" ? "v-caution" : "v-unknown";
  return (
    <div className={`verdict-banner ${tone}`}>
      <span className="vb-icon">
        {status === "review" ? "✅" : status === "question" ? "❓" : status === "pr-open" ? "🔗"
          : status === "failed" ? "✗" : status === "reporter" ? "⏳" : "⛔"}
      </span>
      <div>
        <div className="vb-headline">{STATUS_LABEL[status]}</div>
        {status === "question"
          ? <div className="vb-detail">The agents read the report differently — the question is below.</div>
          : d.fix_reason && <div className="vb-detail">{d.fix_reason}</div>}
      </div>
    </div>
  );
}

function FixActions({ d, onChanged }: { d: IssueDetail; onChanged: () => void }) {
  const { dryRun, pushToast } = useExec();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const status = d.fix_status;
  const run = d.fix_run;
  const req = d.fix_request;
  if (d.state !== "open" || (req && (req.status === "queued" || req.status === "running"))) {
    return null;
  }

  const send = (body: IssueFixBody, done: string) => {
    setBusy(true);
    api.issueFix(d.number, body)
      .then(() => { pushToast(done, "green"); setText(""); onChanged(); })
      .catch((e: Error) => pushToast("Couldn't queue that", "red", { detail: e.message }))
      .finally(() => setBusy(false));
  };

  if (status === "question" || status === "reporter") {
    return <QuestionActions q={run?.question ?? null} asked={status === "reporter"} busy={busy}
                            text={text} setText={setText} send={send} dryRun={dryRun} />;
  }
  if (status === "pr-open") return null;
  if (status === "review") {
    return (
      <div className="fix-composer">
        <div className="row-actions">
          <button className="btn-primary sm" disabled={busy}
            title="Push the change to the bot's fork and open a pull request for maintainers"
            onClick={() => send({ action: "propose", dry_run: dryRun },
                                dryRun ? "Queued a dry-run proposal" : "Queued: open the PR")}>
            {dryRun ? "Open PR (dry run)" : "Open PR"}
          </button>
        </div>
        <textarea className="fix-goal" rows={3} value={text}
          placeholder="Not quite? Say what to change — or that it should start over — and send it back."
          onChange={(e) => setText(e.target.value)} aria-label="Your comments on the fix" />
        <div className="row-actions">
          <button className="btn-secondary sm" disabled={busy || !text.trim()}
            onClick={() => send({ action: "send-back", guidance: text }, "Sent back with your comments")}>
            Send back
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="fix-composer">
      <textarea className="fix-goal" rows={2} value={text}
        placeholder="Optional: anything the agents should know (where to look, what correct behavior is)."
        onChange={(e) => setText(e.target.value)} aria-label="Guidance for the fix" />
      <div className="row-actions">
        <button className="btn-primary sm" disabled={busy}
          title="Three agents independently reproduce and fix this; a fix they agree on comes back here for your review"
          onClick={() => send({ action: "solve", guidance: text || undefined }, "Queued: try to fix")}>
          {run ? "Try again" : "Try to fix"}
        </button>
      </div>
    </div>
  );
}

function QuestionActions({ q, asked, busy, text, setText, send, dryRun }: {
  q: IssueFixQuestion | null;
  asked: boolean;
  busy: boolean;
  text: string;
  setText: (t: string) => void;
  send: (body: IssueFixBody, done: string) => void;
  dryRun: boolean;
}) {
  if (!q || !q.options?.length) return null;
  return (
    <div className="callout issue-fix-question">
      <div><b>{q.question}</b></div>
      <div className="muted small" style={{ margin: "4px 0 8px" }}>
        The agents read the report differently. Pick what should happen and the fix continues.
      </div>
      {q.options.map((o) => (
        <div key={o.label} className="row-actions" style={{ alignItems: "baseline" }}>
          <button className="btn-secondary sm" disabled={busy}
            onClick={() => send({ action: "answer", answer_label: o.label }, `Answered ${o.label}`)}>
            {o.label}
          </button>
          <span>{o.behavior}{o.label === q.default && <span className="muted small"> (default)</span>}</span>
        </div>
      ))}
      <textarea className="fix-goal" rows={2} value={text}
        placeholder="Or say what should happen in your own words."
        onChange={(e) => setText(e.target.value)} aria-label="Your answer" />
      <div className="row-actions">
        <button className="btn-secondary sm" disabled={busy || !text.trim()}
          onClick={() => send({ action: "answer", answer_text: text }, "Answered")}>Answer</button>
        {!asked && (
          <button className="btn-secondary sm" disabled={busy}
            title="Post this question on the GitHub issue as the bot; the reporter's reply resumes the fix"
            onClick={() => send({ action: "ask-reporter", dry_run: dryRun },
                                dryRun ? "Queued a dry-run question" : "Queued: ask the reporter")}>
            {dryRun ? "Ask the reporter (dry run)" : "Ask the reporter on GitHub"}
          </button>
        )}
      </div>
      <div className="muted small">
        {asked
          ? <>Asked on GitHub {when(q.asked?.at)}{q.asked?.url && <> · <a href={q.asked.url} target="_blank" rel="noreferrer">the comment ↗</a></>}.
              {" "}Without a reply, {q.default} is used after a week.</>
          : <>Default: {q.default} — {q.default_reason}</>}
      </div>
    </div>
  );
}

function FixRunBody({ run }: { run: IssueFixRun }) {
  const { prUrl } = useRepoMeta();
  const review = run.reviews[0];
  const proof = run.proof;
  return (
    <>
      <div className="muted small" style={{ margin: "6px 0" }}>
        {run.lane} lane · {run.ending} · {run.models.join(", ")} · {run.agent_runs ?? "?"} agent run(s)
        {run.finished && <> · finished {when(run.finished)}</>}
        {run.base_sha && <> · base {run.base_sha.slice(0, 12)}</>}
        {run.host && <> · on {run.host}</>}
      </div>
      {run.proposal?.pr != null && (
        <div className="callout">
          Proposed as{" "}
          <a href={run.proposal.url ?? prUrl(Number(run.proposal.pr))} target="_blank" rel="noreferrer">
            #{String(run.proposal.pr)} ↗</a>
        </div>
      )}
      {(run.summary || run.root_cause) && (
        <>
          <h4>What the fix does</h4>
          {run.summary && <div><b>{run.summary}</b></div>}
          {run.root_cause && <div className="muted" style={{ marginTop: 4 }}>Root cause: {run.root_cause}</div>}
          {run.changes.length > 0 && (
            <ul style={{ margin: "6px 0 0 18px" }}>
              {run.changes.map((c) => <li key={c.path}><code>{c.path}</code>: {c.rationale}</li>)}
            </ul>
          )}
        </>
      )}
      {run.patch && (
        <>
          <h4>The change{run.tests.length > 0 && <span className="muted small"> · tests: {run.tests.join(", ")}</span>}</h4>
          <DiffView diffText={run.patch} />
          {run.patch_truncated && <div className="muted small">… the change is longer than shown.</div>}
        </>
      )}
      {(proof.compile || proof.related_tests || proof.suite) && (
        <>
          <h4>Checks</h4>
          <ul style={{ margin: "0 0 0 18px" }}>
            {proof.compile && (
              <li>Compile: {proof.compile.exit === 0 ? "passes"
                : proof.compile.tree_fails ? "fails on the base too (not this change's)"
                : `fails${proof.compile.error ? ` — ${proof.compile.error}` : ""}`}</li>
            )}
            {proof.related_tests && (
              <li>Related tests ({proof.related_tests.files.length}):{" "}
                {proof.related_tests.exit === 0 ? "pass" : proof.related_tests.base_fails
                  ? "fail on the base too" : "fail"}</li>
            )}
            {proof.suite && (
              <li>Full suite: {proof.suite.skipped ? `skipped — ${proof.suite.skipped}`
                : proof.suite.confirmed ? `new failures: ${(proof.suite.new_failures ?? []).join(", ")}`
                : `no new failures (${proof.suite.excluded ?? 0} already failing on the base)`}</li>
            )}
          </ul>
        </>
      )}
      {review && (
        <>
          <h4>Reviewer ({review.lens})</h4>
          <div className={`chip sm chip-${review.verdict === "safe" ? "green" : "red"}`}>
            {review.failed ? "didn't finish" : review.verdict}
          </div>
          {review.reason && <div style={{ marginTop: 4 }}>{review.reason}</div>}
          {review.concerns.length > 0 && (
            <ul style={{ margin: "4px 0 0 18px" }}>{review.concerns.map((c, i) => <li key={i}>{c}</li>)}</ul>
          )}
          {review.unasked.length > 0 && (
            <>
              <div className="muted small" style={{ marginTop: 4 }}>Also changes, beyond the report:</div>
              <ul style={{ margin: "2px 0 0 18px" }}>{review.unasked.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </>
          )}
        </>
      )}
      {run.candidates.length > 0 && <Candidates run={run} />}
    </>
  );
}

function Candidates({ run }: { run: IssueFixRun }) {
  const repros = run.agreement?.reproductions ?? [];
  const readingOf = (i: number): number | null => {
    const at = (run.readings ?? []).findIndex((r) => r.includes(i));
    return at < 0 ? null : at;
  };
  return (
    <>
      <h4>The agents</h4>
      <div className="muted small">
        Each agent wrote its own test and fix. A fix is agreed when it passes the others' tests too.
        {run.readings && run.readings.length > 1 && " They split into readings of the report, shown as letters."}
      </div>
      <table className="grid" style={{ marginTop: 6 }}>
        <thead><tr>
          <th>Agent</th><th>Outcome</th>
          {repros.map((r) => <th key={r} title={`Passes agent ${r}'s test?`}>test {r}</th>)}
          <th>Lines</th><th>Its reading</th>
        </tr></thead>
        <tbody>
          {run.candidates.map((c) => <CandidateRow key={c.index} c={c} repros={repros}
            picked={c.index === run.pick} reading={readingOf(c.index)} />)}
        </tbody>
      </table>
    </>
  );
}

function CandidateRow({ c, repros, picked, reading }: {
  c: IssueFixCandidate; repros: number[]; picked: boolean; reading: number | null;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className="rowlink" onClick={() => setOpen(!open)}>
        <td className="mono">{open ? "▾" : "▸"} {c.index} · {c.model}{picked && <span className="chip chip-green sm">picked</span>}</td>
        <td className="small">{c.ending ?? (c.reproduces ? "reproduced" : "no reproduction")}</td>
        {repros.map((r) => {
          const v = c.passes?.[String(r)];
          return <td key={r} className="mono">{v === undefined ? "—" : v ? "✓" : "✗"}</td>;
        })}
        <td className="mono small">{c.fix_lines ?? "—"}</td>
        <td className="mono">{reading === null ? "—" : String.fromCharCode(65 + reading)}</td>
      </tr>
      {open && (
        <tr><td colSpan={4 + repros.length} className="small">
          {c.summary && <div><b>{c.summary}</b></div>}
          {c.root_cause && <div className="muted">Root cause: {c.root_cause}</div>}
          {c.detail && <div className="muted">{c.detail}</div>}
          {c.tests && c.tests.length > 0 && <div className="muted">Tests: {c.tests.join(", ")}</div>}
          {c.changes && c.changes.length > 0 && (
            <ul style={{ margin: "4px 0 0 18px" }}>
              {c.changes.map((ch) => <li key={ch.path}><code>{ch.path}</code>: {ch.rationale}</li>)}
            </ul>
          )}
        </td></tr>
      )}
    </>
  );
}

function FixThread({ entries }: { entries: IssueFixThreadEntry[] }) {
  return (
    <>
      <h4>Thread</h4>
      <div className="issue-fix-thread">
        {entries.map((e, i) => (
          <div key={i} className="small" style={{ margin: "4px 0" }}>
            <span className="muted">{when(e.at)} · {e.by === "worker" ? "🤖 worker" : e.by}:</span> {e.text}
          </div>
        ))}
      </div>
    </>
  );
}
