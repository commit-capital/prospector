"""The ONE issue-side policy module: close-as-dup, derived cluster state, and the
issue-fix lane's gates.

close_dup_allowed is the pipeline's static auto-recommend (drives the dup
worklist); close_dup_eligibility adds live upstream checks that the duplicate and
is still open, and is the app/executor's pre-write gate — the issue-side analog of
gates.py's merge_allowed vs merge_eligibility.
issue_cluster_state is the derived board chip, computed on read, never stored.
reproduction_outcome names a reproduction attempt's result from the host's red
exits and the judge's ratings; fix_patch_regate and fix_proof_bar hold an
agent-authored fix to what it may touch and to host-observed proof plus two
refuting reviews; fix_coverage reads the scope reviewer's account of each defect
a report names into whether a proven fix covers the whole report, and
review_comments reads every review into what the fixer must answer.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, TypedDict

from issue_triage.issue_freshness import is_current
from pipeline import author_fix, diffpaths, gates, risktier, threats

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue, IssueCluster

def close_dup_allowed(issue: Issue, cluster: IssueCluster | None,
                      today: str | None = None) -> tuple[bool, str]:
    """Static gate (pipeline auto-recommend): an open, confirmed, fresh, curated
    duplicate pointing at a canonical other than itself."""
    if issue.state is not None and issue.state != "open":
        return False, f"issue is already {issue.state}"
    if cluster is not None and cluster.needs_review:
        return False, "cluster flagged needs-review — curate (split) before closing"
    if not is_current(issue, "analysis"):
        return False, "analysis missing or stale — re-run ANALYZE"
    if issue.disposition != "close-dup":
        return False, f"disposition is {issue.disposition}, not close-dup"
    canon = issue.canonical
    if not canon:
        return False, "no canonical issue recorded"
    if canon == issue.number:
        return False, "canonical is the issue itself"
    if cluster is None or not (cluster.curation or {}).get("confirmed"):
        return False, "cluster duplicates not confirmed (curation pending)"
    return True, "confirmed duplicate — ready to close"


def close_dup_eligibility(issue: Issue, cluster: IssueCluster | None,
                          live_state: Callable[[int], str | None] | None = None,
                          today: str | None = None,
                          ) -> tuple[bool, str]:
    """Executor's pre-write gate: close_dup_allowed plus checks that the duplicate
    itself is still open. The canonical's current state and resolution do not
    affect whether the duplicate relationship is valid.

    `live_state` fetches an issue's current upstream state ("open"/"closed", or
    None when GitHub is unreachable). It is consulted only after the static checks
    pass, and a fetched value overrides the store's snapshot. A failed fetch falls
    back to the store."""
    ok, reason = close_dup_allowed(issue, cluster, today)
    if not ok:
        return ok, reason
    own = (live_state(issue.number) if live_state else None) or issue.state
    if own is not None and own != "open":
        return False, f"issue #{issue.number} is already {own} upstream — no action needed"
    return True, "confirmed duplicate — ready to close"


def close_fixed_eligibility(issue: Issue, fixed_by_state: str | None,
                            issue_live_state: str | None = None) -> tuple[bool, str]:
    """Executor's pre-write gate for closing an issue as fixed by a merged PR: the
    referenced PR must be currently `merged` and the issue still open. Deterministic
    — a merged fixer stands on its own, with no cluster-curation dependency (the
    app only offers this on a confirmed-cluster card). Both the PR's `merged`
    state and `issue_live_state` are resolved live by the caller; a fetched issue
    state overrides the store's snapshot, falling back to it when the fetch returns
    None (fail-open — a transient read failure must not block the gate)."""
    if fixed_by_state != "merged":
        return False, f"fixing PR is {fixed_by_state or 'unresolved'}, not merged"
    own = issue_live_state or issue.state
    if own != "open":
        return False, f"issue is {own} upstream — no action needed"
    return True, "fixing PR merged, issue open — ready to close as fixed"


def issue_cluster_state(cluster: IssueCluster, issues: dict[int, Issue],
                        today: str | None = None) -> str:
    """Derived board chip: needs-curation | dup-ready | awaiting-repro | no-dups | done."""
    members = [issues[n] for n in cluster.members if n in issues]
    active = [i for i in members if i.state == "open"]
    if members and not active:
        return "done"
    if cluster.needs_review or not (cluster.curation or {}).get("confirmed"):
        return "needs-curation"
    pending_dups = [i for i in active if i.disposition == "close-dup"
                    and (i.resolution or {}).get("status") in (None, "pending")]
    if pending_dups:
        return "dup-ready"
    if any(i.disposition == "request-repro" for i in active):
        return "awaiting-repro"
    if len(active) <= 1:
        return "no-dups"
    return "done"


REPRODUCTION_OUTCOMES = ("reproduced", "not-reproduced", "unwritable", "wrong-symptom",
                         "not-a-defect")
_CONFIDENCES = ("high", "medium", "low")


def _rating(judge: dict | None, key: str, flag: str) -> tuple[bool, bool] | None:
    """(the judge's answer, whether it holds it above low confidence), or None
    when the rating is missing or malformed."""
    part = (judge or {}).get(key)
    if not isinstance(part, dict) or not isinstance(part.get(flag), bool):
        return None
    if part.get("confidence") not in _CONFIDENCES:
        return None
    return part[flag], part["confidence"] != "low"


def reproduction_outcome(red: dict, judge: dict | None, *, gave_up: bool,
                         invalid: str | None) -> str | None:
    """What a reproduction attempt amounts to, from the host's two red exits and
    the judge's ratings. None is a machine fault: a sandbox exit that is not a
    test verdict, or a judge that gave no usable rating. The judge rates; this
    decides — a rating held with low confidence reads as a no."""
    if gave_up or invalid:
        return "unwritable"
    first, confirm = red.get("exit"), red.get("exit_confirm")
    if first == gates.SENTINEL_PASS or confirm == gates.SENTINEL_PASS:
        return "not-reproduced"
    if first != gates.SENTINEL_TEST_FAIL or confirm != gates.SENTINEL_TEST_FAIL:
        return None
    symptom = _rating(judge, "symptom_match", "matches")
    defect = _rating(judge, "defect", "is_defect")
    if symptom is None or defect is None:
        return None
    if symptom != (True, True):
        return "wrong-symptom"
    if defect != (True, True):
        return "not-a-defect"
    return "reproduced"


REVIEW_LENSES = ("root-cause", "scope-safety", "design")
FIX_PATCH_MAX_CHARS = 200_000

# Diff lines a fix may not carry, with what each one is.
_REFUSED_DIFF_LINES = (
    ("Binary files ", "a binary file"), ("GIT binary patch", "a binary file"),
    ("new file mode 120000", "a symlink"), ("new file mode 160000", "a submodule"),
    ("old mode ", "a file-mode change"), ("new file mode 100755", "an executable file"),
)


def changed_line_count(patch: str) -> int:
    return sum(1 for line in patch.splitlines()
              if (line.startswith("+") and not line.startswith("+++"))
              or (line.startswith("-") and not line.startswith("---")))


def fix_patch_regate(patch: str, *, changes: list[author_fix.Change],
                     max_lines: int) -> tuple[bool, str]:
    """Whether an agent's finished fix may go on to proof: (ok, reason). The
    patch is held to what the agent reported — the test files it reported
    aside, which belong to its reproduction — and to the paths, size, and kinds
    of change an issue-driven fix may make. Fail-closed."""
    if not patch.startswith("diff "):
        return False, "the fix is empty or not a diff"
    if len(patch) > FIX_PATCH_MAX_CHARS:
        return False, f"the fix is over {FIX_PATCH_MAX_CHARS} characters"
    for line in patch.splitlines():
        for marker, what in _REFUSED_DIFF_LINES:
            if line.startswith(marker):
                return False, f"the fix carries {what}"
    paths = diffpaths.changed_paths(patch)
    if not paths:
        return False, "the fix names no path"
    tests = [p for p in paths if diffpaths.is_test_path(p)]
    if tests:
        return False, f"the fix touches test files: {', '.join(tests)}"
    try:
        author_fix.assert_disclosed(
            [c for c in changes if not diffpaths.is_test_path(c["path"])], paths)
    except ValueError as e:
        return False, str(e)
    if gates.deps_touched(paths):
        return False, "the fix changes a dependency manifest"
    withheld = gates.fix_withheld_paths(paths)
    if withheld:
        return False, f"the fix touches withheld paths: {', '.join(withheld)}"
    tier = risktier.pr_tier(paths)
    if tier is None or tier == 0:
        return False, "the fix touches a tier-0 path"
    scan = threats.scan_diff(patch)
    if scan["verdict"] == "malicious":
        return False, f"the fix matches a threat signature: {', '.join(scan['signatures'])}"
    count = changed_line_count(patch)
    if count > max_lines:
        return False, f"the fix changes {count} lines (limit {max_lines})"
    return True, "clean"


def _twice(legs: dict | None, want: int) -> bool:
    return bool(legs) and legs.get("exit") == want and legs.get("exit_confirm") == want


def sandbox_fault(record: dict | None) -> str | None:
    """The machine fault a host check's sandbox record (`prove.run_command`)
    carries, or None: an `error` that is not the base's own failure (an
    `error_kind`, which each caller reads on its own) — the sandbox unable to
    apply the patch for a reason that is not the patch's, a phase that exited
    before its command concluded, a docker error. A `refused` patch is the
    patch's, not the machine's."""
    if not record or record.get("error_kind") or not record.get("error"):
        return None
    return str(record["error"])


def checks_fault(checks: list[dict]) -> str | None:
    """The machine fault behind an agent's answer, or None: every sandbox run
    the agent made (`check_records.CheckRecord`) that the check tool did not
    refuse failed before its command concluded, and at least one did — so the
    agent never saw a verdict, and its give-up or its unchecked patch is the
    machine's, not a judgment on the issue."""
    runs = [c for c in checks if c.get("error_kind") != "refused"]
    if not runs or any(c.get("error_kind") != "infrastructure" for c in runs):
        return None
    return f"every sandbox check the agent ran failed: {runs[-1].get('error')}"


def give_up_ending(checks: list[dict], ending: str, detail: str) -> tuple[str, str]:
    """The ending of an agent's give-up: "sandbox" when its checks show the
    machine kept it from any verdict (checks_fault), else `ending`."""
    fault = checks_fault(checks)
    return ("sandbox", fault) if fault else (ending, detail)


def related_tests_outcome(related: dict) -> str:
    """How a fix's related existing tests came out, in words. A failure the
    base shares is waived by fix_proof_bar, and says it proves nothing."""
    run = related.get("run") or {}
    if run.get("exit") == gates.SENTINEL_PASS:
        return "passed"
    if related.get("base_fails"):
        return ("failed, and fail on the base too, so they cannot show whether the "
                "change breaks them")
    return f"failed (exit {run.get('exit')})"


def fix_proof_bar(result: dict) -> tuple[str | None, str]:
    """(None, …) when a fix is proven and reviewed, else the ending it falls
    short at — "fix-unproven" for the host's proof, "fix-rejected" for a
    reviewer, "sandbox" when a check's sandbox could not run (sandbox_fault),
    which is the machine's condition, never a verdict — with the reason. The
    proof is the reproduction red on the base and green with the fix, the
    preservation tests green with the fix, the compile lane, the lint
    (gates.lint_block), and the related tests. Only an explicit `safe` from
    every reviewer's latest answer (latest_reviews), with every lens in
    REVIEW_LENSES answered, passes."""
    proof = result.get("proof") or {}
    if not _twice(proof.get("red"), gates.SENTINEL_TEST_FAIL):
        return "fix-unproven", "the reproduction is not red twice on the base"
    if not _twice(proof.get("green"), gates.SENTINEL_PASS):
        return "fix-unproven", "the reproduction is not green twice with the fix applied"
    if not _twice(proof.get("preserve"), gates.SENTINEL_PASS):
        return "fix-unproven", ("the preservation tests do not pass twice with the fix "
                                "applied: it changes behavior they pin")
    for name, record in (("compile", proof.get("compile")), ("lint", proof.get("lint")),
                         ("related-tests", (proof.get("related_tests") or {}).get("run"))):
        fault = sandbox_fault(record)
        if fault:
            return "sandbox", f"the {name} sandbox could not run: {fault}"
    compiled = proof.get("compile")
    if compiled is not None:
        not_run = compiled.get("refused") or compiled.get("error")
        # A failure the unfixed tree shares is the tree's, not the fix's.
        if not_run or (compiled.get("exit") != gates.SENTINEL_PASS
                       and not compiled.get("tree_fails")):
            return "fix-unproven", ("the compile lane did not pass: "
                                    + str(not_run or compiled.get("error_excerpt")
                                          or f"exit {compiled.get('exit')}"))
    lint = gates.lint_block(proof.get("lint"), "the fix")
    if lint:
        return "fix-unproven", lint
    related = proof.get("related_tests")
    if related and not related.get("base_fails"):
        block = gates.related_tests_block(related, "the fix")
        if block:
            return "fix-unproven", block
    suite = proof.get("suite")
    if suite and suite.get("confirmed"):
        named = ", ".join((suite.get("new_failures") or [])[:5]) or "files the runner did not name"
        return "fix-unproven", ("the fix makes the full suite fail beyond the tree's own "
                                f"failures: {named}")
    latest = latest_reviews(result.get("reviews"))
    for lens in REVIEW_LENSES:
        answers = [r for r in latest if r.get("lens") == lens]
        if not answers:
            return "fix-rejected", f"no {lens} review"
        for review in answers:
            if review.get("verdict") != "safe" or review.get("failed"):
                return "fix-rejected", f"{reviewer(review)}: {review.get('reason') or 'not safe'}"
    return None, "proven on the base and safe under every review lens"


def latest_reviews(reviews: list[dict] | None) -> list[dict]:
    """Each reviewer's latest answer, one per lens and model, in the order the
    reviewers first answered; a revision's reviews come after those of the
    version they answered."""
    latest: dict[tuple[object, object], dict] = {}
    for review in reviews or []:
        latest[review.get("lens"), review.get("model")] = review
    return list(latest.values())


def reviewer(review: dict) -> str:
    """A review's lens, with the model that gave it when recorded."""
    model = review.get("model")
    return f"{review.get('lens')} ({model})" if model else str(review.get("lens"))


CoverageStatus = Literal["addressed", "not-addressed", "already-fixed", "out-of-scope"]
COVERAGE_STATUSES: tuple[CoverageStatus, ...] = ("addressed", "not-addressed", "already-fixed",
                                                 "out-of-scope")
# Which status a defect two models account for differently keeps: a defect
# is covered only when no model says the change leaves it.
_COVERAGE_RANK: dict[CoverageStatus, int] = {"not-addressed": 0, "addressed": 1,
                                             "already-fixed": 2, "out-of-scope": 3}
# The claim-test rulings (`claim_test.Ruling`) that take a defect out of what
# the report asks: the report does not ask for it, or the code means it.
OUT_OF_REPORT_RULINGS = ("outside-report", "deliberate")


class CoverageEntry(TypedDict):
    defect: str
    status: CoverageStatus
    evidence: str


@dataclass(frozen=True)
class Coverage:
    """How much of its report a fix covers. `state` is "complete" when every
    defect the reviewer found in the report is addressed by the change, already
    handled on the base, or out of the report's scope, "partial" when one is
    none of these, and "unchecked" when no review carries a usable account.
    `unshown` holds the not-addressed defects a claim test could not show
    remain (`claim_test`), which `state` and `defects` leave out; only
    "complete" with none unshown closes the issue."""
    state: Literal["complete", "partial", "unchecked"]
    defects: list[CoverageEntry]
    unshown: list[CoverageEntry] = field(default_factory=list)

    @property
    def closes(self) -> bool:
        return self.state == "complete" and not self.unshown

    def having(self, status: CoverageStatus) -> list[CoverageEntry]:
        return [d for d in self.defects if d["status"] == status]


def coverage_entries(raw: object) -> list[CoverageEntry] | None:
    """A reviewer's `reported_defects` as {"defect", "status", "evidence"}
    entries, or None when missing, empty or malformed — a report names at least
    the defect it was filed for."""
    if not isinstance(raw, list) or not raw:
        return None
    out: list[CoverageEntry] = []
    for d in raw:
        if (not isinstance(d, dict) or d.get("status") not in COVERAGE_STATUSES
                or not str(d.get("defect") or "").strip()):
            return None
        out.append({"defect": str(d["defect"]).strip(), "status": d["status"],
                    "evidence": str(d.get("evidence") or "").strip()})
    return out


def defect_key(defect: str) -> str:
    return " ".join(defect.casefold().split())


def fix_coverage(reviews: list[dict] | None, claims: list[dict] | None = None) -> Coverage:
    """The coverage the reviewers give the fix: each model's latest review that
    recorded an account (a revision's review comes after the one it answered),
    merged into one list of defects. Defects the models name in the same words
    are one, with the status from _COVERAGE_RANK. A defect left not addressed
    whose latest claim test (`claims`, in the reviewer's words) ruled it
    outside the report or deliberate (OUT_OF_REPORT_RULINGS) is out of scope,
    with that ruling as its evidence. Any other not-addressed defect whose
    latest claim is unshown moves to `unshown`, unless that would leave no
    defect in scope."""
    accounts: dict[object, list[CoverageEntry]] = {}
    for review in reviews or []:
        defects = coverage_entries(review.get("coverage"))
        if defects is not None:
            accounts[review.get("model")] = defects
    if not accounts:
        return Coverage("unchecked", [])
    merged: dict[str, CoverageEntry] = {}
    for defects in accounts.values():
        for d in defects:
            key = defect_key(d["defect"])
            held = merged.get(key)
            if held is None or _COVERAGE_RANK[d["status"]] < _COVERAGE_RANK[held["status"]]:
                merged[key] = d
    ruled = {defect_key(str(c.get("defect") or "")): c for c in claims or []}
    for key, d in merged.items():
        claim = ruled.get(key)
        if (d["status"] == "not-addressed" and claim
                and claim.get("ruling") in OUT_OF_REPORT_RULINGS):
            merged[key] = {**d, "status": "out-of-scope",
                           "evidence": f"the host's claim test: {claim.get('why') or ''}"}
    claimed = {key: c.get("status") for key, c in ruled.items()}
    out = list(merged.values())
    unshown = [d for d in out if d["status"] == "not-addressed"
               and claimed.get(defect_key(d["defect"])) == "unshown"]
    if len(unshown) < len(out):
        out = [d for d in out if d not in unshown]
    else:
        unshown = []
    partial = any(d["status"] == "not-addressed" for d in out)
    return Coverage("partial" if partial else "complete", out, unshown)


def revision_passed(round_: dict) -> bool | None:
    """Whether a revision round passed the tests it was revised against; None
    when it wrote no change to test. A round that failed them was discarded."""
    return round_.get("required_pass", round_.get("reproductions_pass"))


def counted_revisions(rounds: list[dict]) -> list[dict]:
    """The revision rounds whose change stands."""
    return [r for r in rounds if revision_passed(r) is True]


def review_comments(reviews: list[dict] | None,
                    claims: list[dict] | None = None) -> tuple[bool, list[str]]:
    """What the reviews ask the fixer to change, as one code review's comments,
    and whether any of them rejects the fix: (rejected, comments). Each
    reviewer's latest answer counts (latest_reviews), so any model's judged
    rejection rejects, and every model's comments are combined. A judged
    rejection comments its reason and concerns and rejects; a reported defect
    the merged coverage account (fix_coverage, with `claims`) leaves unaddressed,
    shown to remain or not, is a comment that does not reject, since a proven
    fix for part of a report stands as a partial fix. A reviewer that reached no verdict comments nothing: its
    failure is the caller's to read, never a pass."""
    rejected = False
    comments: list[str] = []
    for review in latest_reviews(reviews):
        if review.get("failed") or review.get("verdict") == "safe":
            continue
        rejected = True
        comments += [f"{reviewer(review)}: {c}"
                     for c in [review.get("reason") or "not safe", *(review.get("concerns") or [])]]
    comments += [f"not addressed: {d['defect']}" + (f" ({d['evidence']})" if d["evidence"]
                                                    else "")
                 for cov in [fix_coverage(reviews, claims)]
                 for d in [*cov.having("not-addressed"), *cov.unshown]]
    return rejected, list(dict.fromkeys(comments))


def propose_gate(record: dict, live: dict | None, *,
                 report_sha: Callable[[str, str], str]) -> tuple[bool, str]:
    """Whether a finished lane run may be proposed upstream as a pull request.
    `record` is the run's `result.json`; `live` the issue as GitHub reports it
    now. The run must have ended `fixed` on a report that is still open and
    unedited, carry a non-empty patch, and scan clear."""
    if record.get("ending") != "fixed" or record.get("fault"):
        return False, f"the run ended {record.get('ending')!r}, not 'fixed'"
    result = record.get("result") or {}
    patch = str(result.get("patch") or "")
    if not patch.strip():
        return False, "the run recorded no patch"
    if live is None:
        return False, "the issue cannot be read upstream"
    if live.get("state") != "open":
        return False, "the issue is closed"
    if report_sha(live.get("title") or "", live.get("body") or "") != record.get("report_sha"):
        return False, "the report was edited after the run; re-run the lane on it"
    verdict = threats.scan_diff(patch).get("verdict")
    if verdict != "clear":
        return False, f"the patch scans {verdict}"
    return True, "fixed, open, unedited, and clear"


def question_gate(record: dict, live: dict | None, *,
                  report_sha: Callable[[str, str], str]) -> tuple[bool, str]:
    """Whether a finished lane run may ask its issue a question: it ended
    `fix-disputed` with at least two readings, on a report that is still open
    and unedited."""
    if record.get("ending") != "fix-disputed":
        return False, f"the run ended {record.get('ending')!r}, not 'fix-disputed'"
    if len((record.get("result") or {}).get("readings") or []) < 2:
        return False, "the run recorded fewer than two readings"
    if live is None:
        return False, "the issue cannot be read upstream"
    if live.get("state") != "open":
        return False, "the issue is closed"
    if report_sha(live.get("title") or "", live.get("body") or "") != record.get("report_sha"):
        return False, "the report was edited after the run; re-run the lane on it"
    return True, "disputed, open, and unedited"
