// Single source of truth for what the app's shorthand means. Every coded
// term the UI shows — dispositions, cluster states, safety verdicts, lanes,
// columns, bulk actions — has one plain-language explanation here, surfaced to
// the user through <InfoTip>. The enum maps are typed Record<EnumType, …> so the
// compiler refuses to build if a new code is added without an explanation.

import type { Disposition, ClusterState, Safety } from "./api";

export interface GlossaryEntry {
  /** Human-readable label for the term (e.g. "Needs human"). */
  title: string;
  /** One short sentence for a non-technical reader: what it means. */
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
    meaning: "Passed every check. Ready to merge as-is.",
  },
  "request-changes": {
    title: "Request changes",
    meaning: "Worth keeping, but not ready yet. We'll ask the author to fix what's missing.",
  },
  "close-dup": {
    title: "Close — duplicate",
    meaning: "Another PR already makes the same change. We'll close this one and point to the other.",
  },
  "close-fixed": {
    title: "Close — already fixed",
    meaning: "This problem is already fixed in the main code. We'll close it with a link to the fix.",
  },
  "close-stale": {
    title: "Close — stale",
    meaning: "Old and inactive. We'll close it; the author can reopen it later.",
  },
  "close-oversized": {
    title: "Close — too big (break up)",
    meaning: "Does too many things at once. We'll close it and ask the author to split it into smaller PRs.",
  },
  "needs-human": {
    title: "Needs human",
    meaning: "We couldn't decide this one automatically. A person needs to make the call.",
    triggers: "Usually because of a security concern, a failed test run, or a judgment call.",
  },
};

// Derived board state for a cluster (computed on read). Exhaustive over ClusterState.
const CLUSTER_STATE_GLOSSARY: Record<ClusterState, GlossaryEntry> = {
  "needs-analysis": {
    title: "Needs analysis",
    meaning: "Grouped, but not reviewed yet — or a PR changed since it was reviewed.",
  },
  "awaiting-authors": {
    title: "Awaiting authors",
    meaning: "We asked the authors for changes and are waiting on them.",
  },
  "needs-first-party-work": {
    title: "Needs first-party work",
    meaning: "No outside PR fully fixes this. Our team needs to write the fix.",
  },
  "blocked-on-decision": {
    title: "Blocked on decision",
    meaning: "Waiting on a product or judgment call before we can move on.",
  },
  "security-pending": {
    title: "Security pending",
    meaning: "A PR here is picked to merge but hasn't passed every check yet, such as the security review or test run.",
  },
  "ready": {
    title: "Ready",
    meaning: "Reviewed and ready for you to act on.",
  },
  "done": {
    title: "Done",
    meaning: "Every PR in this group has been handled.",
  },
};

type SafetyKey = "GREEN" | "YELLOW" | "RED" | "not-run";
// Security-review verdict. Exhaustive over the non-null Safety values + not-run.
const SAFETY_GLOSSARY: Record<SafetyKey, GlossaryEntry> = {
  "GREEN": { title: "Security: GREEN", meaning: "The security review found no problems." },
  "YELLOW": { title: "Security: YELLOW", meaning: "Small security concerns worth a look." },
  "RED": { title: "Security: RED", meaning: "A serious security problem. This PR can't merge until it's fixed." },
  "not-run": { title: "Security: not reviewed", meaning: "No security review yet." },
};

// Everything else, keyed by a stable dotted string. Looked up via term().
export const TERMS: Record<string, GlossaryEntry> = {
  // table columns
  "col.pr": { title: "PR", meaning: "The pull request number on GitHub." },
  "col.loc": { title: "Size", meaning: "How many lines and files the PR changes, not counting auto-generated files. Hover for a breakdown." },
  "col.files": { title: "Files", meaning: "How many files the PR changes." },
  "col.conflicts": { title: "Merge conflicts", meaning: "⚠ means the PR clashes with newer code and the author needs to update it." },
  "col.threat": { title: "Threat", meaning: "Our automatic scan for harmful code: malicious, suspicious, clear, or not scanned yet." },
  "col.safety": { title: "Safety", meaning: "The security review result: GREEN, YELLOW, RED, or — if not reviewed." },
  "col.cluster": { title: "Cluster", meaning: "The group of PRs that fix the same problem. Click to open it." },
  "col.updated": { title: "Updated", meaning: "When the PR last changed. Icons show what the author did since we acted: ↩ reopened · ⬆ pushed changes · 💬 replied." },
  "col.disposition": { title: "Disposition", meaning: "What we plan to do with this PR. Hover a value to see why." },
  "col.author": { title: "Author", meaning: "Who opened the PR." },
  "col.review": { title: "Review", meaning: "What the automated code reviewers think. Each one has to approve before merging." },
  "col.scans": { title: "Scans", meaning: "What the automated security scanners found. Any open finding blocks merging." },
  "col.checks": { title: "CI checks", meaning: "How many of the required automated tests pass. Hover for details." },
  "col.merge": { title: "Merge-ready", meaning: "Whether you can merge this now. Hover to see what's in the way." },
  "col.tier": { title: "Risk tier", meaning: "How sensitive the changed files are, from T0 (most critical) to T3 (low risk, like docs or UI)." },
  "col.age": { title: "Age", meaning: "Days since the PR last changed." },
  "col.author_rate": { title: "Author merge-rate", meaning: "How often this author's PRs get merged." },
  "col.summary": { title: "Agent summary", meaning: "A one-line description of what this PR changes." },
  "col.issues": { title: "Linked Issues", meaning: "Issues this PR may fix." },
  "ui.columns": { title: "Columns", meaning: "Show or hide columns. Your choices are saved in this browser." },

  // filter-row controls (the facet, not a single value)
  "ui.lanes": { title: "Lanes", meaning: "Ready-made filters for common lists. Click one to apply its filters; edit them as you like." },
  "ui.filters": { title: "Filters", meaning: "Narrow the list. Combine as many as you like; Clear all resets them." },
  "ui.bulk": { title: "Bulk actions", meaning: "Do one action on every selected PR. Each PR is still checked on its own, and everything is logged." },
  "filter.drift": { title: "Drift", meaning: "Whether the PR still fits the latest code: applies cleanly, already fixed, or has conflicts." },
  "filter.loc": { title: "Lines of code", meaning: "Filter by how many lines a PR changes. 'Effective' skips auto-generated files." },
  "filter.artifact_dominated": { title: "Mostly generated", meaning: "PRs that look big but are mostly auto-generated files, not real code." },
  "filter.files": { title: "Files", meaning: "Filter by how many files a PR changes." },
  "filter.paths": { title: "Path contains", meaning: "Show PRs that change a file whose path contains this text, e.g. “billing”." },
  "deep-search": { title: "Deep Search", meaning: "An AI reads each PR in the list and checks it against your question. Slower and costs more, so filter first.", note: "It reads each PR's summary, not the full code." },

  // lane filter templates
  "lane.easy": { title: "Easy Lane", meaning: "Small, low-risk PRs that passed every check — the quickest approvals." },
  "lane.stale": { title: "Stale (lane)", meaning: "PRs with open feedback where the author hasn't responded in over 30 days.", note: "Different from a stale analysis, which means the PR changed since we reviewed it." },
  "lane.merge-ready": { title: "Ready to merge (lane)", meaning: "PRs that passed every check. Same as the Home tab's “Ready to merge” card." },
  "lane.needs-human": { title: "Your call (lane)", meaning: "PRs we couldn't decide automatically. Same as the Home tab's “Your call” card." },

  // drift vs the current default branch (canonical states: applicable / already-fixed / conflicts)
  "drift.applicable": { title: "Drift: applicable", meaning: "Still fits the latest code cleanly." },
  "drift.already-fixed": { title: "Drift: already-fixed", meaning: "This is already fixed in the main code, so the PR is probably not needed." },
  "drift.conflicts": { title: "Drift: conflicts", meaning: "Clashes with newer code. The author needs to update it." },

  // threat scan
  "threat.malicious": { title: "Threat: malicious", meaning: "Our scan found what looks like an attack. This PR can never be merged." },
  "threat.suspicious": { title: "Threat: suspicious", meaning: "Our scan found something worth a closer look." },
  "threat.clear": { title: "Threat: clear", meaning: "Our scan found nothing harmful in the latest version." },
  "threat.unscanned": { title: "Threat: unscanned", meaning: "The latest version hasn't been scanned yet. This usually happens within minutes." },

  // cluster outcomes (stored on the cluster, distinct from the derived state above)
  "outcome.merge-ready": { title: "Outcome: merge-ready", meaning: "At least one PR in this group is ready to merge." },
  "outcome.awaiting-authors": { title: "Outcome: awaiting-authors", meaning: "Waiting on authors to make the changes we asked for." },
  "outcome.needs-first-party-work": { title: "Outcome: needs-first-party-work", meaning: "Our team needs to write the fix; no outside PR is good enough." },
  "outcome.close-out": { title: "Outcome: close-out", meaning: "Every PR in this group should be closed." },
  "outcome.blocked-on-decision": { title: "Outcome: blocked-on-decision", meaning: "Waiting on a product or judgment call." },

  // bulk action-bar options
  "bulk.CLOSE_DUP": { title: "Close — dup of", meaning: "Close each selected PR as a copy of another PR (you'll enter its number)." },
  "bulk.CLOSE_FIXED": { title: "Close — already-fixed", meaning: "Close each selected PR because the problem is already fixed." },
  "bulk.CLOSE_STALE": { title: "Close — stale", meaning: "Close each selected PR as old and inactive." },
  "bulk.CLOSE": { title: "Triage close", meaning: "Close each selected PR with a general note." },
  "bulk.REQUEST_CHANGES": { title: "Request changes", meaning: "Ask the author of each selected PR to make changes." },
  "bulk.COMMENT": { title: "Comment", meaning: "Post the same comment on every selected PR." },
  "bulk.REVIEW_RETRIGGER": { title: "Re-trigger a review", meaning: "Ask the automated reviewer to look at each selected PR again." },
  "bulk.QUEUE_VERIFY": { title: "Queue for verification", meaning: "Run each selected PR's tests in a safe, isolated environment. Nothing is posted to GitHub." },
  "bulk.RUN_SECURITY": { title: "Run security reviews", meaning: "Start an in-depth security review of each selected PR. Runs in the background." },
  "bulk.MERGE": { title: "Merge", meaning: "Merge each selected PR. A PR that fails its checks is skipped." },

  // freshness (the OTHER meaning of "stale")
  "freshness.stale": { title: "Stale analysis", meaning: "The PR changed since we reviewed it, so our results may be out of date. Re-run to refresh." },
  "freshness.current": { title: "Current", meaning: "Our results match the latest version of the PR." },
  "freshness.uningested": { title: "New commits upstream", meaning: "The author pushed changes we haven't picked up yet. What you see here is about the older version.", note: "It will update on its own shortly." },
  "freshness.provenance": { title: "Where these facts come from", meaning: "When each result was produced, and whether it still matches the latest version of the PR.", note: "Worth a look before you act." },

  // author responses since we acted
  "resp.reopened": { title: "Reopened", meaning: "The author reopened this PR after we closed it." },
  "resp.new_commits": { title: "New commits", meaning: "The author pushed changes since we acted." },
  "resp.replied": { title: "Replied", meaning: "The author replied since we acted." },

  // misc shorthand seen across tabs
  "trusted": { title: "Trusted author", meaning: "A contributor we trust. When two PRs are about equally good, theirs is preferred." },
  "deferred": { title: "Deferred", meaning: "An automatic library update. We skip these; they're handled on GitHub." },
  "human-merge": { title: "Human merge", meaning: "Changes protected files, so {bot} can't merge it. A code owner has to." },
  "greptile": { title: "Greptile", meaning: "An AI code reviewer that scores PRs 0–5. A PR needs 5/5 to merge." },
  "review": { title: "Automated review", meaning: "What the automated code reviewers and security scanners think of the latest version." },
  "ci": { title: "CI", meaning: "The PR's automated build and tests on GitHub." },
  "dry-run": { title: "Dry run", meaning: "A practice run — logged here but not sent to GitHub." },
  "security-review": { title: "Security review", meaning: "An in-depth AI security review of a PR picked to merge. A RED result sends it to a person." },
  "reversible": { title: "Reversible", meaning: "This can be undone." },
  "permanent": { title: "Permanent", meaning: "This can't be undone. Check carefully." },
};

export const dispositionEntry = (d: Disposition | null | undefined): GlossaryEntry | null =>
  d ? DISPOSITION_GLOSSARY[d] ?? null : null;

export const clusterStateEntry = (s: ClusterState | null | undefined): GlossaryEntry | null =>
  s ? CLUSTER_STATE_GLOSSARY[s] ?? null : null;

export const safetyEntry = (v: Safety | "not-run" | null | undefined): GlossaryEntry | null =>
  v ? SAFETY_GLOSSARY[v as SafetyKey] ?? null : null;

export const term = (key: string): GlossaryEntry | null => TERMS[key] ?? null;
