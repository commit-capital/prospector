# Issue funnel and issue-fix lane — design

Date: 2026-09-17
Status: approved (design), pending implementation

## Goal

Add Issues work queues beside the PR queues: a **funnel** that finds the open
issues of `TRIAGE_REPO` that have no outstanding fix, are well-formed and
reproducible, and are worth fixing; and an **issue-fix lane** in which
first-party agents reproduce such an issue as a failing test in the secretless
sandbox, author a fix, prove red→green on the host, get refuted by independent
reviewers, park for a human, and — once approved — open a pull request that
says `Fixes #N`. That pull request then flows through the existing PR pipeline.

No existing PR queue, gate, or fence changes behavior. Shared code gains a seam
only where a golden test pins the PR path byte for byte.

## Non-goals (v1)

- No unattended issue closes, and no second "keep it open" agent. Humans close
  through the existing executor paths.
- No label writes. The executor has no label path, `gh issue edit` would widen
  the bot's reach to titles and bodies, and a label write bumps `updated_at`.
- No comments in lane prompts. Lane agents read the frozen title and body only.
- No full-suite `regress` leg in the lane; the proposed PR meets it in VERIFY.
- No retry after a reviewer's rejection, and no operator override of a
  `not-a-defect` verdict.
- No reporter-confirmation preview builds.
- No research agent for expected-behavior / obsolete close reasons.
- No unattended propose (the `propose_bar` is specified, not built).
- No replay that reverses one merged fix onto today's tree: the tree it yields
  never existed. The history replay runs on the actual pre-merge tree.
- No shared scaffolding extracted from `fix_worker.py`; its module state is
  monkeypatched by the PR-lane tests.

## Vocabulary

- **Report**: an issue's title and body. `report_sha` is
  `sha256(f"{title}\n{body}")[:16]`, derived, never stored on `meta`. It is the
  text every lane agent reads and the key of every lane run.
- **Pin**: the base SHA and image a verify machine holds in the `verify_base`
  registry. All lane proof runs on the pin.
- **Reproduction**: NEW test file(s) that fail on the pin for the reported
  symptom. Authored with no sight of any fix.
- **Coverage**: whether someone is already fixing the issue —
  `unknown | fixed | in-lane | claimed | likely-claimed | uncovered | unclaimed`.
- **Candidacy**: whether the hunter may pick the issue, why not, and its rank.
- **Lane-authored PR**: a PR whose author is `TRIAGE_BOT_LOGIN` or whose head
  ref is in the `prospector/issue-*` namespace of the push login's fork.
- **Standing**: `{column, bucket, owner, reason}` — whose move an issue is, the
  issue-side analog of `automation.classify` for PRs.

## Invariants

1. The lane's output is a PR that enters the existing PR pipeline, which is its
   independent QA: different agents, different context.
2. Close before fix. The lane sees only what survives closability and coverage.
3. Agents judge; the host decides. A reproduction is `reproduced` only when the
   host saw `SENTINEL_TEST_FAIL` (20) twice; a fix is proven only when the host
   saw `SENTINEL_PASS` (0) twice. Agent sandbox runs are recorded and never
   consulted.
4. The reproduction is authored by a different agent context than the fix,
   frozen before the fix stage, and the fix may not add, change, or delete any
   test path.
5. Every stage reads one frozen report. A live `report_sha` that differs
   cancels the run; a moved `updated_at` alone (a comment, a label, the bot's
   own cross-reference) does not. `report_sha` is the hunter's one-attempt key.
6. All proof runs on the pin. The lane never calls `compile_preflight`, which
   resolves live default-branch HEAD and builds an image for it. The proposed
   branch is cut from the pin the proof ran on, so the tree proposed is the
   tree proven.
7. Fail closed. Park before any upstream write. Every step of autonomy is an
   opt-in switch plus a budget; the switches that open PRs are `.env`-only.
8. One policy module: all new issue policy lives in
   `issue_triage/issue_gates.py`. Coverage, candidacy, and standing are
   computed on read and never stored.

## Architecture

```
open issues ─► INGEST+ ─► tier-0 facts ─► deterministic screen ─► ASSESS ─► rank
   funnel                                    │ derived coverage     │ top-K only:
                                             ▼                      ▼ FIX-MATCH, FIND-FIXED
             needs-info worklist (human-clicked comment)    fix_candidacy.eligible
                                                                    │
   lane    queue ─► REPRODUCE ─► judge ─► FIX ─► prove ─► 2 reviewers ─► park
            ▲ operator click / hunter        (all on the worker's pin)
                                                                    │ approve
             propose: push user ─► fork branch ─► App opens PR "Fixes #N"
                                                                    ▼
                  existing PR pipeline (+ added requirements for lane-authored PRs)
```

Layering: `pipeline/` and `issue_triage/` never import `prospector_app`. The
lane core (`issue_triage/fix_lane.py`) imports only `pipeline.*` and
`issue_triage.*`, so the worker (`prospector_app/backend/issue_fix_worker.py`)
and the trials runner (`pipeline/evals/issue_fix_trials.py`) call the same
code.

## Store

### Issue sections

`ISSUE_SECTIONS` gains `fix_request`, `reproduction`, `assessment`, `thread`,
`pr_match`, `info_request`. `UPDATED_BOUND` gains `fix_request`,
`reproduction`, `assessment`, `thread`, `pr_match`. `links` stays unbound and
gains the sibling key `github` (GitHub's closing references, `[{pr, state,
draft}]`) beside `candidates`; every `links` writer preserves siblings.

**`fix_request`** — the queue; replaced whole on each transition.
`status`, `action` (`reproduce | fix`), `step`, `host`, `attempts`, `mode`
(`normal | blind`, absent = normal), `source` (`operator | auto`, absent =
operator), `queued_at`, `started_at`, `finished_at`, `report_sha`, `base_sha`,
`ending`, `refused_reason` | `error`, `withheld_pr` (blind), and `result`:
`patch` (test ⧺ fix bytes, exactly what would be proposed), `changes`,
`summary`, `root_cause`, `proof` (`red`, `green`, `compile`, `related_tests`),
`reviews[]`, `threat`, `tier`, `checks`, `pr_title`, `pr_body`,
`pushed_head_sha`, `pushed_tree_sha`, `pr`, `pr_url`, `cross_check`.

**`reproduction`** — the fact; survives re-queues (the `verify_request` /
`verify` split). `outcome` (`reproduced | not-reproduced | unwritable |
wrong-symptom | not-a-defect`), `base_sha`, `tier`, `host`, `report_sha`,
`mode`, `files[{path, contents}]`, `test_cmd` (host-derived),
`claimed_symptom`, `expected_red_signature`, `red` (`exit`, `exit_confirm`,
`output_tail`, `duration_s`), `judge` (`symptom_match`, `defect`, each
`{…, confidence, reasoning}`), `give_up`, `checks`.

**`assessment`** — ASSESS facts (see Funnel). **`thread`** —
`{comments: [{login, association, is_bot, at, body}]}`, last 8, bodies clipped
to 1,500 characters, omitted from the app's light snapshot. **`pr_match`** —
`status` (`addressed | partial | unaddressed`), `matches`, `considered`,
`judged: [{pr, head_sha}]`, `by`. **`info_request`** — unbound:
`{status: "asked", asked_at, asked_for, body_sha}`.

### Validation (`validate_issue`)

Every enum above is checked. `awaiting-review`, `approved`, `proposing` require
`result.patch` starting with `diff `, `base_sha`, and `report_sha`. `proposed`
requires an integer `result.pr`. `refused` requires `ending`; `failed` requires
`error`. `mode == "blind"` requires an integer `withheld_pr`. `reproduction`
requires `base_sha` and `report_sha`; `outcome == "reproduced"` additionally
requires well-formed non-empty `files`, `test_cmd`, and
`red.exit == red.exit_confirm == gates.SENTINEL_TEST_FAIL`.

### Store API

`IssueStore.claim_fix_request(n, *, host, statuses=("queued",),
to_status="running") -> dict | None` — the compare-and-swap of
`pipeline/store.py:818` over `_issues.stamped` / `save_if`.
`IssueStore.issues_matching(path, values)` over `Collection.where_json`.
`IssueStore.runs(limit=None, since=None)`. Worker heartbeats live in the PR
`Store` registries (`load_/save_/clear_issue_fix_worker`), because the local
SQLite fallback is one file per family. No mirror columns, no DDL.

`issue_ingest.ingest_records` reloads each changed issue in full and writes it
with `stamped()` / `save_if()` and one retry; a write from the start-of-run
snapshot can revert a claimed request and drops sections a light load omits.

### PR section

`issue_repro` joins `PR_SECTIONS` and `SHA_BOUND`: `entries: [{issue, base_sha,
test_files, test_cmd, c_exit, c_exit_confirm, e_exit, outcome, explanation,
lane_run, host}]`. No function in `pipeline/gates.py` reads it.

### Schema versions

Each step that changes record shape in a way older writers mishandle bumps
`STORE_SCHEMA_VERSION` with a changelog line (the first also backfills the
missing 20–22 lines). Bumps are serialized across the two tracks: the second to
land takes the next number. A bump makes every older checkout read-only
against the shared store, so every machine on it deploys together.

## Lane state machine

`queued → running → {done (action = reproduce) | awaiting-review → approved →
proposing → proposed}`. Terminal: `done`, `proposed`, `refused`, `failed`,
`cancelled`. A request parks only when `issue_gates.fix_proof_bar` passes; any
shortfall ends `refused` with the evidence and patch kept in `result`.

Steps: `claimed`, `preparing the clone`, `agent authoring the reproduction`,
`proving red on the pinned base`, `judging the reproduction`,
`agent authoring the fix`, `proving green`, `compile preflight`,
`related tests`, `reviewing: root-cause`, `reviewing: scope-safety`; when
proposing: `re-proving on the current pin`, `opening the pull request`.

| Class | Status | `ending` / kind |
|---|---|---|
| Verdict — rests until `report_sha` changes | `done` | `reproduced` |
| Verdict | `proposed` | — |
| Verdict | `refused` | `not-reproduced`, `unwritable`, `wrong-symptom`, `not-a-defect`, `no-fix`, `fix-untrusted`, `fix-unproven`, `fix-rejected`, `declined`, `ineligible`, `superseded`, `no-longer-applies`, `already-fixed-on-base` |
| Verdict | `cancelled` | `operator`, `issue-closed` |
| Re-arms at once | `cancelled` | `report-edited` |
| Machine fault — booked on lane health | `failed` | `agent-unavailable`, `run-failed`, `sandbox`, `no-base`, `base-compile`, `interrupted`, `propose-failed` |

`fix-untrusted` covers an undisclosed change, a touched test, a withheld path,
a dependency manifest, a threat signature, a binary or mode change, an oversize
patch, and a hygiene finding. A judge or reviewer that crashed, timed out, or
answered malformed, with no judged rejection beside it, is `run-failed`.

**Hunter rest** (`issue_gates.hunt_rested`): a request whose `report_sha`
differs from the issue's arms it; a verdict ending rests it; a `failed` ending
rests it for one hour, at most three failed attempts per `report_sha`. An
operator's click is bound by none of this.

**Orphans**: `recover_orphans` reads
`issues_matching(("fix_request","status"), ["running","proposing"])`; this
host's own are restart leftovers, another host's only once
`verify_worker.worker_offline` says it is gone; both end `failed` /
`interrupted`. `reproduction` is persisted the moment its stage concludes, so a
retry resumes at the fix stage. A `proposing` orphan is indeterminate; its note
tells the operator to look for an open bot PR first.

**The issue changed under us** (live `fetch_issues.fetch_issue`), checked at
claim, before the fix stage, before parking, at the approve click (store-only),
and at propose:

| Live observation | Response |
|---|---|
| closed | `cancelled` / `issue-closed` |
| title or body hash ≠ `report_sha` | `cancelled` / `report-edited` (a queued request adopts the current hash instead) |
| only `updated_at` moved | continue |
| fetch fails mid-run | continue |
| fetch fails at propose | `failed` — a write needs a live yes |

## Policy (`issue_triage/issue_gates.py`)

All pure, reading stored facts and taking live values as parameters.

```python
def security_terms_hit(issue: Issue) -> list[str]
def injection_markers(text: str) -> list[str]
def external_repro_refs(text: str) -> list[str]
def fix_eligibility(issue: Issue, cluster: IssueCluster | None, *, guided: bool,
                    reporter_blocked: bool, lane_identities_trusted: bool) -> tuple[bool, str]
def hunt_rested(issue: Issue, now: str | None = None) -> bool
def reproduction_outcome(red: dict, judge: dict | None, *, gave_up: bool,
                         invalid: str | None) -> str | None
def reproduction_standing(issue: Issue, base_sha: str | None) -> str
def fix_patch_regate(patch: str, result: dict, *, unattended: bool) -> tuple[bool, str]
def fix_proof_bar(result: dict) -> tuple[bool, str]
def coverage(issue: Issue, pr_links: list[PrLink] | None, *, today: str | None = None) -> Coverage
def fix_candidacy(issue: Issue, coverage: Coverage, cluster: IssueCluster | None, *,
                  issues: dict[int, Issue], today: str | None = None) -> Candidacy
def propose_eligibility(issue: Issue, request: dict, *, live_issue: dict | None,
                        live_base_sha: str | None, proof_base_is_ancestor: bool | None,
                        open_fixers: list[int] | None, approved_by: str | None) -> tuple[bool, str]
def propose_bar(result: dict, changed_paths: list[str], issue: Issue, *,
                open_lane_prs: int, open_lane_pr_limit: int) -> tuple[bool, str]
def lane_autonomy(records: list[PhaseRun], *, lane_rev: int, model: str,
                  today: str | None = None) -> dict
```

**`fix_eligibility`** (an operator's click; guidance chooses the job and lifts
no screen): refuses unless `issue.state == "open"` exactly (`None` refuses);
refuses an in-flight request, a blocklisted reporter, lane identities that
appear in `trusted_authors` / `automation_bots`, any `security_terms_hit`, any
`injection_markers` hit or stripped hidden content, a body over the lane cap.

**`reproduction_outcome`** mirrors `gates.verify_outcome`: a non-sentinel exit
or an unusable rating returns `None` (a fault); a red or confirm that passed is
`not-reproduced`; `matches` false or low confidence is `wrong-symptom`;
`is_defect` false or low confidence is `not-a-defect`; else `reproduced`.

**`fix_patch_regate`** calls `gates.fix_withheld_paths` (unioned with the
profile's `instruction_globs` and `issues.fix.deny_globs`),
`gates.deps_touched`, `risktier.pr_tier` (`None` or tier 0 refuses),
`threats.scan_diff` (`malicious` refuses), `gates.compile_preflight_gate`,
`gates.related_tests_block`, `verify_driver.validate_test_files`,
`author_fix.assert_disclosed` over the non-test paths, and
`gates.changed_line_count` (extracted from `gates.py:756-761`). New here: the
test-untouched sha256 rule, mode 100644 only, the `patch_hygiene` scans, and a
well-formed proof — red exactly 20, green 0, one `base_sha`, `test_cmd` equal
to `verify_driver.derive_test_command(repro_paths)`, red failures named inside
the reproduction files.

**`coverage`** — first match wins: `unknown` (PR index unavailable; fails
closed) → `fixed` (current `fix_scan` `fixed` / `likely-fixed`) → `in-lane` (a
run in flight or an open lane-authored PR) → `claimed` (an open PR linked
`explicit` or `github`, or a human assignee; PR health is evidence and never
releases the claim) → `likely-claimed` (an open `body-ref` / `issue-ref` PR a
current `pr_match` has not cleared, a `pr_match` of `addressed` / `partial`, a
quoted `maintainer_signal` of `working-on-it`) → `uncovered` (`pr_match`
current and `unaddressed`, `fix_scan` a valid `not-fixed`) → `unclaimed` (no
deterministic claim; `pending` names the missing checks).

**`fix_candidacy`** returns `{eligible, blockers[{code, kind, detail}], rank,
repro_tier, lane}`. Blocker kinds: `state` (closed; assessment missing or
stale), `coverage`, `pending` (`unknown`, `unclaimed`, unclustered), `info`
(`no-repro-steps`, `form-incomplete:<heading>`, `no-version`, vague steps with
`sandbox_reproducible: unlikely`), `route` (security — the deterministic screen
and the agent must both be clear; injection), `policy` (not a bug,
`sandbox_reproducible: no`, `maintainer_signal` `by-design` / `wontfix`, other
risk flags, `size: large`, withheld or tier-0 suspected paths, a non-canonical
duplicate). An operator's click may bypass `policy` and `likely-claimed`, never
`route` or `state`. `lane` ∈ `fix-candidate | pending-checks | needs-info |
awaiting-reporter | routed-security | needs-human | not-a-bug | covered |
unassessed`. Rank, ascending: `(repro_tier, -floor(pain·4), size,
-maintainer_confirmed, trusted, -form_score, newest, number)`; `repro_tier` 0 =
artifact `test` / `repo`, or `concrete` steps with sandbox `yes`; 1 =
`concrete` with `unlikely`, or `vague` with `yes` and a test hint; 2 = the
rest. `repro_grade` is not a rank key: its regexes match bare words.

**`propose_eligibility`** needs the re-gate over the stored patch,
`mode == "normal"`, recorded pushed SHAs, a live issue that is open with its
`report_sha` unchanged (`None` refuses), the proof base an ancestor of the live
base, still uncovered live (`None` refuses), a non-empty `approved_by` or a
recorded `propose_bar` pass, and `fix_pr_body.problems()` empty.

## Agent contracts

Common: `allow_gh=False`; `read_root` = the agent's clone; `env_allow`; title
and body inline, JSON-encoded, through `headless_agent.fill`, body capped at
8,000 characters; a Trust paragraph in the `BLIND_PROMPT` pattern (issue text
and sandbox output are attacker-controlled data). Replies through
`json_reply`. `AgentUnavailable` → fault; `AgentDeclined` → `declined`;
`EditsBlockedError` / `RuntimeError` / `ValueError` → `run-failed`.

**Reproduce** (`issue_triage/reproduce_issue.py`, 1,800 s): Read/Grep/Glob
scoped by `read_root`, Edit/Write scoped by `edit_root`,
`Bash(issue-sandbox-check:*)`, no git.
Reply `{files:[{path, purpose}], claimed_symptom, expected_red_signature,
confidence}` or `{give_up, kind}` with `kind` ∈ `needs-live-service |
insufficient-detail | cannot-isolate | not-a-code-defect`. Host validators, in
order: `git status --porcelain -z --untracked-files=all` lists only `??`
entries; regular UTF-8 files, no symlinks; `assert_disclosed`;
`validate_test_files(…, taken_paths=spec.taken_paths)`; the patch rebuilt from
contents by `authored_test_patch`, mode 100644; `threats.scan_diff` `clear`;
`patch_hygiene.test_payload_findings`. One retry when a validator rejects the
files or the first red leg exits 0, the rejection named to the agent.

**Judge** (`issue_triage/judge_repro.py`, 900 s, read-only, cwd = the repro
clone): gets the issue, the test contents, the agent's pre-committed
`claimed_symptom` and `expected_red_signature`, and the host's `red.output_tail`
fenced. Replies `{symptom_match:{matches, confidence, reasoning},
defect:{is_defect, confidence, reasoning}}`. It rates; `reproduction_outcome`
decides.

**Fix** (`issue_triage/fix_issue.py`, 1,800 s, fresh process and clone whose
one commit holds the pinned tree and the validated tests):
gets the issue, the frozen test paths, the red tail, `gates.fix_withheld_globs()`,
an issue-worded safety clause. Reply `{summary, root_cause,
changes:[{path, rationale}]}` or `{give_up}`. Host validators:
`sandbox_check.authored_patch`; non-empty, `diff `-led, ≤ 200,000 characters,
no `Binary files`, no `new file mode 120000`, no `old mode`; then
`fix_patch_regate`. Proof: `green_legs` over `test ⧺ fix`, the compile
lane when `verify.compile_cmd` is set, and the related tests excluding the
reproduction files and the pin's own failing set.

**Reviewers** (`issue_triage/review_issue_fix.py`, 900 s each, read-only, in
the fix clone): `LENSES = {"root-cause", "scope-safety"}`. `root-cause` runs
first and asks whether the change special-cases the test's input or passes the
test without curing the cause; `scope-safety` covers the `review_fix` list and
whether the change relaxes a check. The second is skipped after a judged
rejection. Only an explicit `safe` passes; a reviewer failure carries
`failed: True`.

## Sandbox primitives

`pipeline/prove.py`:

```python
class NoBase(RuntimeError): ...
@dataclass(frozen=True)
class PinnedBase: sha: str; tier: int; image: str; clone: Path
def pinned(store: Store) -> PinnedBase
def compose(label: str, *parts: Path | str | None) -> Path
def run_command(base: PinnedBase, patch: Path, cmd: str, *,
                phase: Literal["green", "compile"], label: str) -> dict
class Legs(TypedDict): exit: int | None; exit_confirm: int | None; output_tail: str; duration_s: float
def red_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str) -> Legs
def green_legs(base: PinnedBase, *, patch: Path, test_cmd: str, label: str) -> Legs
```

`pinned` raises `NoBase` on a missing pin, clone, daemon, or image. The confirm
container runs only after a 20 (red) or a 0 (green); exit 10 raises
`verify_driver.ProbeFailure`. `run_command` returns the compile-preflight
record shape plus `output_tail`, refuses an empty patch or one touching a
dependency manifest, and reuses `verify_driver.base_command_failure` to tell a
base fault from a patch fault. `compose` concatenates its parts into one file
under `SCRATCH/issue-fix/` and refuses parts that touch a common path: every
composition the lane makes is disjoint by construction (new test files, a fix
that may not touch tests, a withheld PR whose paths the tests avoid).

`prospector_app/agent/issue-sandbox-check` →
`prospector_app/backend/issue_sandbox_check.py`: pins arrive in
`PROSPECTOR_ISSUE_CHECK_{ISSUE,BASE,WORKTREE,TEST_PATCH,MAX_RUNS}`, never argv
(`TEST_PATCH` is unset in the reproduce stage and names the frozen tests in the
fix stage, whose clone has them committed);
it refuses a `BASE` that is not the machine's pin and refuses past `MAX_RUNS`
(8); lanes `typecheck` (phase `compile`) and `test <files>` (phase `green`);
it reuses `sandbox_check.lane_command`, `authored_patch`, `check_record`, and
prints up to 4,000 characters of `output_tail`. `sandbox_check.main` is
untouched. `pipeline/check_records.py` holds the path-keyed `append` /
`collect`; `sandbox_check.record_check` / `collect_checks` delegate to it.

`headless_agent.run_agent` gains `read_root: str | None = None` and
`env_allow: Sequence[str] | None = None`. With `None` the flag list and the
environment are today's, byte for byte. With `read_root` the bare `Read`,
`Grep`, `Glob` grants become `Read(//root/**)`, `Grep(//root/**)`,
`Glob(//root/**)` over the realpath — scoping `Read` alone leaves `Grep` and
`Glob` host-wide — and the read-only git rules are withheld, because
`git diff --no-index <file> /dev/null` reads any host file. With `env_allow`
the agent's environment is the CLI's own needs (`PATH`, `HOME`, `USER`,
`LOGNAME`, `SHELL`, `TMPDIR`, `LANG`, `LC_*`, `TERM`, and the `ANTHROPIC_` /
`CLAUDE_` prefixes) plus the named variables, then `env_extra`. The lane
names the Docker launcher variables; `pipeline/settings` loads the
repository `.env` in the tool's own process.

## Propose path

**Identities.** The push user (`TRIAGE_PUSH_LOGIN`, SSH key, no API token)
pushes to its own fork of `TRIAGE_REPO`. The App opens the cross-repo PR with a
token minted by `get-bot-token.sh --scope propose`
(`pull_requests: write, contents: read` on the one repository). The identity
that can push holds no API token; the token that can open a PR cannot push or
merge. Neither identity may appear in `profile.trusted_authors` or
`automation_bots`, nor in upstream's CI trusted-ID list.

**Tool** `prospector_app/agent/propose` (stdlib-only sibling of `resubmit`;
runs only under `resubmit_identity.uses_machine_user()`; in no chat
allowlist): `prepare`, `apply` (patch on stdin), `diff`, `state`, `abort`,
`push -m MSG --base-sha SHA [--expect-head SHA] [--dry-run]`, `delete-branch`.
Worktrees under `prospector_app/cache/propose/issue-<n>`. Remote `upstream`
has its push URL set to the literal `DISABLED`; `origin` is
`git@github.com:<push_login>/<repo_name>.git`, derived, never configured.

**Fence** `assert_propose_target(fork, origin_url, ref, issue, report8)`
raises unless: `origin_url` re-read from git config equals the derived fork URL
and differs from `settings.repo()`; the live fork read shows `fork` true,
`parent.full_name == TRIAGE_REPO`, owner = the push login, public, not archived
(an unreadable fork refuses); `ref` fully matches
`^prospector/issue-([1-9][0-9]{0,8})-([0-9a-f]{8})$` for this issue and this
`report_sha[:8]` — nothing in the name comes from issue text, because an
upstream `pull_request_target` workflow holding the App key receives the branch
name; the refspec is `HEAD:refs/heads/<ref>`; `HEAD` has exactly one parent,
the pin, which is an ancestor of the freshly fetched upstream default branch;
the lease is `--force-with-lease=refs/heads/<ref>:` (must not exist) or the
recorded head on a re-propose; the worktree holds only the applied patch. The
tool records `pushed_head_sha` and `pushed_tree_sha`, and logs `issue-propose`
Activity with `head_before` / `head_after`.

**Guard** (`safety_guard.py`; `_DENY` unchanged): `PROPOSE_WRITE_ALLOW`,
structural `assert_propose_write(argv)` — `argv[:4] == ["gh","api","--method",
"POST"]`, `argv[4] == f"repos/{settings.repo()}/pulls"`, the rest only `-f` /
`-F` pairs with keys `title`, `head`, `base`, `body`, `maintainer_can_modify`
once each; `head` = `<push_login>:` + a lane ref; `base` = the live default
branch; `maintainer_can_modify=false`; `title` matches `^[^\r\n]{1,120}$`;
`body` is `@<file>` under the propose scratch — and
`propose_bot_run(argv, token)` in the shape of `alert_bot_run`.

**Executor** `create_pr(issue: int, *, dry_run: bool, unattended: bool = False)
-> dict`, route `POST /api/execute/issue/{n}/propose` (`dry_run` defaults
true). The client supplies the issue number and `dry_run`; the executor pins
repo, base (resolved live; unresolvable refuses), head, title, body, flags.
Order: `issues.propose_gate` → custody check (fork ref SHA ==
`pushed_head_sha`, its `tree.sha` == `pushed_tree_sha`) → dry-run / no-token
return → adopt an open bot PR for the head, else `propose_bot_run`, treating
422 "already exists" as adopt → record `status="proposed"`, `pr`, re-ingest
the PR. Every branch records `activity.record("issue-propose", …)`; `KINDS`
gains the kind.

**Body** `issue_triage/fix_pr_body.py`: `render(...)` is host-written over the
repository's PR template (`describe_pr.fetch_template`, `required_sections`,
`missing_sections`); the fix agent supplies only bounded fields. It carries a
disclosure banner, the verification evidence (test paths, derived command, red
20 / green 0 at the pin, reviewer lenses), `report_sha`, the approving
operator, and the marker `<!-- prospector:issue-fix v1 issue=N base=… patch=… -->`.
`problems(title, body, commit_message, *, issue)` requires the required
sections, `link_prs.parse_issue_refs(body) == {issue}` for body and commit
message, no HTML / images / bidi characters, `TRIAGE_REPO`-only links,
neutralized `@`, length caps, and a clean self-secret scan. All evidence lives
in the body: `reopen_pr` deletes bot comments.

**Re-proving.** Approval re-proves on the machine's current pin when it
differs from `base_sha`, host-only: red exactly 20, green 0. A red that passes
ends `already-fixed-on-base`; a patch that no longer applies ends
`no-longer-applies`.

**Cleanup** `propose_gc` (worker, half-hourly): lists
`git ls-remote --heads origin 'prospector/issue-*'`; deletes a branch once its
PR is merged or closed or its request has been terminal for a day; a live head
that differs from `pushed_head_sha` closes the PR through the existing close
path, trips the lane, and escalates. A lane PR closed unmerged ends the request
`declined`; the lane never re-proposes that `report_sha`.

**Readiness** rows: the fork exists (Prospector never creates it), SSH reach
(`git ls-remote` under `push_env()`), identities untrusted, App
`pull_requests: write` (`executor.bot_permissions`).

**Hook** `.claude/hooks/deny-triage-writes.py`: `GH_WRITE` gains `create`; a
rule denies `prospector_app/agent/propose … (push|delete-branch)` from agent
shells.

## The PR pipeline and lane-authored PRs

Requirements are only ever added. Ingest stores `head_ref` and `head_repo` in
`meta`; `gates.lane_authored(pr)` is true on either signal.
`merge_eligibility` for a lane-authored PR requires a current GREEN security
verdict, a current non-negative verify outcome, and — behind
`issues.fix.require_human_approval`, default on — a maintainer's APPROVED
review at the head, read live in `merge_pr` (upstream's ruleset requires zero
approvals, so the App could otherwise author and merge). `merge_allowed` is
false for lane-authored PRs: the shipped test is agent-authored, so
`verified-fix` would be circular. `executor.submit_review` blocks a
self-approval before calling GitHub. ANALYZE validation refuses a `close-*`
whose canonical is a lane-authored PR and routes it to `needs-human`.
`gates.fix_eligibility` refuses lane-authored PRs. SECURITY and VERIFY prompts
are unchanged and never told the PR is first-party. A malicious signature on a
lane PR blocklists the bot login as for any author; the lane trips and
escalates.

## Funnel

**Links, derived on read.** `issue_triage/pr_index.py` `build(prs) ->
dict[int, list[PrLink]]` inverts the PR store's `issues.linked`, which every PR
ingest path refreshes from live bodies. `issue_triage/issue_links.py`
`linked_prs(issue, pr_links)` is the one merged accessor, replacing direct
`candidate_prs` reads in `prospector_app/backend/issues.py`. Evidence rank:
`explicit = github > fix-found > fix-match > issue-ref > body-ref > subsystem`.
Ingest adds `closedByPullRequestsReferences`, `assignees`, `lastEditedAt`.

**Threads.** `issue_triage/thread_fetch.py`: aliased batches of 25, only for
issues ingest is rewriting whose comment count is above zero; a failed fetch
skips that issue's rewrite.

**Tier-0.** Profile section `issues`: `forms[{name, kind, required, roles,
environment, asks}]`, `heading_roles`, `security_terms`, `cli_names`,
`fix{hunt, deny_globs, require_human_approval}`. `issue_triage/issue_forms.py`:
`parse_sections`, `match_form` (best fraction of required headings, floor 0.6),
`form_gaps` (absent, `_No response_`, or the unfilled template value).
`grade_repro(body, thread, policy)` v2: `steps`, `expected_actual`,
`code_fences`, `has_command`, `stack_frames`, `version` (semver, calver, SHA),
`paths`, `test_hint`, `form`, `form_missing`, `from_thread`; grades A–F keep
their 0–4 mapping; `SECTION_SCHEMA_VERSION["repro"] = 2`.

**ASSESS** (`issue_assess_driver.py` + `assess_issues.py`; tool-less agent,
batch 25; separate from ANALYZE, whose stored verdict feeds
`close_dup_allowed`). Facts only: `kind` (`bug | feature | enhancement | docs |
question | support`), `claim`, `repro_steps` (`none | vague | concrete`),
`repro_artifact` (`none | snippet | repo | test`), `environment`,
`sandbox_reproducible` (`yes | unlikely | no`) with `sandbox_reason`
(`needs-external-service | needs-credentials | needs-gui | platform-specific |
timing-perf | live-agent | fork-specific | infra-topology |
insufficient-detail`), `test_hint`, `suspected_subsystems`, `suspected_paths`
(≤ 10), `size`, `risk_flags` (`security-sensitive | auth | dependency-change |
data-migration | breaking-change | product-decision`), `maintainer_signal`
(`none | confirmed | working-on-it | by-design | wontfix | asked-for-info`),
`injection_suspect`, `quotes`. `verdict_error` enforces enums, membership in the
batch, `sandbox_reason` set exactly when not `yes`, subsystems within the
profile vocabulary, and that the quotes for `concrete` steps, a non-`none`
`maintainer_signal` (whose commenter must be trusted or MEMBER / OWNER /
COLLABORATOR), and `injection_suspect` are whitespace-normalized substrings of
the bundle. The agent emits no score, eligibility, or rank; its
`injection_suspect` and `security-sensitive` answers only add to the
deterministic screens.

**Needs-info.** `issue_triage/needs_info.py` `compose(issue, candidacy,
policy)` lists each `info` blocker in the form's own heading and `asks` text,
ending with `<!-- prospector:needs-info -->`. A human clicks Post; the existing
`executor.comment_issue` runs with `purpose="needs-info"`, a
`needs_info_gate`, and marker idempotency, and records `info_request`. One bot
ask per issue. A reply bumps `updated_at`, staling facts; `info_state` reads
`reporter-replied` when a non-bot comment postdates `asked_at` or the body hash
differs.

**FIX-MATCH.** `issue_triage/pr_shortlist.py`: score
`3·|shared rare identifiers| + 2·|shared paths| + [same subsystem ≠ other] +
|shared title keywords|`, keep ≥ 3, top 8, every open `body-ref` / `issue-ref`
PR forced in; an empty shortlist is a deterministic `unaddressed`.
`issue_match_driver.verdict_error`: every cited PR is in the issue's shortlist;
`addressed` needs a `fixes` match with non-`low` confidence and non-empty
`issue_symptom`, `pr_change`, `connection`; `unaddressed` needs `considered` to
cover the whole shortlist with a `why_not` each; `matches` and `considered`
partition the shortlist. `pr_match` is also stale when the current shortlist
holds a `(pr, head_sha)` outside `judged`. Run just-in-time for the top K
ranked candidates, K = 3 × daily lane capacity.

**FIND-FIXED upgrades.** Tier-0 `deterministic_fixed` (a merged PR linked
`explicit` / `github`, the issue open and not reopened, no non-bot comment
after the merge → `likely-fixed`, `by: "deterministic"`, never `fixed`); a
required `counter_evidence` field; `not-fixed` stays valid until a PR merged
after `checked_at` lands on the issue's shortlist, with a 30-day backstop; the
ranked merged shortlist in the bundle; a whole-run ledger entry.

**Sweep.** `issue_triage/issue_sweep.py` and job `issue-sweep`: ingest →
cluster → assess → match → find-fixed, continue on failure.

## Evaluation

**Blind trials against open PRs — the go-live gate**
(`pipeline/evals/issue_fix_trials.py`, job `fix-trials`). The lane is measured
on current code against independent work: an issue an open community PR claims
is run blind, and the two sides are crossed on the host. The store held 361
open PRs with an explicit `Fixes #N` link over 343 distinct issues on
2026-09-17.

Instance rules: T1 the issue is open and an open, non-draft PR links it
`explicit`; T2 the PR's diff applies onto the pin (`apply-check`); T3 the
verify safety floor holds for the contributor's code — threat verdict not
`malicious`, `gates.deps_touched` false; T4 the report never names the PR's
number, URL, or head SHA (discard, never scrub); T5 the PR is not bot- or
lane-authored; T6 the diff touches a non-test path. Discards are counted by
reason.

The lane runs with `mode="blind"` on the pin: `withheld_pr` set, `taken_paths`
= the PR's paths so the patches compose by concatenation, and the PR number,
`links`, and `analysis.gist` / `rationale` withheld from every prompt; agents
get `allow_gh=False` and `read_root`. A blind run ends `done` and never parks.
A replay of tool events marks any path outside the run's directory
`contaminated`.

Cells, one sandbox phase each, all host-observed:

| Cell | Tree on the pin | Command | Reads as |
|---|---|---|---|
| A | lane test | lane test cmd | red — the lane's own proof |
| B | lane test ⧺ lane fix | lane test cmd | green — the lane's own proof |
| C | community diff ⧺ lane test | lane test cmd | an independent fix validates the lane's test |
| D | community test hunks | community test cmd | red — the community tests are sound (reused from a current VERIFY on the same base) |
| E | community test hunks ⧺ lane fix | community test cmd | independent tests validate the lane's fix |
| F | community diff | community test cmd | green (reused from VERIFY likewise) |

Outcome from C and E: `agree` (both green), `community-only` (C green, E red),
`lane-only` (C red, E green), `disagree` (both red). A PR that ships no tests
reduces to cell C. A comparison reviewer runs only on non-`agree` outcomes,
with read-only tools, both diffs, both tests, and the cell exits; it returns
`{explanation_kind, evidence[{file, quote}], pr_gap, confidence}` with
`explanation_kind` ∈ `lane-test-wrong-reason | community-fix-incomplete |
community-tests-coupled | lane-fix-incomplete | different-symptoms |
undetermined`, and never sets the outcome. When the PR later merges at the head
the cells ran against, the instance reads `oracle_confirmed`.

Every instance also writes the PR's `issue_repro` entry, so a trial leaves
fix-blind red→green evidence on a real open PR.

Metrics: `reproduced`, `test_validated` (C green), `fixed`, `fix_validated`
(E green with D red and F green), `agree`, `false_accept` (every reviewer safe
∧ E red ∧ D red ∧ F green ∧ explanation not `community-tests-coupled`),
`false_reject`, `test_tamper`, `localized` (the lane touched a file the
community fix touched), per-stage seconds and cost, failures by subsystem and
top directory. Hold-out: `sha1(issue) % 4 == 0`. Ledger phases `trial:run` and
`trial:instance`; aggregates derived on read;
`prospector_app/backend/trials_view.py` and one Control-tab card.

**History replay — the calibration run** (`pipeline/evals/issue_fix_replay.py`,
job `fix-replay`). A batch that answers "would the lane have fixed bugs whose
fix is known?" on closed issues, run once the lane parks fixes and again when
the lane's prompts or model change. It shares the lane core and the scoring
with the blind trials.

The tree is the real one: the merged PR's first parent `P`, materialized on
the pin's image as `pin + git diff --binary <pin> <P>` with dependency-manifest
paths left out, so the source is history's and the installed dependencies are
the image's. Upstream's lockfile moves nearly daily, so an instance is held to
evidence, not to equal manifests: R6 below. No image is built.

Instance rules: R1 closed as completed with exactly one merged closing PR; R2
its merge commit is an ancestor of the pin, landed diff from `git show`; R3
the landed diff touches a test path and a non-test path, no dependency
manifest, ≤ 400 non-test lines, ≤ 10 files; R4 the issue predates the PR and
was not edited after it opened, and the report never names the PR's number,
URL, or SHA (discard, never scrub); R5 not bot- or lane-authored; R6 the known
fix proves itself in this sandbox — the oracle command
`derive_test_command(oracle_test_files)` exits 20 twice over `tree(P) ⧺ the
PR's test hunks` and 0 twice over `tree(P) ⧺ the landed diff`. Discards are
counted by reason; exits 10, 30, 40, and a timeout are sandbox faults and are
retried.

The lane runs with `pre_patch` = the transform to `tree(P)`: its clone is a
single-commit repository of that tree (`patchkit.fresh_repo`), so no history
reaches the agent; agents get `allow_gh=False`, `read_root`, and an anonymized
`RP-<hash>` id; a replay of tool events marks any path outside the instance
directory `contaminated`. Every sandbox patch is one diff from the pin
(`patchkit.flatten` of the transform and the lane's patches, which touch the
same files).

Scores, all from host exits: `reproduced`; `repro_valid` (the lane's test is
red on `tree(P)` and green on `tree(P) ⧺ the landed fix`); `fixed`;
`oracle_pass` (the merged PR's tests pass over `tree(P) ⧺ the lane's non-test
hunks`); `false_accept` (every reviewer safe ∧ oracle failed ∧ not
`oracle_coupled` — the test hunks reference symbols the fix hunks add);
`false_reject`; `test_tamper`; `localized`; per-stage seconds and cost;
failures by subsystem and top directory. After scoring, a comparison reviewer
in a fresh context reads the landed fix beside the lane's and returns
`{relation: equivalent | lane-better | landed-better | lane-wrong | undetermined,
evidence[{file, quote}], confidence}`; it informs the report and sets no
score. Ledger phases `replay:run` and `replay:instance`; the run is resumable
by instance, bounded by `--limit` and the lane's daily budget; aggregates are
derived on read and shown beside the trials on the Control-tab card.

**Autonomy** (`lane_autonomy`, a 30-day window of hold-out, uncontaminated
instances — trials and replay alike, a replay's `oracle_pass` reading as
`fix_validated` and its `repro_valid` as `test_validated` — at the current
lane revision and model; a revision or model change resets it). Hunter: ≥ 20 instances, `test_validated` ≥ 40% of reproduced,
machine faults ≤ 10%. Human-approved propose (the operator's call): suggested
≥ 20 instances whose PR ships tests, `fix_validated` ≥ 70% among
reviewer-accepted fixes, ≤ 2 false accepts, zero `test_tamper`. Unattended
propose: the hunter bar plus ≥ 30 reviewer-accepted instances with ≤ 1 false
accept, `fix_validated` ≥ 85% among accepted, zero `test_tamper`, and ≥ 10
human-approved lane PRs with ≥ 70% merged and none reverted.

## Threat controls

| Threat | Control |
|---|---|
| Prompt injection into a stage | one frozen sanitized report per run; deterministic `injection_markers` over the unclipped body; tool-less ASSESS whose answers only add; `read_root` + `env_allow`; no `gh` for lane agents |
| Malicious spec (a report asking for a relaxed check) | deterministic security screen that guidance never lifts; tier 0 refused; the judge's is-a-defect rating; the `scope-safety` lens; downstream SECURITY and a human approval |
| Test as payload | new test-path files only, patch rebuilt from contents, mode 100644, `patch_hygiene.test_payload_findings`, trojan-source scan; the fixer may not touch tests; fork PRs get no CI secrets |
| Dependency manipulation | `gates.deps_touched` refuses; offline image |
| Exfiltration through the published patch | `read_root`, `env_allow`, `patch_hygiene.self_secret_hits` over patch, title, body, and commit message including test files, `opaque_blobs` |
| Issue edited between stages | `report_sha` pinned at queue, re-checked at every checkpoint; propose fails closed on an unreadable issue |
| Flooding | per-worker daily budget from the ledger, in-flight cap, open-lane-PR cap, canonical-only, one attempt per `report_sha`, blocklisted reporters |
| Reporter-supplied repro repos | never fetched; links neutralized in the snapshot |
| Same-family reviewers missing a backdoor | deterministic gates carry the weight; upstream reviewers from other vendors gate merges; the unattended bar needs a second model family |
| Second-order injection through the authored test | `injection_markers` over the test's added lines; the fix prompt marks the test untrusted |
| CI over-trust | fork PRs; host-derived branch names; lane identities outside the CI trusted-ID list |

## Worker and control

`worker_control.WRITABLE` gains `TRIAGE_ISSUE_FIX_WORKER` and
`TRIAGE_ISSUE_FIX_AUTOHUNT`. `.env`-only: `TRIAGE_ISSUE_FIX_HUNT_LIMIT` (2),
`TRIAGE_ISSUE_FIX_BUDGET` (6 runs per worker per UTC day, counted from
`issue-fix:run` ledger entries where an agent ran), `TRIAGE_ISSUE_FIX_PROPOSE`
(off; opens the write surface). The hunter also requires `issues.fix.hunt` in
the profile. `issue_fix_worker.startup()` refuses without
`TRIAGE_VERIFY_WORKER=1` on the machine: that thread refreshes the pin, this
lane only reads it. Drain order: `open_or_retest("issue-fix")` →
`next_approved()` (only with propose enabled and `executor.live_possible()`) →
`next_queued()` → `next_auto()`. `worker_health.LANES` gains `"issue-fix"`;
`failed` endings book `note_failure`, every other ending `note_success`.

## App surface

Routes: `POST /api/issues/{n}/fix/queue?action=reproduce|fix`,
`/fix/dequeue`, `/fix/approve?dry_run=true`, `GET /api/issue-fix/queue`,
`GET /api/issue-fix/runner`, `GET /api/issues/fix-funnel`, `GET /api/trials`,
`POST /api/execute/issue/{n}/propose`. `issues._row` gains `coverage`,
`candidacy` (`lane`, `repro_tier`, `blockers`), `automation`, and a `fix`
summary; `query_issues` gains `coverage`, `lane`, `kind`, `automation_*`
filters and a `fix_rank` sort.
`prospector_app/backend/issue_automation.classify(issue, cluster, pr_states)`
is the one derivation every issue card, count, and filter reads.

| Column | Bucket | Meaning |
|---|---|---|
| act | `approve-fix` | a request is `awaiting-review` |
| act | `close-fixed`, `close-dup` | the existing close picks |
| act | `post-needs-info` | an askable issue |
| act | `assess` | no current assessment |
| auto | `queued`, `hunt`, `retry`, `pending-checks`, `proposed` | in a worker's hands |
| handed | `reporter-info` | asked, or not reproducible as reported — the reporter's move |
| handed | `linked-pr` | a contributor's PR claims it |
| handed | `not-a-defect`, `no-safe-fix`, `proposal-closed`, `routed-security`, `needs-human` | yours |

Frontend: `components/IssueFixPanel.tsx` in `IssueDetail.tsx`, which also
offers a human-clicked "Post reproduction" comment on a `reproduced` issue
(`executor.comment_issue` with `purpose="reproduced"`, marker-idempotent,
naming the base SHA and the test); a "Fix funnel"
tab in `views/Issues.tsx` (awaiting approval, in flight, fix candidates, needs
info, collapsed awaiting-reporter and security-routed, history); Home issue
cards generalized from a disposition to a query; one trials card on the
Control tab; a `Setup.tsx` switch row per writable flag.

## Spikes

S7 (run 2026-09-17 with canary files under `dontAsk --safe-mode
--setting-sources ""`): `Read(//root/**)` beside bare `Grep`, `Glob` denies an
outside Read and still lets Grep and Glob read outside; scoping all three
denies every outside call and keeps inside calls, relative paths included;
a `Read(//dir/**)` deny rule also blocks Grep and Glob there; the CLI
authenticates with `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TMPDIR`,
`LANG` alone. S12 (before the reproduce lane): new test files
authored from three historical fixed issues run offline in the tier-1 image.
Before propose goes live — S1: an App installation token, and the down-scoped
one, open a cross-fork PR; the 422 shape. S2: bot-authored fork PRs run
`pull_request` workflows unattended. S3: what
`require_extra_approval_for_unattributed_changes` does to an App-authored PR.
S4: upstream's quality-gate script's handling of `PR_AUTHOR` / `PR_BRANCH`.
S5: neither lane identity is in the CI trusted-ID list. S6: the external
reviewers review a bot-authored fork PR. S8: the must-not-exist lease. S9:
branch deletion against open, closed, and merged PRs. S10: a squash-merge
closes `Fixes #N`. S11: Actions disabled on the push user's fork.

## Build order

Two tracks in parallel; the task-by-task plan is
`docs/plans/2026-09-17-issue-fix-lane.md`. Lane: L1 sandbox primitives and
seams → L2 record shape, policy, queue → L3 reproduce lane → L4 fix, prove,
review, park → L5 blind trials against open PRs (cell C first, then D–F) and,
beside it, L5b the history replay (`patchkit`, the `pre_patch` slot, the
`fix-replay` job) → L6 propose (built dry-run; live after the owner reads the
L5 numbers) → L7 hunter → L8 unattended bar. Funnel: F1 fresh links → F2 threads → F3 tier-0 → F4 coverage →
F5 ASSESS → F6 candidacy, standing, worklists → F7 needs-info → F8 FIX-MATCH →
F9 FIND-FIXED upgrades → F10 sweep. L5 needs F1; L7 needs F6; the
`issues` profile section lands with whichever of L2 / F3 comes first.
