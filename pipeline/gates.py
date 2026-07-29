"""The ONE clean/merge gate policy.

Every consumer — the GATE phase, the cockpit chips, the executor's merge
check — calls these functions; nothing else encodes policy.

Policy (per the operator's bar): a merge candidate is CLEAN iff
  the configured review provider's bar (when one is set) ∧ CI passing
  ∧ mergeable/applicable ∧ open non-draft
  ∧ signals+drift computed against the current head.
A merge is ALLOWED iff additionally
  analysis is current with disposition=merge
  ∧ security verdict is current (head + ≤SECURITY_MAX_AGE_DAYS old)
  ∧ verdict is GREEN, or has a logged override
  ∧ dynamic verification is current (head + ≤VERIFY_MAX_AGE_DAYS old) and verified-fix.
Cluster state is DERIVED here, never stored.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from typing import TYPE_CHECKING

from pipeline import codeowners, diffpaths, profile, review_policy, settings
from pipeline.freshness import currency_failure, is_current

if TYPE_CHECKING:
    from pipeline.model import Cluster, Pr

SECURITY_MAX_AGE_DAYS = 7

VERIFY_MAX_AGE_DAYS = 7

# Merge-gate lanes in run order. A lane is required iff the active profile
# configures its command; configuring the key is the entire enforcement switch.
LANE_ORDER: tuple[str, ...] = ("compile", "build")


def configured_lanes() -> dict[str, str]:
    """The merge-gate lanes the active profile configures, in run order:
    lane name -> whole-repo command. A deployment with no lane keys gets {}."""
    v = profile.active().verify
    cmds: dict[str, str | None] = {"compile": v.compile_cmd, "build": v.build_cmd}
    return {name: cmd for name in LANE_ORDER if (cmd := cmds[name])}


def _lanes_verdict(lanes: dict | None) -> str | None:
    """The outcome a record's lane results force on a would-be verified
    outcome, or None when every recorded lane passed (or none were recorded).

    A lane whose command failed is a regression of the merged tree
    (`regressed`); any other non-pass entry — an infra exit, a malformed
    entry, a skip whose cause is not itself in the record — leaves the
    verification unconcluded (`escalate`): never silently mergeable, never
    silently blocked. The scan is order-independent and `regressed` always
    wins globally over `escalate`: escalate is operator-overridable but
    regressed is a hard block, so a malformed entry recorded ahead of a
    genuinely failed lane must never mask it. A lane exit of
    SENTINEL_PATCH_CONFLICT reads as escalate — the apply-check already
    proved the patch applies, so a lane-stage conflict is anomalous
    infrastructure, never needs-rebase."""
    if not lanes:
        return None
    entries = list(lanes.values())
    if any(isinstance(e, dict) and e.get("exit") == SENTINEL_TEST_FAIL for e in entries):
        return "regressed"
    # A skip entry (no "exit" key) counts toward escalate here safely: under
    # fail-fast a skip always coexists with the exit-20 lane that caused it,
    # which the check above already caught, so reaching this line with a skip
    # present and no failed lane is itself an anomaly worth escalating.
    if any(not isinstance(e, dict) or e.get("exit") != SENTINEL_PASS for e in entries):
        return "escalate"
    return None


def _lane_escalate_cause(signals: dict) -> str | None:
    """The lane name whose recorded entry left verification unconcluded — an
    infra exit or malformed entry — or None when no recorded lane did.

    Mirrors _lanes_verdict's regressed-over-escalate precedence: when any
    recorded lane entry is a dict with exit == SENTINEL_TEST_FAIL, that lane
    (or a skip alongside it) is the regressed cause, not this one, so this
    returns None. Otherwise the first entry that is not a dict, or whose exit
    is neither SENTINEL_PASS nor SENTINEL_TEST_FAIL, names the lane."""
    lanes = signals.get("lanes") or {}
    if any(isinstance(e, dict) and e.get("exit") == SENTINEL_TEST_FAIL
           for e in lanes.values()):
        return None
    for name, entry in lanes.items():
        if not isinstance(entry, dict) or entry.get("exit") not in (
                SENTINEL_PASS, SENTINEL_TEST_FAIL):
            return name
    return None


def _lane_regressed_cause(signals: dict) -> str | None:
    """The lane name whose recorded entry failed — exited SENTINEL_TEST_FAIL —
    or None when no recorded lane did."""
    lanes = signals.get("lanes") or {}
    for name, entry in lanes.items():
        if isinstance(entry, dict) and entry.get("exit") == SENTINEL_TEST_FAIL:
            return name
    return None


# Outcomes of the VERIFY phase. `verified-fix` is the fifth merge-bar element;
# every other outcome fails the bar, including `regressed` and `agent-verified`
# (corroborating evidence from an agent-authored test — strong for the operator,
# never an auto-merge signal). An infrastructure error is NOT here — it is a
# hold (verify_outcome returns None and no section is written), so a failed run
# can never present as a clean bill.
VERIFY_OUTCOMES = {
    "verified-fix", "agent-verified", "escalate", "not-verified", "needs-rebase",
    "regressed", "unverifiable-no-test", "unverifiable-needs-live-agent",
    "deps-touched",
}

# The sandbox's host-visible exit contract. A phase container's PID 1 is the
# trusted run-phase.sh; untrusted PR code runs as its child and cannot forge
# these as the host observes them. A red is accepted ONLY on exactly
# SENTINEL_TEST_FAIL: SIGKILL sent to a PID namespace's init from inside that
# namespace is discarded, so killing PID 1 is a no-op that exits 0.
# sandbox/run-phase.sh mirrors these values; test_verify_sentinels.py pins them
# to this declaration.
SENTINEL_PASS = 0
SENTINEL_PROBE_FAIL = 10
SENTINEL_TEST_FAIL = 20
SENTINEL_PATCH_CONFLICT = 30

# Disposition precedence for a PR that belongs to several clusters: the most
# blocking proposal wins (a close overrides a merge; needs-human overrides all).
# Lower index = higher precedence. The ONE place this order is defined.
DISPOSITION_PRECEDENCE: tuple[str, ...] = (
    "needs-human", "close-dup", "close-fixed", "close-stale",
    "request-changes", "merge",
)
_DISPOSITION_RANK = {d: i for i, d in enumerate(DISPOSITION_PRECEDENCE)}


def reconcile_disposition(proposals: list[dict]) -> dict | None:
    """Pick the most-blocking proposal among a PR's per-cluster proposals. Each
    proposal is a row carrying at least `disposition` and `cluster_id`. Ties on
    disposition break to the lower `cluster_id` (deterministic, so re-running is
    stable). Returns None when no proposal carries a known disposition."""
    ranked = [p for p in proposals if p.get("disposition") in _DISPOSITION_RANK]
    if not ranked:
        return None
    return min(ranked, key=lambda p: (_DISPOSITION_RANK[p["disposition"]],
                                      p.get("cluster_id", 1 << 30)))


# Workflow files are automation surface, not manifests: dependabot regenerates
# them, so they pass the bump exemption, but they never count as a dependency
# manifest for the VERIFY refusal.
_WORKFLOW_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")


def _is_manifest(path: str) -> bool:
    """True when a changed path is one of the profile's dependency manifests.
    Entries without a "/" match the basename (fnmatch); entries with one match
    the whole path in the diffpaths glob dialect."""
    p = diffpaths.normalize_path(path)
    if not p:
        return False
    base = p.rsplit("/", 1)[-1]
    return any(
        diffpaths.matches_glob(p, m) if "/" in m else fnmatch.fnmatch(base, m)
        for m in profile.active().dependency_manifests)


def is_dependabot_bump(author: str | None, changed_paths: list[str] | None) -> bool:
    """True iff this PR's author is one of the profile's automation bots AND
    every changed file is a dependency manifest, lockfile, or GitHub-Actions
    workflow — the shape of a genuine dependency bump.

    Such PRs are out of the pipeline's triage scope. The meaningful risk lives in
    the upgraded *package*, not in the diff — and the diff is all our threat
    signatures and the ANALYZE agent can see, so the agent has nothing real to
    judge and invents dispositions about packages it can't evaluate. Keeping them
    out of CLUSTER/ANALYZE (the diff is never fetched, so the threat scanner never
    signature-scans them either) removes that failure mode; the author still merges
    them upstream.

    The change-shape requirement is the security guard: an automation PR that
    touches anything else does NOT match, so a compromised or spoofed automation
    author cannot inherit the exemption with an arbitrary diff — it falls back to
    the full threat scan and analysis. An empty/unknown path list also fails
    closed.

    Both the automation-author list and the manifest vocabulary are repository
    policy data (the active profile's `automation_bots` and
    `dependency_manifests`): waiving a scan is an operator decision, set in
    configuration."""
    if author is None or author not in profile.active().automation_bots:
        return False
    paths = [p for p in (changed_paths or []) if p]
    if not paths:
        return False
    return all(_is_manifest(p) or _WORKFLOW_RE.search(diffpaths.normalize_path(p))
               for p in paths)


def pr_clean(pr: Pr, today: str | None = None) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    # Hard block: threat flags are sticky and fail closed. We do NOT exempt them
    # on staleness — if the head moved, the PR must be re-scanned, never silently
    # cleared. Detection lives in threats.py; this is just the gate consuming it.
    # A malicious verdict blocks outright; a committed credential (secret-leak,
    # a MEDIUM signal) is never merged as-is regardless of the overall verdict.
    sigs = pr.threat_signatures
    if pr.threat_verdict == "malicious":
        reasons.append(f"malicious: {', '.join(sigs) or 'flagged'}")
    if "secret-leak" in sigs:
        reasons.append("secret-leak: a live-looking credential is committed in the diff")
    if pr.state != "open":
        reasons.append(f"not open ({pr.state})")
    if pr.draft:
        reasons.append("draft")
    if not is_current(pr, "signals"):
        reasons.append("signals stale or missing")
    else:
        blocker = review_policy.active().clean_blocker(pr)
        if blocker:
            reasons.append(blocker)
        if pr.ci != "passing":
            reasons.append(f"ci {pr.ci}")
        if not pr.mergeable:
            reasons.append("merge conflicts")
    if not is_current(pr, "drift"):
        reasons.append("drift stale or missing")
    elif pr.drift_state != "applicable":
        reasons.append(f"drift {pr.drift_state}")
    return (not reasons, reasons)


def _analyzed_merge(pr: Pr) -> bool:
    """True iff ANALYZE's stored verdict is `merge`. The gates that mean "is this
    an ANALYZE merge pick" read the stored verdict and re-check the facts
    themselves with specific reasons; Pr.disposition (the derived route) would
    fold those facts into a generic non-merge answer first."""
    return (pr.section("analysis") or {}).get("disposition") == "merge"


def security_eligible(pr: Pr, today: str | None = None) -> bool:
    """Should the (expensive) deep security review run on this PR?
    Only clean merge candidates — never dirty or non-merge PRs."""
    if not is_current(pr, "analysis"):
        return False
    if not _analyzed_merge(pr):
        return False
    ok, _ = pr_clean(pr, today)
    return ok


def verify_eligible(pr: Pr, changed_paths: list[str], today: str | None = None) -> bool:
    """Should the (expensive) dynamic verification run on this PR? Only clean
    merge candidates with a parseable diff.

    The non-empty `changed_paths` precondition is the fail-closed half of the
    deps-touched gate: a PR whose diff is missing or unparseable yields no paths,
    which deps_touched would read as 'touches nothing'. Such a PR is simply not in
    the wave; the CLUSTER fetch-diffs step picks it up later.

    A deps-touching PR IS eligible — this function does not filter it out. The
    caller uses deps_touched to distinguish a deps-touching PR and route it to
    needs-human."""
    if not changed_paths:
        return False
    if not is_current(pr, "analysis"):
        return False
    if not _analyzed_merge(pr):
        return False
    ok, _ = pr_clean(pr, today)
    return ok


def merge_allowed(pr: Pr, today: str | None = None,
                  changed_paths: list[str] | None = None) -> tuple[bool, str]:
    """The executor's final gate before `gh pr merge`.

    When `changed_paths` is given, a CODEOWNERS-gated path forces a manual merge
    by a code owner — the bot must never auto-merge it (#15, #26)."""
    if not is_current(pr, "analysis"):
        return False, "analysis missing or stale — re-run ANALYZE"
    if not _analyzed_merge(pr):
        return False, f"disposition is {pr.disposition}, not merge"
    if changed_paths is not None:
        hm = codeowners.human_merge(changed_paths)
        if hm:
            owners = " ".join(hm["owners"])
            return False, (f"requires manual merge by a code owner ({owners}) — "
                           f"CODEOWNERS path: {', '.join(hm['paths'][:5])}")
    ok, reasons = pr_clean(pr, today)
    if not ok:
        return False, "not clean: " + "; ".join(reasons)
    why = currency_failure(pr, "security", max_age_days=SECURITY_MAX_AGE_DAYS, today=today)
    if why is not None:
        return False, f"security review {why} — re-run SECURITY"
    if pr.security_verdict != "GREEN" and not pr.security_override:
        return False, f"security {pr.security_verdict} without logged override"
    why = currency_failure(pr, "verify", max_age_days=VERIFY_MAX_AGE_DAYS, today=today)
    if why is not None:
        return False, f"dynamic verification {why} — re-run VERIFY"
    if pr.verify_outcome is None:
        return False, "dynamic verification has reached no verdict yet — re-run VERIFY"
    if pr.verify_outcome != "verified-fix":
        return False, f"dynamic verification {pr.verify_outcome}, not verified-fix"
    why = verify_signals_incomplete(pr)
    if why is not None:
        return False, f"verification incomplete: {why} — re-verify before auto-merge"
    return True, "clean + security cleared + verified"


def blocked_on_security(pr: Pr, today: str | None = None) -> bool:
    """True iff a clean merge-disposition PR is blocked solely because its security
    review is missing, stale, or older than SECURITY_MAX_AGE_DAYS — so re-running
    SECURITY is exactly what would unblock merge.

    The single source of truth for 'security is the merge blocker', so the cockpit
    surfaces its re-run button without re-deriving the 7-day policy client-side.
    Mirrors the security-currency branch of merge_allowed: fresh analysis, merge
    disposition, pr_clean, and a non-current security section."""
    if not is_current(pr, "analysis"):
        return False
    if not _analyzed_merge(pr):
        return False
    ok, _ = pr_clean(pr, today)
    if not ok:
        return False
    return not is_current(pr, "security", max_age_days=SECURITY_MAX_AGE_DAYS, today=today)


def security_cleared(pr: Pr, today: str | None = None) -> bool:
    """True iff the PR carries a security verdict that is current (head + ≤
    SECURITY_MAX_AGE_DAYS old) and GREEN. The bar the idle auto-hunter applies
    before any sandbox run on code it selected itself."""
    return (is_current(pr, "security", max_age_days=SECURITY_MAX_AGE_DAYS, today=today)
            and pr.security_verdict == "GREEN")


def merge_eligibility(pr: Pr, today: str | None = None,
                      changed_paths: list[str] | None = None,
                      override_reason: str | None = None) -> tuple[bool, str]:
    """Human-initiated merge gate — the cockpit action bar.

    More permissive than merge_allowed: an operator may merge any PR that passed
    every check we actually RAN on it — Greptile 5/5, CI, mergeable, fresh, no
    threat/secret — even one ANALYZE never reached, SECURITY never reviewed, or
    VERIFY never run.
    ANALYZE disposition is irrelevant here. Security blocks only when a review
    ran for this head and is not GREEN; a never-run review never blocks (we don't
    run the deep review on non-merge-candidate / Easy-Lane PRs). A CODEOWNERS path
    is a hard block — GitHub's ruleset enforces a human merge server-side.

    `override_reason` is the operator's stated reason to merge past a current
    YELLOW verdict; a non-blank reason clears the YELLOW block. The executor logs
    it durably as the verdict's override (Pr.log_security_override) before any
    live merge — passing it here without logging it is only for previewing the
    gate. RED is never overridable this way."""
    if changed_paths is not None:
        hm = codeowners.human_merge(changed_paths)
        if hm:
            owners = " ".join(hm["owners"])
            return False, (f"requires manual merge by a code owner ({owners}) — "
                           f"CODEOWNERS path: {', '.join(hm['paths'][:5])}")
    ok, reasons = pr_clean(pr, today)
    if not ok:
        return False, "not clean: " + "; ".join(reasons)
    # pr_clean already guarantees a fresh head, so a *current* security section
    # means the review ran on this head; its absence means it never ran. Only a
    # review that ran and isn't GREEN blocks.
    if is_current(pr, "security", max_age_days=SECURITY_MAX_AGE_DAYS, today=today):
        if pr.security_verdict != "GREEN" and not pr.security_override:
            if not (pr.security_verdict == "YELLOW" and (override_reason or "").strip()):
                return False, f"security {pr.security_verdict}"
    # Same rule as security above, keyed on the outcome. A verify section exists from
    # the moment the blind adequacy verdict is committed and carries an outcome only
    # once the phase reaches one, so the outcome is what says a verification
    # concluded. A null outcome (blind committed, or a run that errored and held) has
    # concluded nothing and blocks no more than a section that was never written.
    # Only a verification that reached a verdict on this head and did not confirm the
    # fix blocks.
    if (pr.verify_outcome is not None
            and is_current(pr, "verify", max_age_days=VERIFY_MAX_AGE_DAYS, today=today)):
        if pr.verify_outcome == "verified-fix":
            why = verify_signals_incomplete(pr)
            if why is not None:
                # The operator may still merge on partial evidence, but never
                # unknowingly: the reason names the gap and the cockpit displays it.
                return True, f"passed all checks run — verification incomplete: {why}"
        elif pr.verify_outcome == "agent-verified":
            # A concluded, corroborating state: the harness-authored test went
            # red->green, so nothing here says "the PR must change". The reason
            # names the provenance so the operator merges knowingly, and names
            # any configured-lane gap the same way the verified-fix branch does,
            # so partial evidence is never presented as complete.
            provenance = ("passed all checks run — fix corroborated by an "
                         "agent-authored test (not an author-shipped test)")
            why = verify_signals_incomplete(pr)
            if why is not None:
                return True, f"{provenance}; verification incomplete: {why}"
            return True, provenance
        elif not pr.verify_override:
            # escalate ("a human must decide") is the ONE verify outcome an
            # operator may merge past with a logged reason, exactly like a YELLOW
            # security verdict. Every other non-verified outcome (not-verified,
            # regressed, needs-rebase, deps-touched, the unverifiables) is either
            # "the PR must change" or "add a test" and stays a hard block.
            if not (pr.verify_outcome == "escalate" and (override_reason or "").strip()):
                return False, f"dynamic verification {pr.verify_outcome}"
    return True, "passed all checks run"


def security_overridable(pr: Pr, today: str | None = None,
                         changed_paths: list[str] | None = None) -> bool:
    """True iff the block a reason would clear is specifically a current YELLOW
    security verdict with no logged override. The cockpit uses this to surface
    the override-reason input, and the executor to log the reason to the right
    section — so it must name the SECURITY block, not an escalate verify block a
    reason would also clear (see verify_overridable)."""
    if not (is_current(pr, "security", max_age_days=SECURITY_MAX_AGE_DAYS, today=today)
            and pr.security_verdict == "YELLOW" and not pr.security_override):
        return False
    ok, _ = merge_eligibility(pr, today, changed_paths)
    if ok:
        return False
    ok_with, _ = merge_eligibility(pr, today, changed_paths, override_reason="operator override")
    return ok_with


def verify_overridable(pr: Pr, today: str | None = None,
                       changed_paths: list[str] | None = None) -> bool:
    """True iff the block a reason would clear is specifically a current escalate
    verify outcome with no logged override — the ONE verify outcome an operator
    may merge past. The cockpit shows the override-reason input and the executor
    logs the reason to the verify section only where this holds."""
    if not (is_current(pr, "verify", max_age_days=VERIFY_MAX_AGE_DAYS, today=today)
            and pr.verify_outcome == "escalate" and not pr.verify_override):
        return False
    ok, _ = merge_eligibility(pr, today, changed_paths)
    if ok:
        return False
    ok_with, _ = merge_eligibility(pr, today, changed_paths, override_reason="operator override")
    return ok_with


def compile_preflight_gate(result: dict) -> tuple[bool, str]:
    """The merge-time compile preflight verdict: (ok, reason) over the record
    pipeline/compile_preflight.py produces for a live merge — the profile's
    compile command run over (current default-branch HEAD + the PR's diff) in
    the sandbox. Fail-closed: only a run that concluded SENTINEL_PASS clears
    the merge; a refusal, an infrastructure error, a missing exit, or a
    non-sentinel exit blocks with its reason. The excerpt in a compile-failure
    reason is the untrusted container output's own text, bounded by the driver
    — display evidence for the operator, never part of the verdict, which is
    the host-observed exit code alone."""
    refused = result.get("refused")
    if refused:
        return False, str(refused)
    error = result.get("error")
    if error:
        return False, f"could not run ({error}) — live merge refused without a compile verdict"
    exit_code = result.get("exit")
    branch = settings.default_branch()
    base = str(result.get("base_sha") or "")[:12] or "unresolved"
    if exit_code is None:
        return False, "no exit recorded — live merge refused without a compile verdict"
    if exit_code == SENTINEL_PASS:
        return True, f"compile clean against {branch}@{base}"
    if exit_code == SENTINEL_PATCH_CONFLICT:
        return False, (f"the PR no longer applies onto current {branch} "
                       f"({base}) — needs a rebase")
    if exit_code == SENTINEL_TEST_FAIL:
        excerpt = str(result.get("error_excerpt") or "").strip()
        why = f"compile failed against current {branch}@{base}"
        return False, f"{why}: {excerpt}" if excerpt else why
    if exit_code == SENTINEL_PROBE_FAIL:
        return False, "sandbox isolation probe failed — refusing to run PR code"
    return False, (f"compile phase exited {exit_code}, not a sentinel — "
                   "infrastructure failure, live merge refused")


def security_disposition(pr: Pr) -> tuple[str, str] | None:
    """The disposition a current, non-overridden security verdict forces on a merge
    candidate, with rationale — or None when the verdict does not override (GREEN,
    missing, stale, or has a logged override).

    The SINGLE source of truth for the security→disposition consequence, read at
    disposition-derivation time (merge_demotion), so a re-run or override that
    clears the verdict clears the route with it. YELLOW ("should fix before
    merge") routes to request-changes — the findings are the asks; RED ("blocks
    merge / security vuln") routes to needs-human."""
    if not pr.section("security") or pr.security_override or not is_current(pr, "security"):
        return None
    title = (pr.findings or [{}])[0].get("title")
    verdict = pr.security_verdict
    if verdict == "RED":
        return ("needs-human",
                f"Security review is RED — {title or 'a serious security finding'}. "
                "Not a merge candidate.")
    if verdict == "YELLOW":
        return ("request-changes",
                f"Security review is YELLOW — {title or 'findings to address'}. "
                "Fix before merge.")
    return None


def deps_touched(changed_paths: list[str]) -> bool:
    """True iff this PR changes one of the active profile's dependency
    manifests (manifest, lockfile, or workspace file). Such a PR is refused
    before a sandbox boots and routed to needs-human: installing PR-controlled
    dependencies runs attacker lifecycle scripts.

    Callers must supply a non-empty, parsed path list — an unreadable diff yields
    no paths, and no paths means no match here. verify_eligible owns that
    precondition and keeps a diffless PR out of the wave entirely."""
    return any(_is_manifest(p) for p in changed_paths)


# Test-runner name-filter flags: vitest/jest `-t` / `--testNamePattern`,
# mocha `-g` / `--grep`, node:test `--test-name-pattern`. Matched as a whole
# whitespace-delimited token (value attached by `=` or space), so `pnpm -s test`
# or a path merely containing `-t` never matches.
_NAME_FILTER_RE = re.compile(
    r"(?:^|\s)(-t|-g|--testNamePattern|--test-name-pattern|--grep)(?=[=\s]|$)")


def vacuous_name_filter(blind: dict, host: dict) -> str | None:
    """The name-filter flag in the committed test_cmd, when the red run exited
    SENTINEL_PASS with one present — the signature of a filter that matched no
    test names. A runner given a name filter that matches nothing skips every
    test and exits 0 on both red and green (#7524: an `it.each` template title
    like "preserves $status …" renders per-case at run time, so no filter string
    equals it). Returns None when red exited anything else or the command
    carries no filter.

    A diagnostic fact, not a verdict: the outcome stays whatever verify_outcome
    computes (a red that exited 0 is not-verified regardless), and it reads only
    trusted inputs — the pre-committed test_cmd and the host-observed exit —
    never the untrusted output tails. commit_outcomes records it as a dedicated
    finding and the cockpit story names it."""
    if host.get("red_exit") != SENTINEL_PASS:
        return None
    cmd = blind.get("test_cmd")
    if not isinstance(cmd, str):
        return None
    m = _NAME_FILTER_RE.search(cmd)
    return m.group(1) if m else None


# Runner flags that rebase the root the runner resolves path filters against:
# vitest/jest `--config` (root becomes the config file's directory) and vitest
# `--root` (root becomes the value itself).
_REBASE_FLAGS = ("--config", "--root")

# The name-filter flags of _NAME_FILTER_RE as tokens, for reading their values.
_NAME_FILTER_FLAGS = ("-t", "-g", "--testNamePattern", "--test-name-pattern",
                      "--grep")


def _positional(toks: list[str]) -> list[str]:
    """The command's positional tokens: not a dash flag, and not the token
    directly after a valueless dash flag (that token is the flag's argument,
    so a name-filter value never reads as a path)."""
    consumed = {i + 1 for i, t in enumerate(toks[:-1])
                if t.startswith("-") and "=" not in t}
    return [t for i, t in enumerate(toks)
            if i not in consumed and not t.startswith("-")]


def _name_filter_values(toks: list[str]) -> list[str]:
    """The values of every name-filter flag in the command, whether attached
    by space or by `=`."""
    vals: list[str] = []
    for i, t in enumerate(toks):
        if t in _NAME_FILTER_FLAGS and i + 1 < len(toks):
            vals.append(toks[i + 1])
        elif t.startswith(tuple(f"{f}=" for f in _NAME_FILTER_FLAGS)):
            vals.append(t.split("=", 1)[1])
    return vals


def repro_targets_pr_test(repro_cmd: str, diff_text: str) -> str | None:
    """The token in the committed repro_command that names a test the diff
    itself introduces — a changed test path (matched whole or as a "/"-bounded
    suffix, so a workspace-rooted spelling still matches), or a name-filter
    value that appears in the diff's added test lines. The repro phase runs
    the pinned base with no patch applied, so such a target does not exist
    there and the run's exit code carries no signal (#9041: the command's path
    was a file the diff adds; #7936: the -t value was a test title the diff
    adds). Returns None when the command targets nothing the diff introduces.

    Read pre-flight from the committed command and the PR's own diff — the
    run's output plays no part — and fail-safe by construction: a match only
    ever skips a run (verify_pr records skipped_reason
    "repro-targets-pr-test" and spends no sandbox time), so diff text can at
    worst suppress its own PR's corroborating evidence."""
    try:
        toks = shlex.split(repro_cmd)
    except ValueError:
        return None
    changed_tests = [p for p in diffpaths.changed_paths(diff_text)
                     if diffpaths.is_test_path(p)]
    for t in _positional(toks):
        cand = diffpaths.normalize_path(t)
        if cand and any(p == cand or p.endswith("/" + cand)
                        for p in changed_tests):
            return t
    added_test_lines = [
        line[1:] for line in
        diffpaths.filter_diff(diff_text, diffpaths.is_test_path).splitlines()
        if line.startswith("+") and not line.startswith("+++")]
    for val in _name_filter_values(toks):
        if val and any(val in line for line in added_test_lines):
            return val
    return None


def vacuous_path_filter(blind: dict, repro: dict) -> str | None:
    """The path filter in the committed repro_command that repeats the root a
    `--config`/`--root` flag in the same command rebases to, when the repro ran
    and exited SENTINEL_TEST_FAIL — the signature of a filter that matched no
    files (#9041: `--config server/vitest.config.ts` rebases vitest's root to
    server/, so the filter `server/src/...` resolves to server/server/src/...,
    the runner finds no test files and exits nonzero, and the host exit reads
    as a reproduction). Returns None when the repro never ran, exited anything
    else, or the command carries no such conflict.

    A diagnostic fact, not a verdict: the repro is corroborating evidence the
    outcome never turns on, and this reads only trusted inputs — the
    pre-committed repro_command and the host-observed exit — never the
    untrusted output tails. commit_outcomes records it as a dedicated finding
    and the cockpit story names it."""
    if not repro.get("ran") or repro.get("exit_code") != SENTINEL_TEST_FAIL:
        return None
    cmd = blind.get("repro_command")
    if not isinstance(cmd, str):
        return None
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    root: str | None = None
    for i, t in enumerate(toks):
        if t in _REBASE_FLAGS and i + 1 < len(toks):
            flag, val = t, toks[i + 1]
        elif t.startswith(tuple(f"{f}=" for f in _REBASE_FLAGS)):
            flag, val = t.split("=", 1)
        else:
            continue
        d = posixpath.dirname(val) if flag == "--config" else val
        d = posixpath.normpath(d) if d else "."
        if root is None and d != ".":
            root = d
    if root is None:
        return None
    prefix = root + "/"
    for t in _positional(toks):
        if posixpath.normpath(t).startswith(prefix):
            return t
    return None


def _contained_dirty_green(host: dict, green_key: str) -> bool:
    """True iff the failing set parsed from the green leg under `green_key` is
    contamination the red run already carried: non-empty, a PROPER subset of
    red's failing set (so at least one red failure flipped to passing), and no
    member of it is a test the PR's own test hunks mention (`failing_in_diff`
    — a green-failing test the diff introduces or retitles is the PR's test
    still failing, never contamination). The parsed sets come from the runs'
    own failed-tests reports (verify_driver.parse_failed_tests), so a fact
    that is missing, unparsable, or malformed reads False and the exemption
    simply never applies.

    Requires a host-observed red of exactly SENTINEL_TEST_FAIL: containment is
    a statement about which red failures the green leg retained, meaningless
    without one."""
    if host.get("red_exit") != SENTINEL_TEST_FAIL:
        return False
    red = host.get("red_failing")
    green = host.get(green_key)
    in_diff = host.get("failing_in_diff")
    if (not isinstance(red, list) or not isinstance(green, list)
            or not isinstance(in_diff, list) or not green):
        return False
    red_set = {str(t) for t in red}
    green_set = {str(t) for t in green}
    return green_set < red_set and not green_set & {str(t) for t in in_diff}


def green_accepted(host: dict) -> bool:
    """Whether the first green run counts as passing: an exit of SENTINEL_PASS,
    or an exit of SENTINEL_TEST_FAIL whose failing set is contained
    contamination (#3718, #3368: a test in the same file that fails identically
    with and without the fix — a missing sandbox binary, a pre-existing failure
    on the pinned base — poisons the whole-file exit code while the target test
    itself flips red->green). The ONE definition of an accepted green;
    verify_outcome and verify_run_errored read it, and the driver uses it to
    decide whether a dirty first run earns the confirm and regress legs."""
    if host.get("green_exit") == SENTINEL_PASS:
        return True
    return (host.get("green_exit") == SENTINEL_TEST_FAIL
            and _contained_dirty_green(host, "green_failing"))


def green_confirm_accepted(host: dict) -> bool:
    """green_accepted for the confirm re-run: SENTINEL_PASS (which includes a
    contaminant that stopped failing), or a contained SENTINEL_TEST_FAIL judged
    against the same first-run red set. A confirm failing on anything red never
    failed on is a disagreement between the runs, and the outcome escalates it
    exactly like a flaky green."""
    if host.get("green_exit_confirm") == SENTINEL_PASS:
        return True
    return (host.get("green_exit_confirm") == SENTINEL_TEST_FAIL
            and _contained_dirty_green(host, "green_failing_confirm"))


def contained_green_failures(host: dict) -> list[str]:
    """The contaminating test ids the containment exemption accepted, across
    both green legs, sorted — [] when the exemption applied to neither.
    commit_outcomes records them as a dedicated `dirty-green` finding,
    verify_signals_incomplete names them as partial evidence, and the cockpit
    story surfaces them."""
    out: set[str] = set()
    if (host.get("green_exit") == SENTINEL_TEST_FAIL
            and _contained_dirty_green(host, "green_failing")):
        out.update(str(t) for t in host.get("green_failing") or [])
        # The confirm leg's exemption exists only downstream of an accepted
        # first leg, so its names count only alongside the first leg's.
        if (host.get("green_exit_confirm") == SENTINEL_TEST_FAIL
                and _contained_dirty_green(host, "green_failing_confirm")):
            out.update(str(t) for t in host.get("green_failing_confirm") or [])
    return sorted(out)


def verify_run_errored(blind: dict, host: dict, *, regress: dict | None) -> bool:
    """True iff a PR's committed signals record a run that errored: the host saw a
    phase exit outside its sentinel set (a killed container, an OOM, a timeout, an
    image fault), or a clean red->green's confirm re-run or regress leg is
    incomplete — missing, not run, a non-sentinel exit, or a first failure whose
    confirming run never concluded. Nothing follows from such a run however the
    judge rates the output, because the exit code it reported carries no meaning.

    A blind verdict that settles the outcome by itself — no test to run, or a live
    agent required — asked for no sandbox, so its unrecorded exit codes are not a
    failed run. So does a diff with no test hunks to apply for red (host's
    no_test_hunks) — apply-check ran, but red/green never did, by policy rather
    than failure.

    The ONE definition of the VERIFY hold: verify_outcome returns None on it, so
    the PR is not settled and a re-queue runs it again."""
    if blind.get("requires_live_agent") or not blind.get("test_cmd"):
        return False
    if host.get("no_test_hunks"):
        return False
    apply_exit = host.get("apply_exit")
    if apply_exit == SENTINEL_PATCH_CONFLICT:
        return False   # host-authoritative needs-rebase — a result, not an error
    if apply_exit != SENTINEL_PASS:
        return True
    accepted = (SENTINEL_PASS, SENTINEL_TEST_FAIL)
    if host.get("red_exit") not in accepted or host.get("green_exit") not in accepted:
        return True
    if not (host.get("red_exit") == SENTINEL_TEST_FAIL and green_accepted(host)):
        return False
    # An accepted red->green (green passed, or its failures are contained
    # contamination) must carry a completed confirm re-run — the second
    # red+green (fresh containers) that rules out a flaky reproduction. Missing
    # or non-sentinel confirm exits are an incomplete run and hold; a confirm
    # that DISAGREES with the first run is a nondeterministic result the outcome
    # escalates, not an error.
    if (host.get("red_exit_confirm") not in accepted
            or host.get("green_exit_confirm") not in accepted):
        return True
    if not (host.get("red_exit_confirm") == SENTINEL_TEST_FAIL
            and green_confirm_accepted(host)):
        return False
    # An accepted red->green must carry a completed regress leg. The full-suite
    # run is part of the run itself, so a record without one — or with a
    # non-sentinel exit, or a first failure whose confirming run never
    # concluded — is an incomplete run, and an incomplete run holds
    # (fail-closed) rather than passing unregression-checked. Two skips are
    # accepted: `no-suite-config` (the pin declared the repository has no
    # full-suite contract) and a `lane-`-prefixed reason (a merge-gate lane
    # failed first).
    if regress is None:
        return True
    if not regress.get("ran"):
        reason = str(regress.get("skipped_reason") or "")
        # Two deliberate skips are complete results, not failures: a pin with
        # no suite contract, and a regress leg not reached because a merge-gate
        # lane failed first (the lane's own record carries the verdict).
        return reason != "no-suite-config" and not reason.startswith("lane-")
    if regress.get("exit_first") not in accepted:
        return True
    if (regress.get("exit_first") == SENTINEL_TEST_FAIL
            and regress.get("exit_confirm") not in accepted):
        return True
    return False


def _authored_lane_outcome(authored: dict | None, judge: dict | None, *,
                           regress: dict | None,
                           lanes: dict | None = None) -> str:
    """The no-test lane's outcome, from the agent-authored test's committed
    record and host-observed exits.

    `agent-verified` is corroborating evidence for the operator, never a gate
    input: merge_allowed still requires an author-shipped verified-fix. Every
    non-clean lane result — no authored test, a validation skip, an infra
    error, a red that never failed, a flaky confirm, a wrong-reason or
    low-confidence red — settles to unverifiable-no-test with the attempt on
    record. The lane never holds (a broken run is re-queued by the operator,
    not automatically) and never escalates: an agent-authored artifact moves
    no policy state. A confirmed full-suite regression is host-observed
    evidence about the PR itself, so it keeps `regressed`.

    Merge-gate lanes are host policy (the profile's commands over the patched
    tree), not agent artifacts, so a lane result moves the outcome here
    exactly as on the author-shipped path."""
    if not authored or not authored.get("test_cmd"):
        return "unverifiable-no-test"
    if not (authored.get("red_exit") == SENTINEL_TEST_FAIL
            and authored.get("green_exit") == SENTINEL_PASS):
        return "unverifiable-no-test"
    if not (authored.get("red_exit_confirm") == SENTINEL_TEST_FAIL
            and authored.get("green_exit_confirm") == SENTINEL_PASS):
        return "unverifiable-no-test"
    match = (judge or {}).get("red_reason_match") or {}
    if match.get("matches") is not True or match.get("confidence") == "low":
        return "unverifiable-no-test"
    if regress is not None and regress.get("confirmed"):
        return "regressed"
    lane_verdict = _lanes_verdict(lanes)
    if lane_verdict is not None:
        return lane_verdict
    return "agent-verified"


def verify_outcome(blind: dict, host: dict, judge: dict | None, *,
                   regress: dict | None, authored: dict | None = None,
                   lanes: dict | None = None) -> str | None:
    """The ONE VERIFY outcome policy, computed from the four signals.

    `blind` is the adequacy verdict committed BEFORE any run; `host` is the
    exit codes the trusted driver observed, plus the parsed failing sets the
    driver stores when both legs exit SENTINEL_TEST_FAIL (green_accepted /
    green_confirm_accepted own how those relax the green bar). `judge` is the
    post-run red-reason rating (`red_reason_match`), plus Signal 4's
    repro-reason rating (`repro_reason_match`) when a repro ran. `regress` is
    the full-suite signal:
    a confirmed regression (both runs exited 20) demotes a would-be
    `verified-fix` to `regressed`; escalate keeps primacy. Returns None to
    HOLD, writing no section: either the run errored (verify_run_errored — the
    PR re-runs), or the judge's red-reason rating is unusable.

    `repro_reason_match` is carried in `judge` but intentionally not consulted
    below: Signal 4 (the agent's independent repro) is corroborating evidence,
    not a gate — a repro that failed for the wrong reason (a timeout, an
    import error) is recorded but does not change the outcome.

    The judgment agent supplies only `judge` and never an outcome, so the
    escalate rule below is unreachable from a prompt: a blind verdict of
    unfaithful against a clean red->green escalates no matter what the agent
    says about the red reason.

    `authored` is the agent-authored test lane's committed record for a PR
    whose diff ships no test; `_authored_lane_outcome` owns that branch.

    `lanes` is the per-lane merge-gate record (compile/build commands over the
    patched tree); `_lanes_verdict` owns its mapping — a failed lane is
    `regressed`, an infra-broken lane `escalate`, and a record with no lanes is
    gated only by the signals above."""
    if blind.get("requires_live_agent"):
        return "unverifiable-needs-live-agent"
    if not blind.get("test_cmd"):
        if host.get("apply_exit") == SENTINEL_PATCH_CONFLICT:
            return "needs-rebase"
        return _authored_lane_outcome(authored, judge, regress=regress, lanes=lanes)
    if host.get("no_test_hunks"):
        # has_test claimed a PR-authored test, but the diff carried no test hunk
        # to apply for red — no legitimate red was ever possible here.
        return "unverifiable-no-test"

    if host.get("apply_exit") == SENTINEL_PATCH_CONFLICT:
        return "needs-rebase"
    if verify_run_errored(blind, host, regress=regress):
        return None
    if not (host.get("red_exit") == SENTINEL_TEST_FAIL and green_accepted(host)):
        return "not-verified"
    # verify_run_errored has cleared the confirm re-run as complete and sentinel;
    # a confirm that disagrees with the accepted first run — a red that passed,
    # a green that failed outside containment — is a flaky (nondeterministic)
    # reproduction: escalate to a human, never a silent verified-fix.
    if not (host.get("red_exit_confirm") == SENTINEL_TEST_FAIL
            and green_confirm_accepted(host)):
        return "escalate"

    if blind.get("faithful") is False:
        return "escalate"
    if judge is None:
        return None
    match = judge.get("red_reason_match") or {}
    matches = match.get("matches")
    # A rating is usable only when `matches` is an actual bool; anything else holds.
    if not isinstance(matches, bool):
        return None
    if matches is False:
        return "not-verified"
    if match.get("confidence") == "low":
        return "escalate"
    if regress is not None and regress.get("confirmed"):
        return "regressed"
    lane_verdict = _lanes_verdict(lanes)
    if lane_verdict is not None:
        return lane_verdict
    return "verified-fix"


def verify_signals_incomplete(pr: Pr) -> str | None:
    """The reason this PR's verify evidence is partial — an attempted signal that
    did not corroborate — or None when every attempted signal did. A deployment
    that configures merge-gate lanes also requires them on the record: a
    verification that never ran them is partial evidence, named first, and a
    record whose lanes did run but did not all pass is inconsistent with its
    own evidence regardless of the stored outcome, named next.

    A verified outcome whose green legs were accepted through the dirty-green
    containment exemption (contained_green_failures) is partial evidence too:
    the fix flipped its target test, but the file's suite did not come out
    clean, so the operator merges knowing which failures were waved through.

    Signal 4 (the independent repro) is the one optional signal: a blind pass
    that authored no repro_command attempted nothing, so nothing is incomplete.
    When one WAS authored it must have run and the judge must have rated it
    matching; a repro that never ran, ran unrated, or was rated not-matching is
    partial evidence. merge_allowed refuses to auto-recommend on it; the cockpit
    names it on the human path and in the verify panel. Operator decision
    2026-07-16: a verified-fix whose repro never executed (#7524, a harness path
    defect) must not present as full-confidence evidence at merge time.

    Reads the record as it stands — currency is the callers' concern."""
    signals = pr.verify_signals
    required = configured_lanes()
    if required:
        lanes = signals.get("lanes") or {}
        missing = [name for name in required if name not in lanes]
        if missing:
            return (f"the {', '.join(missing)} lane(s) this deployment requires "
                    f"were not part of this verification — re-verify to complete "
                    f"the evidence")
        if _lanes_verdict(lanes) is not None:
            return ("a recorded merge-gate lane did not pass — the stored "
                    "outcome is inconsistent with its own lane evidence; "
                    "re-verify")
    contaminated = contained_green_failures(signals.get("red_green") or {})
    if contaminated:
        return ("the green runs exited failing, accepted because every failure "
                "is a test that also failed red (contamination: "
                f"{'; '.join(contaminated[:3])}) — corroborating evidence, not "
                "a fully clean green")
    blind = signals.get("blind_adequacy") or {}
    if not blind.get("repro_command"):
        return None
    repro = signals.get("independent_repro") or {}
    if not repro.get("ran"):
        return "an independent repro was authored but never ran"
    rating = signals.get("repro_reason_match") or {}
    matches = rating.get("matches")
    if matches is True:
        return None
    if matches is False:
        return ("the independent repro did not corroborate — it failed for a "
                "reason that does not match the prediction (see findings)")
    return "the independent repro ran but was never rated by the judge"


def verify_disposition(pr: Pr) -> tuple[str, str] | None:
    """The disposition a current verify outcome forces on a merge candidate, with
    rationale — or None when it does not force one (verified-fix, agent-verified,
    the unverifiable outcomes, a null outcome, missing, or stale).

    The SINGLE source of truth for the verify->disposition consequence, read at
    disposition-derivation time (merge_demotion), so a re-run that clears the
    outcome clears the route with it. Mirrors security_disposition: the
    RED-equivalents (escalate, deps-touched) route to needs-human; the
    evidence-backed asks route to request-changes; a logged operator override on
    an escalate clears it (returns None), the same way a logged security
    override clears a YELLOW."""
    if not pr.section("verify") or not is_current(pr, "verify") or pr.verify_override:
        return None
    outcome = pr.verify_outcome
    if outcome == "escalate":
        lane = _lane_escalate_cause(pr.verify_signals)
        if lane is not None:
            return ("needs-human",
                    f"Dynamic verification escalated — the {lane} merge-gate lane "
                    "could not run to a verdict (infrastructure exit, not "
                    "evidence about the PR). A human must decide or re-queue.")
        return ("needs-human",
                "Dynamic verification escalated — the blind adequacy verdict says this "
                "test does not faithfully reproduce the claimed defect, yet it goes "
                "clean red-green. A human must judge.")
    if outcome == "deps-touched":
        return ("needs-human",
                "Dynamic verification refused — this PR changes dependency manifests or "
                "lockfiles, which are never installed from a PR. Review by hand.")
    if outcome == "needs-rebase":
        return ("request-changes",
                "Dynamic verification could not apply this PR to the pinned main. "
                "Rebase onto main.")
    if outcome == "not-verified":
        return ("request-changes",
                "Dynamic verification did not confirm the fix — the test did not "
                "reproduce the defect on pinned main, or did not pass after the fix.")
    if outcome == "regressed":
        lane = _lane_regressed_cause(pr.verify_signals)
        if lane is not None:
            return ("request-changes",
                    f"Dynamic verification found the merged tree fails the {lane} "
                    "merge-gate lane with this PR applied — the command and "
                    "error are in verify.findings/signals. Update the PR "
                    "against current main.")
        return ("request-changes",
                "Dynamic verification found a regression — with this PR applied, "
                "tests that pass on the pinned main fail, confirmed on a second "
                "run. The failing tests are in verify.findings. Update the PR "
                "against current main.")
    return None


def forced_disposition(pr: Pr) -> tuple[str, str] | None:
    """The disposition this PR's current facts force on a merge pick, with
    rationale — or None when none of them force one.

    Composes the two single sources of a disposition consequence,
    security_disposition and verify_disposition. When both force a route the
    most-blocking wins by DISPOSITION_PRECEDENCE, and an equal-ranked pair breaks
    to security: it is the higher-stakes finding, and breaking it the same way
    every time keeps a re-run stable."""
    routes = [r for r in (security_disposition(pr), verify_disposition(pr)) if r is not None]
    if not routes:
        return None
    return min(routes, key=lambda r: _DISPOSITION_RANK[r[0]])


def bar_asks(reasons: list[str]) -> list[str]:
    """Turn a PR's clean-gate failures into actionable author asks."""
    policy = review_policy.active()
    label = policy.label.lower()
    asks = []
    for r in reasons:
        if policy.required and label and r.startswith(label):
            asks.append(f"{policy.label} review is {r.split()[-1]} — address its review "
                        f"comments so it reaches {policy.threshold}/{policy.score_max} "
                        "(our merge bar).")
        elif r.startswith("ci"):
            asks.append("CI is not green — fix the failing/unknown checks.")
        elif "conflict" in r:
            asks.append(f"Rebase onto current {settings.default_branch()} and resolve "
                        f"the merge conflicts.")
        elif r.startswith("drift"):
            asks.append(f"Resolve the drift from {settings.default_branch()} (rebase "
                        f"onto current {settings.default_branch()}).")
        elif r == "draft":
            asks.append("Mark the PR ready for review — it is currently a draft — "
                        "before it can merge.")
        elif r.startswith("secret-leak"):
            asks.append("Remove the hardcoded credential from the diff and load it "
                        "from an environment variable / secret store instead, then "
                        "rotate the exposed key at its provider and force-push — it is "
                        "already public in this PR's history, so treat it as compromised.")
    return asks or ["Address the outstanding quality gates before this can merge."]


# Staleness never demotes: an unmeasured gate proves nothing, and the fail-closed
# merge gates (merge_allowed / merge_eligibility via pr_clean) refuse stale facts
# on their own.
_STALE_BAR_REASONS = frozenset({"signals stale or missing", "drift stale or missing"})


def merge_demotion(pr: Pr) -> tuple[str, str | None, list[str]] | None:
    """The route a stored `merge` pick reads as, given every current fact:
    (disposition, rationale, extra_asks) — or None when nothing blocks. The
    rationale is None when the analyze rationale stands (a quality-gate block
    speaks through the asks); a forced security/verify route supplies its own.

    Composes the fact consequences (forced_disposition: security + verify,
    most-blocking wins) with the quality-gate merge bar (pr_clean, staleness
    reasons excluded). A needs-human route carries no asks — the decision is the
    operator's, not the author's.

    The SINGLE source of the merge-pick consequence, read by Pr.disposition /
    Pr.rationale / Pr.asks — derived on read, never stored, so a re-run, a
    logged override, or a signal refresh is reflected immediately."""
    forced = forced_disposition(pr)
    _, reasons = pr_clean(pr)
    bar = [r for r in reasons if r not in _STALE_BAR_REASONS]
    if forced is None and not bar:
        return None
    if forced is not None and forced[0] == "needs-human":
        return (forced[0], forced[1], [])
    return ("request-changes",
            forced[1] if forced is not None else None,
            bar_asks(bar) if bar else [])


# ---------------------------------------------------------------------------
# Derived cluster state (the board chip) — computed on read, never stored.
# ---------------------------------------------------------------------------
def cluster_state(cluster: Cluster, prs: dict[int, Pr], today: str | None = None) -> str:
    members = [prs[n] for n in cluster.prs if n in prs]
    active = [r for r in members if r.state == "open"]
    if members and not active:
        return "done"
    outcome = cluster.outcome
    if not outcome:
        return "needs-analysis"
    # a moved head on any actively-routed PR sends the cluster back to analysis
    if any(r.section("analysis") and not is_current(r, "analysis") for r in active):
        return "needs-analysis"
    if outcome in ("awaiting-authors", "needs-first-party-work", "blocked-on-decision"):
        return outcome
    if outcome == "close-out":
        return "ready"
    # merge-ready: every merge-routed PR needs a current GREEN (or override) and
    # a current verified-fix
    merge_routed = [r for r in active if r.disposition == "merge"]
    # A blocking fact — a non-GREEN security verdict, a verify outcome — re-routes a
    # merge pick through forced_disposition, which leaves the stored plan proposing
    # a merge that no member carries. A member still owed work names the state then;
    # a cluster with only closes left is the operator's to execute.
    if not merge_routed:
        if any(r.disposition == "needs-human" for r in active):
            return "blocked-on-decision"
        if any(r.disposition == "request-changes" for r in active):
            return "awaiting-authors"
    for r in merge_routed:
        ok, _ = merge_allowed(r, today)
        if not ok:
            return "security-pending"
    return "ready"
