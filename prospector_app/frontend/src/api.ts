// Typed client for the app backend.

import { markReachable, isProxyDown } from "./health";

export type Safety = "GREEN" | "YELLOW" | "RED" | null;
export type SafetyRollup = { green: number; yellow: number; red: number; unknown?: number };

export type Disposition =
  | "merge" | "request-changes" | "close-dup" | "close-fixed" | "close-stale"
  | "close-oversized" | "needs-human";

export type SafetyFilter = "GREEN" | "YELLOW" | "RED" | "not-run";
export type DriftFilter = "applicable" | "already-fixed" | "conflicts";
export type CiFilter = "passing" | "failing" | "unknown";
export type ResponsesFilter = "any" | "reopened" | "new_commits" | "replied" | "resubmitted";

/** One named check's pass/fail/never-ran bucket, matched against a PR's checks
 *  rollup by stable key (CheckItem.key) rather than its display name — see
 *  CheckClause below. */
export type CheckStatus = "pass" | "fail" | "never_ran";

/** Filter PRs on one named CI/vetting check (e.g. "security", "verify"):
 *  match if its rolled-up status falls in `status` (an array ORs; a bare
 *  value is shorthand for a one-element array). Multiple clauses in
 *  FilterSpec.checks AND together, so "no merge conflicts passed AND security
 *  passed AND verify never ran" is three clauses. */
export interface CheckClause {
  key: string;
  status: CheckStatus | CheckStatus[];
}

export type NumCmp = { op: "<" | "<=" | "==" | ">=" | ">"; value: number };

/** Filter PRs by change size. `metric` picks added / removed / both (add+del);
 *  `scope` "effective" counts only human-written lines (source+test, generated
 *  artifacts stripped), "all" counts the raw diffstat; `op` is the comparison.
 *  value undefined = the control is shown but not yet filtering. */
export interface LocFilter {
  metric: "additions" | "deletions" | "both";
  scope: "effective" | "all";
  op: "<" | ">";
  value?: number;
}

/** Filter PRs by how many files they touch. `op` is the comparison;
 *  value undefined = the control is shown but not yet filtering. */
export interface FilesFilter {
  op: "<" | ">";
  value?: number;
}

export interface FilterSpec {
  q?: string;
  author?: string;
  cluster?: number;
  cluster_none?: boolean;
  safety?: SafetyFilter | SafetyFilter[];
  drift?: DriftFilter | DriftFilter[];
  disposition?: Disposition | Disposition[];
  ci?: CiFilter | CiFilter[];
  checks?: CheckClause[];   // per-check passed/failed/never-ran, one clause per check key
  threat?: "malicious" | "suspicious" | "clear";
  conflicts?: boolean;
  has_tests?: boolean;
  draft?: boolean;
  state?: "closed" | "all";       // PR Explorer defaults to open PRs only; widen to include closed/merged
  trusted_author?: boolean;
  clean?: boolean;
  greptile?: NumCmp;
  greptile_stale?: boolean;
  greptile_severity?: "defects" | "nits" | "clean";
  // {reviewer id: bar status} over the row's reviewer digests
  reviewer_status?: Record<string, BarStatus | BarStatus[]>;
  age_days?: NumCmp;
  max_files?: number;
  max_total_lines?: number;
  risk_tier?: number | number[];  // path-based blast-radius tier (0 = core … 3 = leaf)
  merge_ok?: boolean;        // the row's merge gate (gates.merge_eligibility) passes
  has_summary?: boolean;     // an agent summary exists for the PR
  has_issues?: boolean;      // the PR has linked issues
  responses?: ResponsesFilter | ResponsesFilter[];
  loc?: LocFilter;
  files?: FilesFilter;       // how many files a PR touches, greater-/less-than
  pain?: NumCmp;
  author_rate?: NumCmp;          // filter by author's historical merge-rate (0–1 decimal)
  artifact_dominated?: boolean;  // diff is mostly generated noise (snapshots/locales/lockfiles)
  paths?: string;            // substring over a PR's changed file paths
  numbers?: number[];        // restrict to a PR-number set (Deep Search overlay, or the PR-column filter)
}

/** One PR's verdict from Deep Search — the agent's match decision + a short why. */
interface DeepMatch { pr: number; reason: string }
export interface DeepProgress { done: number; total: number }
export interface DeepResult {
  matches: DeepMatch[];
  total: number;       // candidates actually judged (after the cap)
  capped: boolean;     // the candidate set hit MAX_CANDIDATES and was trimmed
  judged: number;      // freshly agent-judged this run
  from_cache: number;  // reused from a prior run
}

/** Who marked a PR's response signal seen, and when. Shared across operators. */
export interface PRResponseAck {
  at: string;
  by: string;
}

/** How the community responded to our triage since we acted (replied / reopened /
 *  pushed a fix). Null when there's no detected response. */
export interface PRResponses {
  reopened: boolean;
  new_commits: boolean;
  replied: boolean;
  resubmitted: boolean;
  resubmitted_pr: number | null;
  last_response_at: string | null;
  snippet: string | null;
  by: string | null;
  ack: PRResponseAck | null;
}

export interface QueryResult {
  items: PRRow[];
  total: number;
  offset: number;
  limit: number;
  match_ids: number[];
}

export interface BulkResult {
  pr: number;
  action: string;
  status: string;
  detail: string;
  job_id?: number;
}

export type ClusterState =
  | "needs-analysis" | "awaiting-authors" | "needs-first-party-work"
  | "blocked-on-decision" | "security-pending" | "ready" | "done";

export interface PainBreakdown {
  issue_pain: number;
  linked_issues: number;
  pr_comments: number;
  pr_reactions: number;
}

export interface ClusterSummary {
  cluster_id: number;
  root_problem: string;
  pr_count: number;
  state: ClusterState;
  outcome: string | null;
  dispositions: Record<string, number>;
  security: SafetyRollup;
  security_prs?: { pr: number; verdict: string; fresh?: boolean | null; title: string | null; gating?: boolean; findings: { severity: string; title: string }[] }[];
  analyzed_at: string | null;
  notes?: string | null;
  pain_score?: number | null;
  pain_breakdown?: PainBreakdown | null;
}

interface ProposedAction {
  action: Disposition | null;
  canonical?: number | null;
  upstream_pr?: number | null;
  upstream_commit?: string | null;
  upstream_date?: string | null;
  asks?: string[] | null;
  rationale?: string | null;
  fresh?: boolean;
}

export type ReviewerKind = "review" | "scanner";
export type BarStatus = "pass" | "fail" | "stale" | "pending" | "na";

/** One automated reviewer or security scanner the backend knows, from GET
 *  /api/capabilities — `active` means it gates this repository. Drives the
 *  Review/Scans columns, their filters, the PR page's per-reviewer blocks and
 *  the re-trigger controls. */
export interface ReviewerCap {
  id: string;
  label: string;
  kind: ReviewerKind;
  active: boolean;
  retrigger: boolean;
  score_max: number | null;
  threshold: number | null;
  bar_label: string;
}

/** A reviewer's compact verdict on one PR row (service.pr_row `reviews[id]`). */
export interface ReviewDigest {
  id: string;
  label: string;
  kind: ReviewerKind;
  status: BarStatus;
  reason: string | null;
  score: number | null;
  score_max: number | null;
  reviewed_sha: string | null;
  stale: boolean | null;
  open: Record<string, number>;
  observed_at: string | null;
  checks: { name: string | null; conclusion: string | null; status: string | null; title: string | null }[];
  extra: Record<string, unknown>;
  summary_line: string;
}

export interface ReviewFinding {
  path: string | null;
  line: number | null;
  severity: string | null;
  title: string | null;
  body: string;
  resolved: boolean;
  outdated: boolean;
  commit: string | null;
  url: string | null;
}

/** A reviewer's stored entry on one PR (the `reviews` section). */
export interface ReviewEntry {
  kind: ReviewerKind;
  reviewed_sha: string | null;
  observed_at: string | null;
  score: number | null;
  findings: ReviewFinding[];
  summary: string | null;
  checks: ReviewDigest["checks"];
  extra: Record<string, unknown>;
}

export type ReviewsDetail = Record<string, { entry: ReviewEntry | null; digest: ReviewDigest }>;

interface SignalSummary {
  greptile: number | null;
  greptile_stale: boolean | null;
  greptile_severity: "defects" | "nits" | "clean" | null;
  ci: string | null;
  conflicts: boolean | null;
  additions: number | null;
  deletions: number | null;
  changed_files: number | null;
}

export interface IssueLink { issue: number; pain: number | null; how: string }

export interface PRRow {
  number: number;
  title: string | null;
  author: string | null;
  head_sha?: string;
  url?: string;
  created_at?: string;
  updated_at?: string;
  draft?: boolean;
  clusters: number[];
  disposition: Disposition | null;
  // deferred out of triage (dependency bump handled upstream) — see suggest.py
  out_of_scope?: boolean;
  safety: Safety;
  safety_fresh?: boolean;
  safety_findings: number;
  drift_state: string | null;
  github_state?: string | null;
  clean?: boolean;
  clean_reasons?: string[];
  stale_sections?: string[];
  live_head_sha?: string | null;
  fact_freshness?: FactFreshness[];
  issues?: IssueLink[];
  trusted_author?: boolean;
  proposed_action?: ProposedAction;
  suggestion?: Suggestion;
  signals?: SignalSummary | null;
  // every reviewer with an entry on this PR or active on the repository, by id
  reviews?: Record<string, ReviewDigest> | null;
  summary?: { one_liner?: string | null; primary_change?: string | null } | null;
  size_split?: SizeSplit | null;
  loc_breakdown?: LocBreakdown | null;
  human_merge?: HumanMerge | null;
  safety_titles?: { severity: string; title: string; location: string }[];
  author_stats?: AuthorStats | null;
  checks?: ChecksRollup;
  merge_gate?: { ok: boolean; reason: string; overridable?: boolean; override_kind?: "security" | "verify" | null };
  age_days?: number | null;
  responses?: PRResponses | null;
  pain_score?: number | null;
  pain_breakdown?: PainBreakdown | null;
  // path-based blast-radius tier (0 = core/supply chain … 3 = leaf);
  // null until the diff is cached ("unknown", rendered as absence)
  risk_tier?: number | null;
}

interface SizeBucket { additions: number; deletions: number; files: number }
interface SizeSplit { test: SizeBucket; non_test: SizeBucket; removes_tests: boolean }

/** Effective-LOC breakdown of a diff: `effective` = source+test lines (what a
 *  human wrote), `raw` = all lines, `artifact` = raw − effective (generated /
 *  vendored / lockfile / locale / migration noise). `by_category` holds the
 *  per-category buckets that are non-empty; `dominant_artifact` is the biggest
 *  artifact category, for a one-glance "why it's huge". */
export interface LocBreakdown {
  effective: number;
  raw: number;
  artifact: number;
  dominant_artifact: string | null;
  by_category: Record<string, SizeBucket>;
}
export interface HumanMerge { required: boolean; paths: string[]; owners: string[] }
// One sha-bound fact's provenance: when it was computed, the head it describes,
// and whether it still holds. `why` is the short reason it doesn't.
export interface FactFreshness {
  section: string;
  checked_at?: string | null;
  against_head_sha?: string | null;
  current: boolean;
  why?: string | null;
}

// The executor's refusal payload when a write would quote facts the author has
// moved past — mirrors executor._stale_gate.
export interface StaleBlock {
  was?: string | null;
  now?: string | null;
  sections: { section: string; checked_at?: string | null }[];
}

export interface Divergence { kind: string; was?: string; now?: string; message: string }
export interface FreshnessItem { number: number; reachable: boolean; diverged: Divergence[]; state?: string; head?: string }

// Mirrors backend models.CloseAction — the POST /api/execute/pr/{n} body.
export interface CloseActionBody {
  pr?: number;
  action: string;
  // The body the backend accepts; this frontend resolves any override into
  // `action` before sending, so it never populates `override_action` itself.
  override_action?: string | null;
  canonical?: number;
  upstream_pr?: number | null;
  upstream_commit?: string | null;
  upstream_date?: string | null;
  merge_prs?: number[] | null;
  dup_reason?: string | null;
  comment?: string;
  reason?: string;
  tags?: string[];
  // Post over a "head moved since we analyzed this" refusal, after the operator
  // confirms the drift the app showed them.
  override_stale?: boolean;
}

// Mirrors backend models.SuggestAccept (CloseAccept | ReviewAccept | MergeAccept),
// discriminated on `kind`. Keep field names in sync with prospector_app/backend/models.py.
interface SuggestAccept {
  kind: "close" | "review" | "merge";
  action?: string;
  canonical?: number;
  upstream_pr?: number | null;
  upstream_commit?: string | null;
  upstream_date?: string | null;
  event?: string;
  body?: string;
  method?: string;
  tags?: string[];
  merge_prs?: number[] | null;
}
export interface Suggestion {
  action: string;
  label: string;
  tone: "green" | "yellow" | "red" | "muted";
  rationale: string;
  comment?: string;
  bot_comment?: string | null;  // verbatim text the configured bot will post (null for merge/no-op)
  reversible?: boolean;         // false only for merge (permanent)
  needs_verify?: string | null; // reason to eyeball this before firing, or null
  blocker?: "security" | null;  // when action==="BLOCKED": security is what re-running would clear
  accept: SuggestAccept | null;
}

export interface CheckItem { key: string; name: string; status: "pass" | "warn" | "fail" | "na"; detail: string; at?: string | null }
export interface ChecksRollup { checks: CheckItem[]; passed: number; total: number }

export interface AuthorStats {
  handle: string;
  url: string;
  merge_rate?: number;
  merge_rate_shrunk?: number;
  total?: number;
  open?: number;
  merged?: number;
  closed_unmerged?: number;
  comments?: number;
  issues_filed?: number;
  issues_resolved?: number;
}

interface SafetyFinding {
  severity: string;
  lens: string;
  category: string;
  title: string;
  detail: string;
  location: string;
}

interface SafetyVerdict {
  pr: number;
  head_sha: string;
  reviewed_at: string;
  verdict: Safety;
  findings: SafetyFinding[];
  signals?: Record<string, unknown>;
  cluster?: number;
  override?: unknown;
}

export interface ClusterDetail {
  cluster_id: number;
  root_problem: string;
  outcome: string | null;
  state: ClusterState;
  rationale: string | null;
  rationale_summary: string | null;
  notes: string | null;
  analyzed_at: string | null;
  prs: PRRow[];
  buckets: Record<string, PRRow[]>;
}

interface SafetySummary {
  verdict: Safety | null;
  level: "safe" | "caution" | "risk" | "unknown";
  headline: string;
  detail: string;
}

// --- VERIFY (dynamic verification) — mirrors backend verify_view.py ---

/** Signal 1: the blind adequacy verdict, committed to the store BEFORE any
 *  sandbox run — so it cannot be rationalized backward from a green result. */
interface VerifyBlind {
  has_test?: boolean;
  faithful?: boolean | null;
  confidence?: string | null;
  claimed_symptom?: string | null;
  expected_red_signature?: string | null;
  expected_repro_signature?: string | null;
  requires_live_agent?: boolean;
  from_linked_issue?: boolean;
  test_cmd?: string | null;
  repro_command?: string | null;
  reasoning?: string | null;
}

/** Signal 2: host-observed exit codes (trusted facts) plus the captured output
 *  tails — the PR's own test output: untrusted, attacker-influenced text. */
interface VerifyRedGreen {
  apply_exit?: number | null;
  red_exit?: number | null;
  green_exit?: number | null;
  red_output_tail?: string | null;
  green_output_tail?: string | null;
  // the confirm re-run (rules out a flaky red→green); null when the first run
  // was not clean, so no confirm ran
  red_exit_confirm?: number | null;
  green_exit_confirm?: number | null;
}

/** Signals 3/5: the judge's reason-match rating over untrusted output.
 *  `applicable` is set on repro_reason_match only (false = no repro ran). */
export interface VerifyReasonMatch {
  matches?: boolean | null;
  confidence?: string | null;
  reasoning?: string | null;
  applicable?: boolean;
}

/** Signal 4: the agent-authored independent repro — corroborating evidence,
 *  never a gate. `output_tail` is untrusted test output. */
interface VerifyRepro {
  ran?: boolean;
  exit_code?: number | null;
  from_linked_issue?: boolean;
  output_tail?: string | null;
}

/** The agent-authored test lane (no-test PRs) — corroborating evidence only,
 *  never an auto-merge signal. Exit codes are host-observed sentinels. */
interface VerifyAuthoredTest {
  attempted?: boolean;
  can_author?: boolean;
  files?: { path: string; contents: string }[];
  test_cmd?: string | null;
  expected_red_signature?: string | null;
  confidence?: string;
  reasoning?: string;
  skipped_reason?: string | null;
  red_exit?: number | null;
  green_exit?: number | null;
  red_exit_confirm?: number | null;
  green_exit_confirm?: number | null;
  red_output_tail?: string;
  green_output_tail?: string;
}

export interface VerifySignals {
  blind_adequacy?: VerifyBlind;
  red_green?: VerifyRedGreen;
  red_reason_match?: VerifyReasonMatch;
  independent_repro?: VerifyRepro;
  repro_reason_match?: VerifyReasonMatch;
  authored_test?: VerifyAuthoredTest;
  lanes?: Record<string, {
    cmd?: string;
    exit?: number;
    ok?: boolean;
    duration_s?: number;
    error_excerpt?: string;
    skipped?: string;
  }>;
}

interface VerifyFinding { title?: string; detail?: string; confidence?: string }

export type VerifyLevel = "verified" | "attention" | "blocked" | "info" | "pending";

/** One check in the run's plain-English storyline, derived server-side from
 *  the same trusted inputs the gate reads — explanation only, never policy. */
export interface VerifyStep {
  key: string;
  label: string;
  result: "pass" | "fail" | "warn" | "skip" | "info";
  note: string;
  reasoning: string | null;
}

/** A PR's VERIFY record rendered for the detail view. `outcome` is verbatim
 *  from the store (gates.py is the one policy — the UI never recomputes or
 *  softens it); null = blind verdict committed, no conclusion yet. `cause`
 *  names the specific check that broke; `story` is the run's checks in order. */
export interface VerifyDetail {
  outcome: string | null;
  tier: number | null;
  checked_at: string | null;
  against_base_sha: string | null;
  against_head_sha: string | null;
  stale_reason: string | null;
  signals_incomplete: string | null;
  level: VerifyLevel;
  // the four-state operator label ("Verified" / "Not verified" / "Needs your
  // call" / "Couldn't run") — the glanceable lead; `outcome` is the raw detail
  state: string;
  headline: string;
  detail: string;
  // whose fault a non-green outcome is: "pr" (the PR), "system" (the harness —
  // not the PR's fault), "judgment" (a human must decide), or null (no fault)
  fault: VerifyFault;
  cause: string | null;
  story: VerifyStep[];
  signals: VerifySignals;
  findings: VerifyFinding[];
}

export type VerifyFault = "pr" | "system" | "judgment" | null;

/** A PR's sandbox-verification queue state (the verify_request section):
 *  queued from any app, run by the verification worker on the sandbox
 *  machine, parked as waiting-for-base while the runner has no usable pinned
 *  base (retried, bounded), finished as done / error / cancelled. */
export interface VerifyRequest {
  status: "queued" | "running" | "waiting-for-base" | "done" | "error" | "cancelled";
  source?: "operator" | "auto" | null;
  step?: string | null;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error_kind?: string | null;
  error?: string | null;
  log_tail?: string | null;
  checked_at?: string | null;
  fault?: VerifyFault;
}

/** Verification-runner liveness, from the verify_worker heartbeat registry.
 *  `configured` = this backend runs the worker; `online` = some machine's
 *  worker beat recently (the queue is shared, the runner may be elsewhere). */
export interface VerifyRunner {
  configured: boolean;
  online: boolean;
  host?: string | null;
  current_pr?: number | null;
  last_beat?: string | null;
  hosts: RunnerHost[];
}

/** One machine's worker heartbeat, from either lane's registry. */
export interface RunnerHost {
  host?: string | null;
  online: boolean;
  last_beat?: string | null;
  current_pr?: number | null;
  autohunt: boolean;
}

/** An autofix action the push bot may run on a contributor's PR head branch.
 *  `update` merges the base branch in, `rebase` replays the PR onto current
 *  base behind a pinned lease, `fix` has an agent author a change. */
export type FixAction = "update" | "rebase" | "fix" | "describe";

/** Every action a request can carry: the four queueable actions plus
 *  `resolve`, which the worker records when it escalates a conflicted rebase
 *  to an agent-authored merge resolution. */
export type FixRequestAction = FixAction | "resolve";

/** The autofix queue state for one PR: queued in any app, run by the fix
 *  worker on the machine holding the push key, parked as awaiting-review with
 *  the authored diff, approved by a human, then pushed. `refused` means a gate
 *  or the compile preflight blocked it and nothing reached upstream. */
export interface FixRequest {
  status: "queued" | "running" | "awaiting-review" | "approved" | "pushing"
        | "pushed" | "refused" | "failed" | "cancelled";
  action: FixRequestAction;
  source?: "operator" | "auto" | null;
  step?: string | null;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  refused_reason?: string | null;
  result?: FixResult | null;
  host?: string | null;
  base_sha?: string | null;
  /** The operator's own instruction for an agent-authored fix, and what
   *  authorizes one where the profile names no fixable gates. */
  guidance?: string | null;
  checked_at?: string | null;
  against_head_sha?: string | null;
}

/** What the worker authored, carried on the request so the review view can
 *  show the diff before anything is pushed. A conflicted rebase carries the
 *  paused worktree's conflict diff (merge_diff + conflict_paths) — on a
 *  refusal and on a parked `resolve` alike — so the diff panel can show the
 *  conflicted hunks. */
export interface FixResult {
  patch?: string | null;
  message?: string | null;
  output?: string | null;
  detail?: string | null;
  merge_diff?: string | null;
  conflict_paths?: string[] | null;
  /** Per-file rationale from the conflict-resolution agent (action `resolve`). */
  resolutions?: { path: string; rationale: string }[] | null;
  /** Per-file rationale from the authoring agent (action `fix`). */
  changes?: { path: string; rationale: string }[] | null;
  /** The rewritten PR description (action `describe`), and the author's text
   *  it replaces. */
  body?: string | null;
  previous_body?: string | null;
  /** The refuting reviewer's judgment of an authored fix. Present on a parked
   *  fix and on one it rejected — a rejection is the interesting half. */
  review_verdict?: { verdict: "safe" | "unsafe"; reason: string;
                     concerns: string[] } | null;
  compile_preflight?: { exit?: number | null; refused?: string | null;
                        error?: string | null; error_excerpt?: string | null } | null;
}

/** Autofix-runner liveness plus this backend's push configuration.
 *  `can_queue` is what disables the actions — it asks whether ANY worker is
 *  reachable against the shared store, not whether this backend holds the key,
 *  since queueing from a laptop is the point. `push_identity` is this
 *  backend's own credential, for diagnostics. */
export interface FixRunner {
  configured: boolean;
  online: boolean;
  can_queue: boolean;
  push_identity: boolean;
  push_login?: string | null;
  autopush: FixAction[];
  host?: string | null;
  current_pr?: number | null;
  last_beat?: string | null;
  hosts: RunnerHost[];
}

/** One row of the autofix queue: a request in flight, or one that ended within
 *  the last half hour and is held here so a run finishing between two polls is
 *  still readable. `finished_at` is what tells the two apart. `resolvable` is
 *  the claim a parked row makes: the action produced a change and the compile
 *  preflight did not reject it. `base_sha` is the base it was proven against —
 *  approving re-runs the action against whatever base is current then, so this
 *  dates the proof rather than gating it. */
export interface FixQueueEntry {
  pr: number;
  title?: string | null;
  status: FixRequest["status"];
  action: FixRequestAction;
  source?: "operator" | "auto" | null;
  step?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  host?: string | null;
  queued_at?: string | null;
  base_sha?: string | null;
  /** The operator's own instruction for an agent-authored fix. */
  guidance?: string | null;
  /** The one line worth reading about where this request stands: why it was
   *  refused, what it failed on, or the message a proven change carries. */
  detail?: string | null;
  /** The paths a conflicted rebase paused on — what an agent resolve would be
   *  asked to settle. Empty on every other ending. */
  conflict_paths: string[];
  resolvable: boolean;
}

export interface FixQueue {
  queue: FixQueueEntry[];
  runner: FixRunner;
  history: AutohuntRun[];
}

/** One security, verify, or fix run from the store's runs ledger, normalized
 *  for the panel that reports its lane. `trigger` is "autohunt" on hunter-fired
 *  runs, null on operator-fired or unstamped entries. `action` and `detail`
 *  belong to the fix lane and are null in the other two. */
export interface AutohuntRun {
  phase: "security" | "verify" | "fix";
  pr: number;
  title?: string | null;
  started?: string | null;
  finished?: string | null;
  trigger?: string | null;
  result?: string | null;
  action?: FixRequestAction | null;
  detail?: string | null;
}

/** One machine's pinned sandbox base. `age_hours` and `stale` are null/false
 *  when its timestamp does not parse — a malformed stamp is not evidence the
 *  lane is broken. `refresh_failures` counts consecutive failed daily
 *  refreshes, reset by any successful one. */
export interface VerifyBaseHost {
  host: string;
  base_sha?: string | null;
  tier?: number | null;
  pinned_at?: string | null;
  age_hours?: number | null;
  stale: boolean;
  refresh_ok?: boolean | null;
  refresh_error?: string | null;
  refresh_failures: number;
}

/** Every verification machine's pinned base. Each holds its own and tracks the
 *  default branch on its own daily cadence, so they are reported side by side;
 *  an empty list means no machine has prepared one. */
export interface VerifyBaseHealth {
  hosts: VerifyBaseHost[];
}

/** The idle hunter's live status: worker opt-in + liveness, pool sizes
 *  computed with the hunter's own gates, and its failure parking lots —
 *  security runs parked by the worker's failure memory, and verify requests
 *  that ended in error (awaiting an operator re-queue), auto-queued or
 *  operator-queued alike. */
interface AutohuntStatus {
  enabled: boolean;
  runner: VerifyRunner;
  base: VerifyBaseHealth;
  security_pool: number;
  verify_pool: number;
  security_failed: number[];
  verify_failed: { pr: number; error_kind?: string | null; source?: string | null }[];
}

/** Run counts + result breakdown for one lane (security or verify) within
 *  the summary's selected window. `pr_ids_by_result` carries the distinct PR
 *  numbers behind each result bucket, so a result chip can open exactly those
 *  PRs in the Explorer. */
export interface AutohuntResultCounts {
  total: number;
  by_result: Record<string, number>;
  pr_ids_by_result: Record<string, number[]>;
}

/** Windowed digest of the runs ledger: totals + per-result breakdown for each
 *  lane over the last `days` days (`days` is null for the all-time window). */
interface AutohuntSummary {
  days: number | null;
  security: AutohuntResultCounts;
  verify: AutohuntResultCounts;
}

export interface Autohunt { status: AutohuntStatus; summary: AutohuntSummary; history: AutohuntRun[]; }

/** One PR with a sandbox-verification request in flight: running, waiting on
 *  a base refresh, or queued. `source` is "auto" for the idle hunter, null/
 *  undefined for an operator-queued request. */
export interface VerifyQueueEntry {
  pr: number;
  title?: string | null;
  status: "queued" | "running" | "waiting-for-base";
  source?: "auto" | null;
  step?: string | null;
  queued_at?: string | null;
  started_at?: string | null;
  host?: string | null;
}

/** One in-flight run anywhere on the deployment, for the header status label.
 *  `worker_online` asks whether the claiming host's worker is still beating —
 *  a claimed run whose worker went quiet is stuck, not slow. */
export interface WorkActive {
  lane: "verify" | "fix";
  pr: number;
  title?: string | null;
  action?: string | null;
  step?: string | null;
  host?: string | null;
  started_at?: string | null;
  source?: string | null;
  worker_online: boolean;
}

/** One lane's worker heartbeat on one machine, for the header flyout. */
export interface WorkWorker {
  lane: "verify" | "fix";
  host?: string | null;
  online: boolean;
  last_beat?: string | null;
  current_pr?: number | null;
  autohunt: boolean;
}

/** What the system is doing right now, across every machine on this store —
 *  the header status label's feed. `jobs` is this backend's own Control-tab
 *  jobs; everything else is deployment-wide via the shared store. */
export interface WorkStatus {
  active: WorkActive[];
  queued: { verify: number; fix: number };
  awaiting_review: number;
  workers: WorkWorker[];
  jobs: { running: number; labels: string[] };
}

/** The sandbox-verification queue: PRs currently in flight, plus verify-only
 *  run history from the runs ledger over the selected window. */
export interface VerifyQueue { queue: VerifyQueueEntry[]; history: AutohuntRun[]; }

export interface PRDetail extends PRRow {
  body?: string | null;
  base?: string | null;
  security_detail?: SafetyVerdict | null;
  safety_summary?: SafetySummary;
  verify_detail?: VerifyDetail | null;
  verify_request?: VerifyRequest | null;
  fix_request?: FixRequest | null;
  analysis_detail?: unknown;
  size?: { additions: number | null; deletions: number | null; changed_files: number | null };
  reviews_detail?: ReviewsDetail | null;
  ci_checks?: { name: string; conclusion: string; status: string }[];
  summary?: { one_liner?: string | null; mechanism?: string | null } | null;
  merge_gate?: { ok: boolean; reason: string; overridable?: boolean; override_kind?: "security" | "verify" | null };
  // the changed paths that pinned risk_tier (the detail view's "why this tier")
  risk_tier_paths?: string[];
}

export interface DiffResult {
  diff: string;
  note: string | null;
  error: string | null;
  file_count: number;
  truncated: boolean;
  source: string;
}

async function get<T>(url: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, { cache: "no-store" });
  } catch (e) {
    markReachable(false); // never reached the server (connection refused / DNS)
    throw e;
  }
  if (isProxyDown(r.status)) { markReachable(false); throw new Error(`${url} → ${r.status}`); }
  markReachable(true); // server answered, even if with an app-level error below
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

export interface ActivityScopeParams {
  prAuthor?: string;
  operator?: string;
}

function activitySearch(scope: ActivityScopeParams = {}, initial?: Record<string, string>): URLSearchParams {
  const qs = new URLSearchParams(initial);
  if (scope.prAuthor) qs.set("pr_author", scope.prAuthor);
  if (scope.operator) qs.set("operator", scope.operator);
  return qs;
}

// --- GitHub Issues, folded into the app (#192) ---
export interface IssuePR {
  pr: number; title?: string | null; how?: "explicit" | "fix-found" | "issue-ref" | "subsystem" | null;
  in_store?: boolean; state?: "open" | "merged" | "closed" | null;
}
export interface IssueRow {
  number: number;
  title: string | null;
  author: string | null;
  trusted_author?: boolean;
  author_stats?: AuthorStats | null;
  labels: string[];
  comments: number;
  reactions: number;
  thumbs_up: number;
  created_at?: string;
  updated_at?: string;
  state: string;
  url: string;
  subsystem: string | null;
  repro_grade: string | null;
  repro_score: number | null;
  pain: number | null;
  cluster: number | null;
  cluster_size: number;
  canonical: number | null;
  is_dup: boolean;
  duplicates: number[];
  disposition: IssueTriageDisposition | null;
  linked_prs: IssuePR[];
  linked_pr_count: number;
  referenced_pr_count: number;
  referenced_merged_count: number;
}
/** Per-column filters for the Issues table (#494) — the issue-side analog of
 *  PR Explorer's FilterSpec. `author` is a starts-with match, `subsystem` and
 *  `labels` are substring matches (`labels` against any of the issue's
 *  labels); `repro_grade` accepts one value or several (OR'd); `pain`, `dups`
 *  (duplicate count), and `linked_prs` (linked-PR count) are numeric compares. */
export interface IssueFilterSpec {
  author?: string;
  pain?: NumCmp;
  repro_grade?: string | string[];
  subsystem?: string;
  dups?: NumCmp;
  linked_prs?: NumCmp;
  labels?: string;
}
export type IssueDisposition = "not-planned" | "completed" | "fixed" | "dup";
/** The issue pipeline's per-issue triage verdict (issue ANALYZE). */
export type IssueTriageDisposition = "close-dup" | "close-fixed" | "request-repro" | "link-pr" | "needs-human";
/** The stored analysis section: the verdict plus the agent's written reasons. */
interface IssueAnalysis {
  disposition: IssueTriageDisposition;
  gist?: string | null;
  rationale?: string | null;
  asks?: string[] | null;
  canonical?: number | null;
  fixed_by?: number | null;
}
/** Full issue detail for the flyout: the table row plus body + analysis. */
export interface IssueDetail extends IssueRow {
  body: string | null;
  analysis: IssueAnalysis | null;
  fixed_comment: string | null;
  dup_comment: string | null;
  cluster_label: string | null;
}
export interface IssueExecResult { issue: number; action: string; status: string; detail: string; canonical?: number | null; forced?: boolean }

export type AlertSource = "code-scanning" | "dependabot" | "secret-scanning";
export type AlertState = "open" | "dismissed" | "fixed";
export type AlertSeverity = "critical" | "high" | "medium" | "low";
export type AlertVerdict = "fixed" | "likely-fixed" | "not-fixed";
export interface AlertLink { kind: "pr" | "issue"; number: number; how: string; note?: string | null; state?: string | null }
export interface AlertRow {
  id: number;
  source: AlertSource;
  number: number;
  state: AlertState;
  raw_state: string | null;
  severity: AlertSeverity;
  title: string | null;
  rule_id: string | null;
  package: string | null;
  ecosystem: string | null;
  manifest_path: string | null;
  secret_type: string | null;
  path: string | null;
  start_line: number | null;
  html_url: string;
  created_at: string | null;
  updated_at: string | null;
  verdict: AlertVerdict | null;
  action: string | null;
  evidence: string | null;
  links: AlertLink[];
  link_count: number;
  dismissed_reason: string | null;
  quality: boolean;
}
/** Full alert detail for the side panel: the row plus the raw meta section and
 *  the valid dismissal reasons for the alert's source. */
export interface AlertDetail extends AlertRow {
  meta: Record<string, unknown>;
  dismiss_reasons: string[];
}
export interface AlertQueryResult { items: AlertRow[]; total: number; offset: number; limit: number; pr_states_loading: boolean }
export interface AlertCaps { available: boolean; sources: Record<AlertSource | "advisory", boolean> }
export interface AlertDismissResult { source: AlertSource; alert: number; action: string; status: string; detail: string; reason: string; forced?: boolean }
export type AdvisoryState = "triage" | "draft" | "published" | "closed" | "withdrawn";
export type AdvisorySeverity = AlertSeverity | "unknown";
export type AdvisoryVerdict = "fixed" | "likely-fixed" | "not-fixed" | "duplicate";
export interface AdvisoryRow {
  id: number;
  ghsa_id: string;
  state: AdvisoryState;
  severity: AdvisorySeverity;
  summary: string | null;
  reporter: string | null;
  cve_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  html_url: string;
  verdict: AdvisoryVerdict | null;
  by: "deterministic" | "agent" | null;
  duplicate_of: string | null;
  fix_commit: string | null;
  evidence: string | null;
  links: AlertLink[];
  link_count: number;
}
/** Detail for the side panel: the row plus the report body and the full fix-scan section. */
export interface AdvisoryDetail extends AdvisoryRow {
  description: string;
  cwe_ids: string[];
  vulnerable_range: string | null;
  patched_versions: string | null;
  fix_scan: Record<string, unknown> | null;
}
export interface AdvisoryQueryResult { items: AdvisoryRow[]; total: number; offset: number; limit: number; pr_states_loading: boolean }
interface IssueDup {
  number: number;
  title: string | null;
  author: string | null;
  trusted_author?: boolean;
  repro_grade: string | null;
  url: string;
}
export interface IssueDupGroup {
  canonical: number;
  canonical_title: string | null;
  canonical_url: string;
  cluster: number | null;
  label: string | null;
  pain: number | null;
  subsystem: string | null;
  linked_prs: IssuePR[];
  dups: IssueDup[];
  dup_comment: string;            // default note for close-as-dup, prefilled into the box
  fixed_comment: string | null;   // default note for close-as-fixed, or null when no fixer
}
/** One tier-1 already-fixed issue: open, close-fixed disposition, current fix
 *  scan, and a fixer PR a live check shows merged. `comment` is the templated
 *  close note the executor posts by default. */
export interface IssueFixedItem {
  number: number;
  title: string | null;
  fixed_by: number;
  gist: string | null;
  rationale: string | null;
  upstream_date: string | null;
  pain: number | null;
  comment: string;
  url: string;
  fixer_url: string;
}
/** One tier-2 likely-fixed issue, surfaced for human review in the flyout. */
export interface IssueLikelyFixedItem {
  number: number;
  title: string | null;
  gist: string | null;
  rationale: string | null;
  pain: number | null;
  url: string;
}

/** Where the app's 🐞 Feedback button files issues, plus the operator login
 *  to pre-assign and this checkout's branch/worktree for the issue's context
 *  footer. The frontend can't resolve repo or login on its own; a null repo
 *  means no feedback repo is configured and the button is hidden. */
export interface FeedbackTarget {
  repo: string | null;
  assignee: string | null;
  labels: string[];
  branch: string | null;
  worktree: string | null;
}

/** The configured upstream repository's identity — every upstream link, the tab
 *  title, and repo-naming copy derive from this (backend /api/meta, cached once
 *  at bootstrap by RepoMetaProvider). */
export interface RepoMeta {
  configured: boolean;
  repo: string;
  owner: string;
  name: string;
  url: string;
  default_branch: string;
  display_name: string;
  feedback_repo: string | null;
  agent_provider: string;
  test_paths: { dir_pattern: string; file_pattern: string };
}

export interface TableColumn { name: string; json: boolean }
export interface TableSummary {
  name: string;
  description: string;
  row_count: number;
  columns: TableColumn[];
  preview: Record<string, unknown>[];
}
export interface TablePage {
  name: string;
  columns: TableColumn[];
  rows: Record<string, unknown>[];
  total: number;
}

/** One readiness check on this machine. `blocking` says whether failing it stops
 *  the machine processing work at all — a missing push identity limits it to
 *  verification rather than breaking it. */
export interface SetupCheck {
  key: string;
  label: string;
  ok: boolean;
  detail: string;
  remedy?: string | null;
  blocking: boolean;
}

/** What the backend serving this page still needs before it can process work.
 *  Always the local machine — the Setup view provisions the box you loaded it
 *  from, and the Control tab is where the whole fleet is reported. */
export interface SetupReadiness {
  host: string;
  checks: SetupCheck[];
  ready: boolean;
  autofix_ready: boolean;
}

/** The six worker lane switches, the only .env keys the lane writer may touch. */
export type WorkerFlags = Record<string, string>;

/** Where this checkout stands on the setup ladder. */
export interface OnboardingState {
  configured: boolean;
  /** The store snapshot is still on its cold load; `counts` is empty until
   *  it lands, and the wizard polls. */
  loading: boolean;
  repo: string;
  display_name: string;
  bot_login: string;
  writes_ready: boolean;
  worker_ready: boolean;
  /** The raw choice: null until the operator picks a provider. */
  agent_provider: string | null;
  counts: { prs?: number; clusters?: number };
}

/** One thing `probe` looked at. `problem` is a category, never raw error text. */
export interface ProbeFinding {
  ok: boolean;
  problem?: string;
  prs?: number;
  clusters?: number;
}

export interface ProbeResult {
  store?: ProbeFinding;
  repo?: ProbeFinding;
  key_file?: ProbeFinding;
  agent?: ProbeFinding;
}

/** Whether this machine can run the agent pane: the configured provider and,
 *  for claude, the local CLI's presence and login. */
export interface ChatReady {
  provider: string;
  ok: boolean;
  problem?: string;
  auth_method?: string;
  subscription?: string;
}

/** A GitHub user as the push-identity card needs it: the login, its id, and
 *  GitHub's per-account no-reply email. */
export interface PushAccount {
  login: string;
  id: number;
  email: string;
}

/** This machine's contributor-push key for a login: where it is and the public
 *  half the operator adds to the account. */
export interface PushKeyInfo {
  path: string;
  public_key: string;
}

/** What GitHub said about a key: `login` is the account it greeted, `ok`
 *  whether that is the one asked for. */
export interface PushProbeResult {
  ok: boolean;
  login: string | null;
  problem: string | null;
}

/** One step of setup. `bundle` supplies `env` and `profile` in their place. */
export interface OnboardingApplyBody {
  step: "connect" | "join" | "writes" | "worker" | "profile" | "agent";
  env?: Record<string, string>;
  profile?: Record<string, unknown> | null;
  bundle?: string;
}

export const api = {
  setupReadiness: () =>
    get<{ readiness: SetupReadiness; flags: WorkerFlags }>("/api/setup/readiness"),
  /** The deployment bundle a teammate pastes. `includeKey` adds the bot's
   *  private key, so their machine executes approved writes too;
   *  `includePushKey` the contributor-push identity, so it runs autofix. */
  setupShare: async (includeKey: boolean, includePushKey: boolean) => {
    const r = await fetch("/api/setup/share", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ include_key: includeKey, include_push_key: includePushKey }),
    });
    if (!r.ok) {
      const problem = await r.json().catch(() => ({ detail: `${r.status}` }));
      throw new Error(problem.detail ?? `${r.status}`);
    }
    return r.json() as Promise<{ bundle: string }>;
  },
  onboardingState: () => get<OnboardingState>("/api/onboarding/state"),
  /** The operator's own gh login with no argument, else the named account. */
  pushAccount: async (login?: string) => {
    const q = login ? `?login=${encodeURIComponent(login)}` : "";
    const r = await fetch(`/api/onboarding/push-identity/account${q}`);
    if (!r.ok) {
      const problem = await r.json().catch(() => ({ detail: `${r.status}` }));
      throw new Error(problem.detail ?? `${r.status}`);
    }
    return r.json() as Promise<PushAccount>;
  },
  pushKey: async (login: string) => {
    const r = await fetch("/api/onboarding/push-identity/key", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login }),
    });
    if (!r.ok) {
      const problem = await r.json().catch(() => ({ detail: `${r.status}` }));
      throw new Error(problem.detail ?? `${r.status}`);
    }
    return r.json() as Promise<PushKeyInfo>;
  },
  pushProbe: async (login: string, keyFile?: string) => {
    const r = await fetch("/api/onboarding/push-identity/probe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login, key_file: keyFile ?? null }),
    });
    if (!r.ok) throw new Error(`/api/onboarding/push-identity/probe → ${r.status}`);
    return r.json() as Promise<PushProbeResult>;
  },
  chatReady: () => get<ChatReady>("/api/chat/ready"),
  onboardingProbe: async (body: {
    store_url?: string; repo?: string; key_file?: string; agent?: boolean;
  }) => {
    const r = await fetch("/api/onboarding/probe", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`/api/onboarding/probe → ${r.status}`);
    return r.json() as Promise<ProbeResult>;
  },
  onboardingApply: async (body: OnboardingApplyBody) => {
    const r = await fetch("/api/onboarding/apply", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const problem = await r.json().catch(() => ({ detail: `${r.status}` }));
      throw new Error(problem.detail ?? `${r.status}`);
    }
    return r.json() as Promise<OnboardingState>;
  },
  setSetupFlags: async (flags: WorkerFlags) => {
    const r = await fetch("/api/setup/flags", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ flags }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({ detail: `${r.status}` }));
      throw new Error(body.detail ?? `${r.status}`);
    }
    return r.json() as Promise<{
      applied: { lanes: Record<string, string>; flags: WorkerFlags };
      readiness: SetupReadiness;
    }>;
  },
  clusters: () => get<{ items: ClusterSummary[] }>("/api/clusters"),
  tables: () => get<{ tables: TableSummary[] }>("/api/tables"),
  tableRows: (name: string, query: string) =>
    get<TablePage>(`/api/tables/${encodeURIComponent(name)}${query}`),
  feedbackTarget: () => get<FeedbackTarget>("/api/feedback/target"),
  generateFeedback: (description: string) =>
    fetch("/api/feedback/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description }),
    }).then((r) => r.json() as Promise<{ title: string; body: string }>),
  listIssues: () => get<{ items: IssueRow[]; pr_states_loading: boolean }>("/api/issues"),
  queryIssues: (opts: {
    q?: string; sort?: string; direction?: string; disposition?: string; state?: string;
    offset?: number; limit?: number;
  } & IssueFilterSpec = {}) =>
    fetch("/api/issues/query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    }).then((r) => r.json() as Promise<{ items: IssueRow[]; total: number; offset: number; limit: number; pr_states_loading: boolean }>),
  issueDuplicates: () => get<{ groups: IssueDupGroup[] }>("/api/issues/duplicates"),
  issuesAlreadyFixed: () =>
    get<{ fixed: IssueFixedItem[]; likely_fixed: IssueLikelyFixedItem[] }>("/api/issues/already-fixed"),
  getIssue: (n: number) => get<IssueDetail>(`/api/issues/${n}`),
  listAlerts: () => get<{ items: AlertRow[]; pr_states_loading: boolean }>("/api/alerts"),
  queryAlerts: (opts: {
    q?: string; sort?: string; direction?: string; source?: string; state?: string;
    severity?: string[]; verdict?: string; offset?: number; limit?: number;
  } = {}) =>
    fetch("/api/alerts/query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    }).then((r) => r.json() as Promise<AlertQueryResult>),
  alertCaps: () => get<AlertCaps>("/api/alerts/caps"),
  getAlert: (source: AlertSource, n: number) => get<AlertDetail>(`/api/alerts/${source}/${n}`),
  queryAdvisories: (opts: {
    q?: string; sort?: string; direction?: string; state?: string | string[]; verdict?: string;
    offset?: number; limit?: number;
  } = {}) =>
    fetch("/api/advisories/query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    }).then((r) => r.json() as Promise<AdvisoryQueryResult>),
  getAdvisory: (ghsa: string) => get<AdvisoryDetail>(`/api/advisories/${ghsa}`),
  dismissAlert: async (source: AlertSource, n: number, reason: string, comment: string, dryRun: boolean) => {
    const r = await fetch(`/api/execute/alert/${source}/${n}/dismiss?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason, comment: comment || null }),
    });
    return r.json() as Promise<AlertDismissResult>;
  },
  closeIssueDup: async (n: number, canonical: number | undefined, dryRun: boolean, comment?: string) => {
    const r = await fetch(`/api/execute/issue/${n}/close-dup?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ canonical, comment }),
    });
    return r.json() as Promise<IssueExecResult>;
  },
  closeIssueFixed: async (n: number, fixedBy: number, dryRun: boolean, comment?: string) => {
    const r = await fetch(`/api/execute/issue/${n}/close-fixed?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fixed_by: fixedBy, comment }),
    });
    return r.json() as Promise<IssueExecResult>;
  },
  closeIssue: async (
    n: number,
    body: { disposition: IssueDisposition; comment: string; fixed_by?: number; canonical?: number },
    dryRun: boolean,
  ) => {
    const r = await fetch(`/api/execute/issue/${n}/close?dry_run=${dryRun}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return r.json() as Promise<IssueExecResult>;
  },
  commentIssue: async (n: number, comment: string, dryRun: boolean) => {
    const r = await fetch(`/api/execute/issue/${n}/comment?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ comment }),
    });
    return r.json() as Promise<IssueExecResult>;
  },
  reopenIssue: async (n: number, dryRun: boolean) => {
    const r = await fetch(`/api/reopen/issue/${n}?dry_run=${dryRun}`, { method: "POST" });
    return r.json() as Promise<IssueExecResult>;
  },
  cluster: (id: number) => get<ClusterDetail>(`/api/clusters/${id}`),
  pr: (n: number) => get<PRDetail>(`/api/prs/${n}`),
  prActions: (n: number) => get<{ items: PRAction[] }>(`/api/prs/${n}/actions`),
  prHistory: (n: number) => get<{ items: PRHistoryItem[] }>(`/api/prs/${n}/history`),
  prReviews: (n: number) => get<{ reviews: ReviewsDetail }>(`/api/prs/${n}/reviews`),
  suggestForAction: (n: number, disposition: string) =>
    get<Suggestion>(`/api/suggest/pr/${n}?disposition=${encodeURIComponent(disposition)}`),
  defaultComment: (action: string, canonical?: number) => {
    const qs = new URLSearchParams({ action });
    if (canonical) qs.set("canonical", String(canonical));
    return get<{ comment: string }>(`/api/default-comment?${qs}`);
  },
  refreshLive: async (prs?: number[]) => {
    const r = await fetch("/api/live/refresh", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prs ? { prs } : {}),
    });
    return r.json() as Promise<{
      attempted: number;
      checked: number;
      changed: number;
      prs: number[];
      failed: number[];
      complete: boolean;
      fetched_at: string | null;
    }>;
  },
  liveStatus: () => get<{ fetched_at: string | null }>("/api/live/status"),
  scanResponses: async (prs?: number[]) => {
    const r = await fetch("/api/responses/scan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(prs ? { prs } : {}),
    });
    return r.json() as Promise<{ checked: number; with_response: number; prs: number[]; failed: number[] }>;
  },

  // Mark PR `n`'s current response signal as seen (#537) — for every operator,
  // until a newer response supersedes it.
  ackResponse: async (n: number) => {
    const r = await fetch(`/api/responses/${n}/ack`, { method: "POST" });
    return r.json() as Promise<{ pr: number; ack: PRResponseAck }>;
  },
  freshness: async (prs: number[]): Promise<{ items: FreshnessItem[] }> => {
    const r = await fetch("/api/freshness", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prs }),
    });
    return r.json();
  },
  queryPrs: async (spec: FilterSpec, opts: { sort?: string; direction?: string; offset?: number; limit?: number } = {}): Promise<QueryResult> => {
    const url = "/api/prs/query";
    let r: Response;
    try {
      r = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spec, ...opts }),
      });
    } catch (e) {
      markReachable(false); // never reached the server (connection refused / DNS)
      throw e;
    }
    if (isProxyDown(r.status)) { markReachable(false); throw new Error(`${url} → ${r.status}`); }
    markReachable(true); // server answered, even if with an app-level error below
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    return r.json();
  },

  // Match totals for a batch of filter specs, one total per spec in order —
  // backs the Home screen's cards, so a card's count always agrees with the
  // Explorer result set its link opens. While the backend snapshot is still
  // cold-loading, counts is null and loading is true; the caller polls.
  prCounts: async (specs: FilterSpec[]): Promise<{ counts: number[] | null; loading?: boolean }> => {
    const url = "/api/prs/counts";
    const r = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ specs }),
    });
    if (!r.ok) throw new Error(`${url} → ${r.status}`);
    return r.json();
  },

  searchPrs: (query: string) =>
    fetch("/api/prs/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    }).then((r) => r.json() as Promise<{ spec: FilterSpec; note: string }>),

  // Agentic Deep Search: judge `prs` against `query`, streaming progress; resolves
  // with the final matched set. Mirrors executeBulk's SSE frame parsing.
  deepSearch: async (
    query: string, prs: number[], onProgress: (p: DeepProgress) => void,
  ): Promise<DeepResult> => {
    const resp = await fetch("/api/prs/deep-search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, prs }),
    });
    const reader = resp.body!.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let result: DeepResult = { matches: [], total: 0, capped: false, judged: 0, from_cache: 0 };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      buf = buf.replace(/\r\n/g, "\n");
      const frames = buf.split("\n\n");
      buf = frames.pop() ?? "";
      for (const f of frames) {
        const ev = /event: (\w+)/.exec(f)?.[1];
        const data = /data: (.*)/s.exec(f)?.[1];
        if (!data) continue;
        if (ev === "progress") onProgress(JSON.parse(data) as DeepProgress);
        else if (ev === "result") result = JSON.parse(data) as DeepResult;
      }
    }
    return result;
  },

  executeBulk: async (
    body: { prs: number[]; action: string; comment?: string;
            comments?: Record<number, string>; canonical?: number;
            method?: string; reason?: string; tags?: string[]; reviewer?: string; dry_run: boolean },
    onResult: (r: BulkResult) => void,
    onDone: (summary: Record<string, number>) => void,
  ) => {
    const resp = await fetch("/api/execute/bulk", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const reader = resp.body!.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      // sse_starlette frames are CRLF-delimited; normalize so the blank-line
      // split works regardless of the server's line endings.
      buf += dec.decode(value, { stream: true });
      buf = buf.replace(/\r\n/g, "\n");
      const frames = buf.split("\n\n");
      buf = frames.pop() ?? "";
      for (const f of frames) {
        const ev = /event: (\w+)/.exec(f)?.[1];
        const data = /data: (.*)/s.exec(f)?.[1];
        if (!data) continue;
        if (ev === "result") onResult(JSON.parse(data) as BulkResult);
        else if (ev === "done") onDone(JSON.parse(data).summary as Record<string, number>);
      }
    }
  },

  executeCluster: async (
    items: Array<{ pr: number; action: string; comment?: string; reason?: string;
                   canonical?: number; method?: string; upstream_pr?: number;
                   upstream_commit?: string; upstream_date?: string; tags?: string[] }>,
    dryRun: boolean,
    onResult: (r: ExecResult) => void,
    onDone: (summary: Record<string, number>, aborted?: string | null) => void,
  ) => {
    const resp = await fetch("/api/execute/cluster", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, dry_run: dryRun }),
    });
    const reader = resp.body!.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      // sse_starlette frames are CRLF-delimited; normalize so the blank-line
      // split works regardless of the server's line endings.
      buf += dec.decode(value, { stream: true });
      buf = buf.replace(/\r\n/g, "\n");
      const frames = buf.split("\n\n");
      buf = frames.pop() ?? "";
      for (const f of frames) {
        const ev = /event: (\w+)/.exec(f)?.[1];
        const data = /data: (.*)/s.exec(f)?.[1];
        if (!data) continue;
        if (ev === "result") onResult(JSON.parse(data) as ExecResult);
        else if (ev === "done") {
          const d = JSON.parse(data) as { summary: Record<string, number>; aborted?: string | null };
          onDone(d.summary, d.aborted);
        }
      }
    }
  },
  diff: async (n: number): Promise<DiffResult> => {
    const r = await fetch(`/api/prs/${n}/diff`, { cache: "no-store" });
    if (!r.ok) return { diff: "", error: `HTTP ${r.status}`, note: null, file_count: 0, truncated: false, source: "" };
    return r.json();
  },
  jobSpecs: () => get<{ specs: JobSpec[] }>("/api/jobs/specs"),
  jobsList: () => get<{ jobs: JobRec[] }>("/api/jobs"),
  identities: () => get<IdentitiesResult>("/api/identities"),
  // "Retry live mode" — re-probes whether this machine can mint a bot
  // token, since the backend only probes once and caches the result for its
  // whole lifetime otherwise (see /api/identities/refresh).
  refreshIdentities: async (): Promise<IdentitiesResult> => {
    const r = await fetch("/api/identities/refresh", { method: "POST" });
    return r.json();
  },
  executePr: async (n: number, action: CloseActionBody, dryRun: boolean) => {
    const r = await fetch(`/api/execute/pr/${n}?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(action),
    });
    return r.json() as Promise<ExecResult>;
  },
  reopenPr: async (n: number, dryRun: boolean) => {
    const r = await fetch(`/api/reopen/pr/${n}?dry_run=${dryRun}`, { method: "POST" });
    return r.json() as Promise<ExecResult>;
  },
  runState: async (prs: number[]): Promise<{ states: Record<number, RunState> }> => {
    const r = await fetch("/api/actions/run-state", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prs }),
    });
    return r.json();
  },
  submitReview: async (n: number, event: string, body: string, dryRun: boolean, reason?: string,
                       tags?: string[], overrideStale?: boolean) => {
    const r = await fetch(`/api/review/pr/${n}?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, body, reason, tags, override_stale: overrideStale }),
    });
    return r.json() as Promise<ExecResult>;
  },
  activityProgress: (scope: ActivityScopeParams = {}) =>
    get<ActivityProgress>(`/api/activity/progress?${activitySearch(scope)}`),
  activityIssueProgress: (scope: Pick<ActivityScopeParams, "operator"> = {}) =>
    get<IssueActivityProgress>(`/api/activity/issue-progress?${activitySearch(scope)}`),
  activityFirehose: (days = 30, allTime = false, scope: ActivityScopeParams = {}) => {
    const qs = activitySearch(scope, { days: String(days) });
    if (allTime) qs.set("all_time", "true");
    return get<FirehoseStats>(`/api/activity/firehose?${qs}`);
  },
  prAuthors: () => get<{ authors: Array<{ login: string; pr_count: number }> }>("/api/activity/pr-authors"),
  activityPeople: () => get<{ people: ActivityPerson[] }>("/api/activity/people"),
  activitySummary: (p: { group_by?: string; since?: string; until?: string; identity?: string; operator?: string; pr_author?: string; include_dry_run?: boolean } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== "") qs.set(k, String(v));
    return get<ActivitySummary>(`/api/activity/summary?${qs}`);
  },
  trainingStats: () => get<{ count: number; with_reason: number; decisions: Record<string, number> }>("/api/training/stats"),
  capabilities: () => get<{
    login: string | null;
    merge_upstream: boolean;
    reviewers: ReviewerCap[];
    store_schema: { code_version: number; store_version: number | null; write_block: string | null } | null;
    write_block: string | null;
  }>("/api/capabilities"),
  instance: () => get<{ branch: string | null; worktree: string | null }>("/api/instance"),
  meta: () => get<RepoMeta>("/api/meta"),
  mergePr: async (n: number, dryRun: boolean, method = "squash", reason?: string) => {
    const q = reason ? `&reason=${encodeURIComponent(reason)}` : "";
    const r = await fetch(`/api/merge/pr/${n}?dry_run=${dryRun}&method=${method}${q}`, { method: "POST" });
    return r.json() as Promise<ExecResult>;
  },
  commentLine: async (n: number, file: string, line: number, body: string, dryRun: boolean,
                      overrideStale?: boolean) => {
    const r = await fetch(`/api/comment/pr/${n}?dry_run=${dryRun}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, line, body, override_stale: overrideStale }),
    });
    return r.json() as Promise<ExecResult>;
  },
  retriggerReview: async (n: number, reviewer: string, dryRun: boolean) => {
    const r = await fetch(`/api/reviews/${encodeURIComponent(reviewer)}/retrigger/pr/${n}?dry_run=${dryRun}`, { method: "POST" });
    return r.json() as Promise<ExecResult>;
  },
  queueVerify: async (n: number) => {
    const r = await fetch(`/api/prs/${n}/verify/queue`, { method: "POST" });
    const body = (await r.json().catch(() => null)) as { detail?: string; pr?: number; status?: string } | null;
    if (!r.ok) throw new Error(body?.detail ?? `queue verification → ${r.status}`);
    return body as { pr: number; status: string };
  },
  dequeueVerify: async (n: number) => {
    const r = await fetch(`/api/prs/${n}/verify/dequeue`, { method: "POST" });
    const body = (await r.json().catch(() => null)) as { detail?: string; pr?: number; status?: string } | null;
    if (!r.ok) throw new Error(body?.detail ?? `cancel verification → ${r.status}`);
    return body as { pr: number; status: string };
  },
  verifyRunner: () => get<VerifyRunner>("/api/verify/runner"),
  queueFix: async (n: number, action: FixAction, guidance?: string) => {
    const r = await fetch(`/api/prs/${n}/fix/queue?action=${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(guidance ? { guidance } : {}),
    });
    const body = (await r.json().catch(() => null)) as { detail?: string; pr?: number; status?: string } | null;
    if (!r.ok) throw new Error(body?.detail ?? `queue ${action} → ${r.status}`);
    return body as { pr: number; action: FixAction; status: string };
  },
  dequeueFix: async (n: number) => {
    const r = await fetch(`/api/prs/${n}/fix/dequeue`, { method: "POST" });
    const body = (await r.json().catch(() => null)) as { detail?: string; pr?: number; status?: string } | null;
    if (!r.ok) throw new Error(body?.detail ?? `cancel autofix → ${r.status}`);
    return body as { pr: number; status: string };
  },
  approveFix: async (n: number) => {
    const r = await fetch(`/api/prs/${n}/fix/approve`, { method: "POST" });
    const body = (await r.json().catch(() => null)) as { detail?: string; pr?: number; status?: string } | null;
    if (!r.ok) throw new Error(body?.detail ?? `approve autofix → ${r.status}`);
    return body as { pr: number; status: string };
  },
  fixRunner: () => get<FixRunner>("/api/fix/runner"),
  fixQueue: (days = 7, allTime = false, limit = 100) => {
    const qs = new URLSearchParams({ days: String(days), limit: String(limit) });
    if (allTime) qs.set("all_time", "true");
    return get<FixQueue>(`/api/fix/queue?${qs}`);
  },
  workStatus: () => get<WorkStatus>("/api/status/now"),
  autohunt: (days = 7, allTime = false, limit = 100) => {
    const qs = new URLSearchParams({ days: String(days), limit: String(limit) });
    if (allTime) qs.set("all_time", "true");
    return get<Autohunt>(`/api/autohunt?${qs}`);
  },
  verifyQueue: (days = 7, allTime = false, limit = 100) => {
    const qs = new URLSearchParams({ days: String(days), limit: String(limit) });
    if (allTime) qs.set("all_time", "true");
    return get<VerifyQueue>(`/api/verify/queue?${qs}`);
  },
  activity: (limit = 200) => get<{ items: ActivityItem[] }>(`/api/activity?limit=${limit}`),
  syncActivity: async (limit = 500) => {
    const r = await fetch(`/api/activity/sync?limit=${limit}`, { method: "POST" });
    return r.json() as Promise<{ synced: boolean; items: ActivityItem[] }>;
  },
  actionItems: (params: { status?: string; kind?: string } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) qs.set(k, v);
    return get<{ items: ActionItem[]; counts: Record<string, number> }>(`/api/action-items?${qs}`);
  },
  setActionItemStatus: async (id: string, status: string) => {
    const r = await fetch(`/api/action-items/${encodeURIComponent(id)}/status`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }),
    });
    return r.json() as Promise<ActionItem>;
  },
  pipelineStatus: () => get<PipelineStatus>("/api/pipeline/status"),
  suggestedActions: (view: SuggestedActionView) =>
    get<{ items: SuggestedAction[] }>(`/api/actions/suggested?view=${view}`),
};

export type SuggestedActionView = "prs" | "issues" | "alerts";

/** One Control-tab job worth running now, surfaced on the view whose data it
 * feeds; `count` is the default batch size for jobs that take one. */
export interface SuggestedAction {
  kind: string;
  title: string;
  reason: string;
  last_run: string | null;
  count: number | null;
  estimate_seconds: number | null;
}

interface PipelinePhase {
  phase: string;
  label: string;
  last_run: string | null;
}

/** One per-PR fact's freshness split: stamped against the PR's present head
 * (current), stamped before a later push (stale), or never stamped. */
interface SectionCoverage {
  current: number;
  stale: number;
  never: number;
}

/** Threat-scan coverage: the freshness split plus, over the uncovered PRs
 * (stale + never), whether this machine's diff cache already holds the current
 * head's diff (diff_cached_here) or the scan fetches it on demand as it runs
 * (diff_uncached_here) — a fetch-workload hint, not a coverage boundary. */
interface ThreatCoverage extends SectionCoverage {
  diff_cached_here: number;
  diff_uncached_here: number;
}

interface PipelineCoverage {
  total: number;
  clustered: number;
  not_clustered: number;
  analysis: SectionCoverage;
  security: SectionCoverage;
  threat: ThreatCoverage;
}

interface IssueCoverage {
  total: number;
  open: number;
  analyzed: number;
  pending_analysis: number;
}

/** Rough per-unit durations (seconds) averaged from recent runs-ledger history;
 *  null where no run has yet recorded a real, count-tagged duration to sample. */
interface PipelineEstimates {
  ingest_seconds: number | null;
  threat_scan_seconds_per_pr: number | null;
  analyze_clusters_seconds_per_cluster: number | null;
  issue_analyze_seconds_per_issue: number | null;
}

export interface PipelineStatus {
  phases: PipelinePhase[];
  coverage: PipelineCoverage;
  issue_coverage: IssueCoverage;
  estimates: PipelineEstimates;
}

export interface ActivityItem {
  at: string; kind: string; pr?: number; issue?: number; action?: string; status?: string;
  detail?: string; dry_run?: boolean; identity?: string; operator?: string;
  operator_email?: string; cluster?: number;
  cluster_id?: string | null; approved_count?: number; by?: string; reason?: string;
}

export interface ActivityProgress {
  open_total: number; universe: number; actioned: number; remaining: number;
  merged: number; closed: number; by_reason: Record<string, number>; pct: number;
}

export interface IssueActivityProgress {
  open_total: number; universe: number; actioned: number; remaining: number;
  closed: number; by_reason: Record<string, number>; pct: number;
}

export interface ActivityBucket { key: string; total: number; [k: string]: number | string }
export interface ActivitySummary {
  range: { since: string | null; until: string | null };
  group_by: string;
  totals: Record<string, number>;
  buckets: ActivityBucket[];
}

export interface FirehoseStats {
  days: string[];
  pr_incoming: number[];
  pr_closed: number[];
  pr_merged: number[];
  pr_reopened: number[];
  pr_triaged: number[];
  iss_incoming: number[];
  iss_closed: number[];
  totals: {
    pr_incoming_7d: number;
    pr_closed_7d: number;
    pr_merged_7d: number;
    pr_reopened_7d: number;
    pr_triaged_7d: number;
    iss_incoming_7d: number;
    iss_closed_7d: number;
    pr_incoming_nd: number;
    pr_closed_nd: number;
    pr_merged_nd: number;
    pr_reopened_nd: number;
    pr_triaged_nd: number;
    iss_incoming_nd: number;
    iss_closed_nd: number;
    // backwards compat aliases
    pr_incoming_30d: number;
    pr_triaged_30d: number;
    iss_incoming_30d: number;
  };
  reopened_after_close: Array<{
    pr: number; title: string | null; url: string | null;
    author: string | null; closed_at: string | null; reason: string | null;
  }>;
  iss_action_counts: Record<string, number>;
}

export interface ActivityPerson {
  display: string;
  login: string;
  is_operator: boolean;
  pr_count: number;
}

export interface ActionItem {
  id: string; kind: string; pr: number; summary: string; evidence: string;
  detail: string; status: string; created: string;
  pr_title?: string | null; pr_url?: string | null; pr_author?: string | null;
  pr_summary?: string | null;
}

export interface Identity { id: string; label: string; available: boolean; note: string | null }
export interface IdentitiesResult { identities: Identity[]; live_possible: boolean; live_error: string | null }
// `status: "stale"` is a refusal the operator can confirm past: the write quotes
// facts the author has moved beyond, and `stale` names the drift.
export interface ExecResult { pr: number; action: string; status: string; detail: string; forced?: boolean; stale?: StaleBlock }

/** One real action taken on a PR (close/merge/reopen/comment/review), from the
 *  activity log — the bot `identity` that posted it + the human `operator`. */
export interface PRAction {
  kind: string; action?: string | null; status: string;
  identity?: string | null; operator?: string | null;
  at: string; reason?: string | null; detail?: string | null;
  event_url?: string | null;  // deep-link to the exact GitHub event, when captured
}

/** One event in a PR's condensed upstream activity history — a comment,
 *  review (an automated reviewer's is `bot_review` with its `reviewer` id and,
 *  for Greptile, the parsed `score`), commit, or reopen/close/force-push/rename
 *  event. Oldest first. */
export interface PRHistoryItem {
  kind: "comment" | "review" | "bot_review" | "commit" | "reopened" | "closed" | "force_push" | "renamed";
  at: string;
  actor?: string | null;
  summary?: string | null;
  url?: string | null;
  state?: string | null;
  reviewer?: string | null;
  score?: number | null;
}

/** The latest landed live action on a PR, from the activity log (#10). */
export interface RunState {
  kind: string; status: string; at: string;
  action?: string | null; reason?: string | null;
  done: boolean; undoable: boolean;
}

export interface JobSpec { kind: string; label: string; needs_cluster: boolean; needs_pr?: boolean; needs_count?: boolean }
export interface JobRec { id: number; kind: string; cluster: number | null; pr?: number | null; count?: number | null; status: "queued" | "running" | "done" | "failed"; label: string; started: string; returncode: number | null }
