// Single source of truth for what the app's shorthand means. Every coded
// term the UI shows — dispositions, cluster states, safety verdicts, lanes,
// columns, bulk actions — has one plain-language explanation here, surfaced to
// the user through <InfoTip>. The enum maps are typed Record<EnumType, …> so the
// compiler refuses to build if a new code is added without an explanation.

import type { Disposition, ClusterState, Safety } from "./api";

export interface GlossaryEntry {
  /** Human-readable label for the term (e.g. "Needs human"). */
  title: string;
  /** One or two plain-language sentences: what it means. */
  meaning: string;
  /** Optional: what lands something in this state (shown when there's no
   *  instance-specific reason to show instead). */
  triggers?: string;
  /** Optional: what the operator can do about it, or a caveat. */
  note?: string;
}

// Per-PR decision the pipeline assigns. Exhaustive over Disposition.
const DISPOSITION_GLOSSARY: Record<Disposition, GlossaryEntry> = {
  "merge": {
    title: "Merge",
    meaning: "Clears every quality gate — the configured review bar, CI passing, mergeable, security clean, and dynamic verification confirming the fix. We'll merge it upstream as-is.",
  },
  "request-changes": {
    title: "Request changes",
    meaning: "Worth landing, but something is below the merge bar. We'll ask the author to close the gaps, then reconsider.",
  },
  "close-dup": {
    title: "Close — duplicate",
    meaning: "A duplicate of another PR that makes the same change. We'll close it and point the author at the canonical one.",
  },
  "close-fixed": {
    title: "Close — already fixed",
    meaning: "An equivalent fix already landed on the default branch, so this PR is redundant. We'll close it with a link to where it was fixed.",
  },
  "close-stale": {
    title: "Close — stale",
    meaning: "Old and inactive with no path forward. We'll close it to keep the queue honest — reopenable if the author comes back.",
  },
  "close-oversized": {
    title: "Close — too big (break up)",
    meaning: "Bundles too many distinct changes into one PR. We'll close it with a note asking the author to split it into smaller, single-purpose PRs — and, when its cluster is already merging some PRs, point at those so they don't resubmit that work.",
  },
  "needs-human": {
    title: "Needs human",
    meaning: "The pipeline reviewed this PR but deliberately did not auto-decide. A person has to make the call before anything happens upstream.",
    triggers: "Lands here when: a RED security review · a VERIFY escalation (blind verdict vs red→green disagreement) or deps-touched refusal · a threat-scan flag · or a product/judgment call the agent won't make.",
  },
};

// Derived board state for a cluster (computed on read). Exhaustive over ClusterState.
const CLUSTER_STATE_GLOSSARY: Record<ClusterState, GlossaryEntry> = {
  "needs-analysis": {
    title: "Needs analysis",
    meaning: "Clustered but not yet analyzed — or a PR's head moved and its analysis went stale. Waiting on the ANALYZE pass.",
  },
  "awaiting-authors": {
    title: "Awaiting authors",
    meaning: "The fix needs author changes before we can act. We've asked; now we wait for them to push.",
  },
  "needs-first-party-work": {
    title: "Needs first-party work",
    meaning: "No contributor PR fully solves this. The team needs to write the fix in-house.",
  },
  "blocked-on-decision": {
    title: "Blocked on decision",
    meaning: "Stuck on a product or judgment call that has to be made before triage can proceed.",
  },
  "security-pending": {
    title: "Security pending",
    meaning: "A merge-routed PR here doesn't pass the merge gate yet — its security review is missing, stale (head moved or aged out), or non-GREEN; or dynamic verification hasn't confirmed the fix; or the PR is no longer clean (CI, conflicts, Greptile).",
  },
  "ready": {
    title: "Ready",
    meaning: "Triaged and ready for a human to action in Prospector.",
  },
  "done": {
    title: "Done",
    meaning: "Every PR in this cluster has been actioned (merged, closed, or resolved).",
  },
};

type SafetyKey = "GREEN" | "YELLOW" | "RED" | "not-run";
// Security-review verdict. Exhaustive over the non-null Safety values + not-run.
const SAFETY_GLOSSARY: Record<SafetyKey, GlossaryEntry> = {
  "GREEN": { title: "Security: GREEN", meaning: "The adversarial security review found no issues." },
  "YELLOW": { title: "Security: YELLOW", meaning: "Minor security concerns worth a look — not blocking on their own." },
  "RED": { title: "Security: RED", meaning: "A serious security issue. The PR can't merge until it's resolved, and it's flipped to needs-human." },
  "not-run": { title: "Security: not reviewed", meaning: "No security review yet. We only run it on PRs routed to merge." },
};

// Everything else, keyed by a stable dotted string. Looked up via term().
export const TERMS: Record<string, GlossaryEntry> = {
  // table columns
  "col.pr": { title: "PR", meaning: "The pull-request number on the upstream repository." },
  "col.loc": { title: "LOC", meaning: "Lines of code the PR changes. Shows effective LOC — the lines a human wrote — once the diff is classified, stripping generated noise (migration snapshots, locale bundles, lockfiles, vendored/built files). 'effective / raw' means most of the diff is generated. Falls back to the raw additions+deletions until the diff is analyzed. Hover for the per-category breakdown." },
  "col.files": { title: "Files", meaning: "How many files the PR touches." },
  "col.safety": { title: "Safety", meaning: "The security-review verdict — GREEN, YELLOW, RED, or — when the PR hasn't been reviewed." },
  "col.cluster": { title: "Cluster", meaning: "The dedup group this PR belongs to — PRs fixing the same root issue. Click to open it." },
  "col.updated": { title: "Updated", meaning: "When the PR last changed upstream. Click to sort by recency. Chips show how the author responded since we acted: ↩ reopened · ⬆ new commits · 💬 replied. Click ✓ seen to acknowledge a response — it stops showing until a newer one arrives." },
  "col.disposition": { title: "Disposition", meaning: "What we've decided to do with this PR. Hover any value for the specific reason." },
  "col.author": { title: "Author", meaning: "The GitHub handle that opened the PR." },
  "col.greptile": { title: "Greptile", meaning: "Greptile's AI code-review confidence, 0–5. We require 5/5 to merge." },
  "col.checks": { title: "CI checks", meaning: "Required status checks that pass, out of the total. Hover for the per-check breakdown." },
  "col.merge": { title: "Merge-ready", meaning: "Whether a human can merge this now — gate-clean, security GREEN or never-run, no CODEOWNERS block. Hover for the blocking reason." },
  "col.tier": { title: "Risk tier", meaning: "Path-based blast radius of the files the PR touches: T0 = orchestration/auth core & supply chain (workflows, package manifests, lockfile), T1 = governed routes/services & db schema/migrations, T2 = shared contracts & other server code, T3 = leaf (ui, docs, tests). The most severe touched path wins; — means the diff isn't cached yet. An ordering signal for review rigor — it never changes the merge bar." },
  "col.age": { title: "Age", meaning: "Days since the PR last changed upstream." },
  "col.author_rate": { title: "Author merge-rate", meaning: "How often this author's PRs get merged upstream. Hover for merged/total and open count." },
  "col.summary": { title: "Agent summary", meaning: "The pipeline's one-line, diff-grounded description of what this PR changes." },
  "col.issues": { title: "Linked Issues", meaning: "Issues this PR may fix, backed by an explicit fix reference or issue-to-PR reference." },
  "ui.columns": { title: "Columns", meaning: "Show or hide table columns. Your choices are remembered in this browser." },

  // filter-row controls (the facet, not a single value)
  "ui.lanes": { title: "Lanes", meaning: "Shorthand for the common triage queues: clicking a lane drops its filter set into the query, where every filter shows as its own chip you can edit or clear. The chip stays lit while the query still matches the lane exactly." },
  "ui.filters": { title: "Filters", meaning: "Narrow the list by safety, drift, disposition, author, cluster, and more. Combine freely; Clear all resets them." },
  "ui.bulk": { title: "Bulk actions", meaning: "Apply one triage action to every selected PR at once. Each write still passes its own per-PR gate and is logged to the activity feed." },
  "filter.drift": { title: "Drift", meaning: "Whether the PR still applies to the current default branch: applicable (merges clean), already-fixed (redundant), or conflicts (needs a rebase)." },
  "filter.loc": { title: "Lines of code", meaning: "Filter by how many lines a PR changes — added, removed, or both — greater- or less-than a value. 'Effective' counts only the lines a human wrote (source + tests), stripping generated artifacts like migration snapshots, locale bundles, lockfiles, and vendored/built files; 'all lines' counts the raw diffstat. Effective falls back to the aggregate until the diff is classified." },
  "filter.artifact_dominated": { title: "Mostly generated", meaning: "Show only PRs whose diff is large but almost all generated noise — migration snapshots, locale bundles, lockfiles, vendored/built files — rather than reviewable code. These are the candidates to ask to rebase or gitignore the artifacts." },
  "filter.files": { title: "Files", meaning: "Filter by how many files a PR touches, greater- or less-than a value — e.g. find sprawling PRs with more than 20 files changed." },
  "filter.paths": { title: "Path contains", meaning: "Filter to PRs that change a file whose path contains this text (case-insensitive substring), e.g. “billing” or “src/auth”. Derived from the cached diff." },
  "deep-search": { title: "Deep Search", meaning: "For open-ended queries no filter can express, an agent reads each PR in the current result set and judges it against your text. Matches are agent judgments — not deterministic — cached per query; hover a row's 🪄 why for the reason. Slower and costs tokens, so narrow with filters first.", note: "Judges on compact facts (title, description, changed files, summary, signals) — not the diff." },

  // lane filter templates
  "lane.easy": { title: "Easy Lane", meaning: "Merge-ready (every check green), and also tiny (under 20 effective lines), leaf-surface (tier 3), and a pipeline merge pick — the fastest possible approvals. Edit any of the dropped-in filters to widen it." },
  "lane.stale": { title: "Stale (lane)", meaning: "Feedback stands and the author has gone quiet: review score below the bar, scored against the latest commit, with no PR update in over 30 days. A triage shortlist, not a verdict.", note: "Different from a stale analysis, which means the PR changed since we analyzed it." },
  "lane.merge-ready": { title: "Merge-ready (lane)", meaning: "Every check green — review at the bar and current, CI passing, no conflicts, security GREEN, verified. The same query as the Home tab's “PRs ready to merge” card." },
  "lane.needs-human": { title: "Needs human (lane)", meaning: "PRs the pipeline couldn't auto-decide — they need your call. The same as the needs-human disposition (a RED security verdict also lands here)." },

  // drift vs the current default branch (canonical states: applicable / already-fixed / conflicts)
  "drift.applicable": { title: "Drift: applicable", meaning: "The branch still merges cleanly onto the current default branch — worth acting on." },
  "drift.already-fixed": { title: "Drift: already-fixed", meaning: "An equivalent fix already landed on the default branch, so this PR is likely redundant (a close candidate)." },
  "drift.conflicts": { title: "Drift: conflicts", meaning: "The branch no longer merges cleanly onto the default branch — the author needs to rebase." },

  // threat scan
  "threat.malicious": { title: "Threat: malicious", meaning: "The threat scan matched a supply-chain attack pattern. A sticky hard block — it can never merge.", note: "Fails closed: no staleness exemption." },
  "threat.suspicious": { title: "Threat: suspicious", meaning: "The threat scan saw something worth a human look, short of malicious." },
  "threat.clear": { title: "Threat: clear", meaning: "No attack signatures matched in the diff." },

  // cluster outcomes (stored on the cluster, distinct from the derived state above)
  "outcome.merge-ready": { title: "Outcome: merge-ready", meaning: "At least one PR in this cluster is clean enough to merge." },
  "outcome.awaiting-authors": { title: "Outcome: awaiting-authors", meaning: "Blocked on author changes we've requested." },
  "outcome.needs-first-party-work": { title: "Outcome: needs-first-party-work", meaning: "The team must write the fix; no contributor PR suffices." },
  "outcome.close-out": { title: "Outcome: close-out", meaning: "This whole cluster should be closed (duplicates / stale / already-fixed)." },
  "outcome.blocked-on-decision": { title: "Outcome: blocked-on-decision", meaning: "Needs a product or judgment call before triage can proceed." },

  // bulk action-bar options
  "bulk.CLOSE_DUP": { title: "Close — dup of", meaning: "Close each selected PR as a duplicate of a canonical PR (you'll enter its number)." },
  "bulk.CLOSE_FIXED": { title: "Close — already-fixed", meaning: "Close each selected PR because the fix already landed on the default branch." },
  "bulk.CLOSE_STALE": { title: "Close — stale", meaning: "Close each selected PR as stale / abandoned." },
  "bulk.CLOSE": { title: "Triage close", meaning: "Close each selected PR with a generic triage reason." },
  "bulk.REQUEST_CHANGES": { title: "Request changes", meaning: "Post a change-request review on each selected PR, asking the author to fix the gaps." },
  "bulk.COMMENT": { title: "Comment", meaning: "Post one shared comment to every selected PR — no state change." },
  "bulk.GREPTILE_RETRIGGER": { title: "Re-trigger Greptile", meaning: "Post the configured review trigger on each selected PR so Greptile reviews its current head again." },
  "bulk.QUEUE_VERIFY": { title: "Queue for verification", meaning: "Queue each selected PR for sandbox verification — the verify worker runs its tests red→green in an isolated container. A local queue write; nothing is posted upstream." },
  "bulk.RUN_SECURITY": { title: "Run security reviews", meaning: "Start a deep 3-lens adversarial security review for each selected PR. Reviews run as bounded background jobs and continue after leaving this page." },
  "bulk.MERGE": { title: "Merge", meaning: "Merge each selected PR. Gated individually — nothing merges that fails its own gate; no comment is posted." },

  // freshness (the OTHER meaning of "stale")
  "freshness.stale": { title: "Stale analysis", meaning: "The PR changed since we analyzed it, so its analysis and security may be out of date. Re-run to refresh." },
  "freshness.current": { title: "Current", meaning: "Our analysis matches the PR's current head commit." },

  // author responses since we acted
  "resp.reopened": { title: "Reopened", meaning: "The author reopened this PR after we closed it." },
  "resp.new_commits": { title: "New commits", meaning: "The author pushed new commits since we acted — our analysis may be stale." },
  "resp.replied": { title: "Replied", meaning: "The author left a reply since we acted." },

  // misc shorthand seen across tabs
  "trusted": { title: "Trusted author", meaning: "A contributor the repository profile names as trusted — in ANALYZE their PR breaks an otherwise-close canonical tie (a tiebreaker, never an override of a clearly-better PR)." },
  "deferred": { title: "Deferred", meaning: "A dependency bump from a trusted automation author, touching only dependency manifests. We defer these out of triage — the meaningful risk is in the package, not the diff — and the author lands them upstream." },
  "human-merge": { title: "Human merge", meaning: "Touches CODEOWNERS-gated paths — {bot} can't auto-merge. A code owner must merge it." },
  "greptile": { title: "Greptile", meaning: "An AI code-review confidence score, 0–5. We require 5/5 to merge; anything less is request-changes." },
  "ci": { title: "CI", meaning: "The PR's continuous-integration checks (build / tests) on GitHub." },
  "dry-run": { title: "Dry run", meaning: "A simulated action — logged but never sent upstream. The fallback on any machine that can't mint a real {bot} token." },
  "security-review": { title: "Security review", meaning: "A 3-lens adversarial review with a refuting verifier, run only on PRs routed to merge. RED flips the PR to needs-human." },
  "reversible": { title: "Reversible", meaning: "This action can be undone — closes can be reopened, comments deleted." },
  "permanent": { title: "Permanent", meaning: "This action cannot be undone — a merge is forever. Review carefully." },
};

export const dispositionEntry = (d: Disposition | null | undefined): GlossaryEntry | null =>
  d ? DISPOSITION_GLOSSARY[d] ?? null : null;

export const clusterStateEntry = (s: ClusterState | null | undefined): GlossaryEntry | null =>
  s ? CLUSTER_STATE_GLOSSARY[s] ?? null : null;

export const safetyEntry = (v: Safety | "not-run" | null | undefined): GlossaryEntry | null =>
  v ? SAFETY_GLOSSARY[v as SafetyKey] ?? null : null;

export const term = (key: string): GlossaryEntry | null => TERMS[key] ?? null;
