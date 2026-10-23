"""issue_gates: close-as-dup policy + derived cluster state."""
import copy

import pytest

from issue_triage import issue_gates
from issue_triage import issue_store

META = {"title": "t", "state": "open", "updated_at": "T1"}


def _confirmed_dup(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    iss = st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    cl.record_curation({"confirmed": True, "canonical": 4})
    iss.route_to("close-dup", "dup of #4", canonical=4)
    return st, st.load_issue(5), st.load_issue_cluster(1)


def test_allowed_for_confirmed_dup(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_allowed(iss, cl)
    assert ok, reason


def test_blocked_when_needs_review(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    cl.set_needs_review(True)
    ok, reason = issue_gates.close_dup_allowed(iss, st.load_issue_cluster(1))
    assert not ok and "needs-review" in reason


def test_blocked_when_not_close_dup(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    iss.route_to("needs-human", "unclear")
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), None)
    assert not ok and "close-dup" in reason


def test_blocked_when_canonical_is_self(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    iss = st.create_issue(5, META)
    # bypass model (route_to forbids nothing here) — craft analysis pointing at self
    iss.route_to("close-dup", "self", canonical=5)
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), None)
    assert not ok and "itself" in reason


def test_blocked_when_curation_unconfirmed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    iss = st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    iss.route_to("close-dup", "dup of #4", canonical=4)
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), st.load_issue_cluster(1))
    assert not ok and "confirm" in reason.lower()


def test_blocked_when_issue_closed_in_store(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    dup = st.edit_issue(5)
    dup.set_meta({**META, "state": "closed"})
    ok, reason = issue_gates.close_dup_allowed(st.load_issue(5), cl)
    assert not ok and "already closed" in reason


def test_eligibility_allows_closed_not_planned_canonical(tmp_path):
    """The canonical's resolution does not invalidate a duplicate relationship."""
    st, iss, cl = _confirmed_dup(tmp_path)
    canon = st.edit_issue(4)
    canon.set_meta({**META, "state": "closed", "state_reason": "not_planned"})
    ok, reason = issue_gates.close_dup_eligibility(iss, cl)
    assert ok, reason


def test_eligibility_allows_canonical_closed_as_completed(tmp_path):
    """A fixed canonical remains a valid explanation for its duplicates."""
    st, iss, cl = _confirmed_dup(tmp_path)
    canon = st.edit_issue(4)
    canon.set_meta({**META, "state": "closed", "state_reason": "completed"})
    ok, reason = issue_gates.close_dup_eligibility(iss, cl)
    assert ok, reason


def test_eligibility_passes_with_open_canonical(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(iss, cl)
    assert ok, reason


def test_eligibility_blocks_live_closed_dup(tmp_path):
    """#411: the store says open but the duplicate itself is closed upstream —
    the live check blocks it."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(
        iss, cl, live_state=lambda n: "closed" if n == 5 else "open")
    assert not ok and "#5" in reason and "already closed" in reason


def test_eligibility_does_not_live_check_canonical(tmp_path):
    """Only the issue being closed needs a live state check; the canonical's
    current state is irrelevant to the duplicate relationship."""
    st, iss, cl = _confirmed_dup(tmp_path)
    calls: list[int] = []

    def fetch(n: int) -> str:
        calls.append(n)
        return "open" if n == 5 else "closed"

    ok, reason = issue_gates.close_dup_eligibility(iss, cl, live_state=fetch)
    assert calls == [5]
    assert ok, reason


def test_eligibility_fails_open_when_live_unreachable(tmp_path):
    """A live fetcher that returns None (GitHub unreachable) falls back to the
    store's state and does not block."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, live_state=lambda n: None)
    assert ok, reason


def test_eligibility_skips_live_fetch_when_statically_blocked(tmp_path):
    """The live fetcher is only consulted once the static checks pass."""
    st, iss, cl = _confirmed_dup(tmp_path)
    cl.set_needs_review(True)
    calls: list[int] = []

    def fetch(n: int) -> str | None:
        calls.append(n)
        return "open"

    ok, _ = issue_gates.close_dup_eligibility(iss, st.load_issue_cluster(1), live_state=fetch)
    assert not ok and calls == []


def test_cluster_state_dup_ready(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    assert issue_gates.issue_cluster_state(cl, st.all_issues()) == "dup-ready"


def test_cluster_state_needs_curation_when_unconfirmed(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(4, META)
    st.create_issue(5, META)
    cl = st.create_issue_cluster(1, "c")
    cl.set_members([4, 5])
    assert issue_gates.issue_cluster_state(cl, st.all_issues()) == "needs-curation"


RED = {"exit": 20, "exit_confirm": 20}
SURE = {"symptom_match": {"matches": True, "confidence": "high", "reasoning": "r"},
        "defect": {"is_defect": True, "confidence": "medium", "reasoning": "r"}}


def _judge(**over):
    out = {k: dict(v) for k, v in SURE.items()}
    for key, change in over.items():
        out[key].update(change)
    return out


@pytest.mark.parametrize("red,judge,gave_up,invalid,want", [
    (RED, SURE, False, None, "reproduced"),
    (RED, SURE, True, None, "unwritable"),
    (RED, SURE, False, "path-not-a-test-path", "unwritable"),
    ({"exit": 0, "exit_confirm": None}, SURE, False, None, "not-reproduced"),
    ({"exit": 20, "exit_confirm": 0}, SURE, False, None, "not-reproduced"),
    ({"exit": 124, "exit_confirm": None}, SURE, False, None, None),
    ({"exit": 20, "exit_confirm": 30}, SURE, False, None, None),
    (RED, None, False, None, None),
    (RED, {"symptom_match": {"matches": "yes"}}, False, None, None),
    (RED, _judge(symptom_match={"matches": False}), False, None, "wrong-symptom"),
    (RED, _judge(symptom_match={"confidence": "low"}), False, None, "wrong-symptom"),
    (RED, _judge(defect={"is_defect": False}), False, None, "not-a-defect"),
    (RED, _judge(defect={"confidence": "low"}), False, None, "not-a-defect"),
    (RED, _judge(symptom_match={"confidence": "certain"}), False, None, None),
])
def test_reproduction_outcome(red, judge, gave_up, invalid, want):
    assert issue_gates.reproduction_outcome(red, judge, gave_up=gave_up, invalid=invalid) == want


def test_every_outcome_is_in_the_vocabulary():
    assert set(issue_gates.REPRODUCTION_OUTCOMES) == {
        "reproduced", "not-reproduced", "unwritable", "wrong-symptom", "not-a-defect"}


FIX = ("diff --git a/src/x.ts b/src/x.ts\n--- a/src/x.ts\n+++ b/src/x.ts\n"
       "@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n")
CHANGES = [{"path": "src/x.ts", "rationale": "r"}]


def test_a_disclosed_in_bounds_fix_clears_the_regate():
    assert issue_gates.fix_patch_regate(FIX, changes=CHANGES, max_lines=300) == (True, "clean")


def test_a_test_file_reported_beside_the_fix_belongs_to_the_reproduction():
    changes = CHANGES + [{"path": "src/x.test.ts", "rationale": "reproduces it"}]
    assert issue_gates.fix_patch_regate(FIX, changes=changes, max_lines=300) == (True, "clean")


def test_an_unreported_source_file_is_refused_beside_a_reported_test():
    ok, why = issue_gates.fix_patch_regate(
        FIX, changes=[{"path": "src/x.test.ts", "rationale": ""}], max_lines=300)
    assert not ok and "did not report" in why


@pytest.mark.parametrize("patch,changes,needle", [
    ("", CHANGES, "not a diff"),
    (FIX, [], "did not report"),
    (FIX.replace("src/x.ts", "src/x.test.ts"), [{"path": "src/x.test.ts", "rationale": ""}],
     "test files"),
    (FIX.replace("src/x.ts", "package.json"), [{"path": "package.json", "rationale": ""}],
     "dependency manifest"),
    (FIX + "Binary files a/i.png and b/i.png differ\n", CHANGES, "binary"),
    (FIX.replace("--- a/src/x.ts", "old mode 100644\nnew mode 100755\n--- a/src/x.ts"), CHANGES,
     "mode"),
    (FIX + "x" * 200_000, CHANGES, "over 200000"),
    (FIX + "GIT binary patch\n", CHANGES, "binary"),
    (FIX + "new file mode 120000\n", CHANGES, "symlink"),
    (FIX + "new file mode 160000\n", CHANGES, "submodule"),
    (FIX + "new file mode 100755\n", CHANGES, "executable"),
])
def test_the_regate_refuses(patch, changes, needle):
    ok, why = issue_gates.fix_patch_regate(patch, changes=changes, max_lines=300)
    assert not ok and needle in why


def test_the_regate_counts_changed_lines_against_the_limit():
    ok, why = issue_gates.fix_patch_regate(FIX, changes=CHANGES, max_lines=1)
    assert not ok and "2 lines" in why
    assert issue_gates.changed_line_count(FIX) == 2


def test_the_regate_refuses_a_withheld_path(monkeypatch):
    monkeypatch.setattr(issue_gates.gates, "fix_withheld_paths", lambda paths: list(paths))
    ok, why = issue_gates.fix_patch_regate(FIX, changes=CHANGES, max_lines=300)
    assert not ok and "withheld" in why


def test_the_regate_refuses_tier_zero(monkeypatch):
    monkeypatch.setattr(issue_gates.risktier, "pr_tier", lambda paths: 0)
    ok, why = issue_gates.fix_patch_regate(FIX, changes=CHANGES, max_lines=300)
    assert not ok and "tier" in why


def test_the_regate_refuses_a_malicious_scan(monkeypatch):
    monkeypatch.setattr(issue_gates.threats, "scan_diff",
                        lambda patch: {"verdict": "malicious", "signatures": ["self-decoder"]})
    ok, why = issue_gates.fix_patch_regate(FIX, changes=CHANGES, max_lines=300)
    assert not ok and "threat signature" in why


PROVEN = {"proof": {"red": {"exit": 20, "exit_confirm": 20},
                    "green": {"exit": 0, "exit_confirm": 0},
                    "preserve": {"exit": 0, "exit_confirm": 0}, "compile": None,
                    "related_tests": None},
          "reviews": [{"lens": lens, "verdict": "safe", "reason": "", "concerns": []}
                      for lens in issue_gates.REVIEW_LENSES]}


def _result(**over):
    out = copy.deepcopy(PROVEN)
    for dotted, value in over.items():
        node = out
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return out


def test_a_proven_doubly_reviewed_fix_passes_the_bar():
    assert issue_gates.fix_proof_bar(PROVEN)[0] is None


def test_a_compile_failure_the_tree_shares_does_not_count_against_the_fix():
    compiled = {"exit": 20, "error_excerpt": "TS5058", "tree_fails": True}
    assert issue_gates.fix_proof_bar(_result(proof__compile=compiled))[0] is None


@pytest.mark.parametrize("lint", [
    {"exit": 20, "tree_fails": True},
    {"exit": 20, "error": "fails on trunk itself", "error_kind": "base-lint"},
])
def test_a_lint_failure_the_base_shares_does_not_count_against_the_fix(lint):
    assert issue_gates.fix_proof_bar(_result(proof__lint=lint))[0] is None


def test_a_suite_flake_does_not_count_against_the_fix():
    suite = {"confirmed": False, "flake": True, "new_failures": []}
    assert issue_gates.fix_proof_bar(_result(proof__suite=suite))[0] is None


def test_related_tests_the_base_fails_too_do_not_count_against_the_fix():
    related = {"files": ["a.test.ts"], "run": {"exit": 20}, "base_fails": True}
    assert issue_gates.fix_proof_bar(_result(proof__related_tests=related))[0] is None


@pytest.mark.parametrize("over,ending", [
    ({"proof__green": {"exit": 20, "exit_confirm": None}}, "fix-unproven"),
    ({"proof__green": {"exit": 0, "exit_confirm": 20}}, "fix-unproven"),
    ({"proof__red": {"exit": 0, "exit_confirm": None}}, "fix-unproven"),
    ({"proof__preserve": {"exit": 20, "exit_confirm": None}}, "fix-unproven"),
    ({"proof__preserve": None}, "fix-unproven"),
    ({"proof__suite": {"confirmed": True, "new_failures": ["a.test.ts"]}}, "fix-unproven"),
    ({"proof__compile": {"exit": 20, "error_excerpt": "TS2304"}}, "fix-unproven"),
    ({"proof__compile": {"refused": "empty"}}, "fix-unproven"),
    ({"proof__related_tests": {"files": ["a.test.ts"], "run": {"exit": 20}}}, "fix-unproven"),
    ({"proof__lint": {"exit": 20, "error_excerpt": "boundary"}}, "fix-unproven"),
    ({"proof__lint": {"error": "docker down"}}, "sandbox"),
    # A sandbox that could not run the check is the machine's fault, never a
    # verdict on the fix (#13817: a full disk on the worker).
    ({"proof__compile": {"exit": 40, "error": "the sandbox could not apply the patch for a "
                         "reason that is not the patch's: No space left on device"}},
     "sandbox"),
    ({"proof__compile": {"error": "the sandbox phase exited 125 before the command "
                         "concluded"}}, "sandbox"),
    ({"proof__related_tests": {"files": ["a.test.ts"], "run": {"error": "docker down"}}},
     "sandbox"),
    ({"reviews": PROVEN["reviews"][:1]}, "fix-rejected"),
    ({"reviews": [PROVEN["reviews"][0],
                  {"lens": "scope-safety", "verdict": "unsafe", "reason": "widens auth",
                   "concerns": []}]}, "fix-rejected"),
    ({"reviews": [PROVEN["reviews"][0],
                  {"lens": "scope-safety", "verdict": "safe", "reason": "", "concerns": [],
                   "failed": True}]}, "fix-rejected"),
])
def test_the_bar_names_the_shortfall(over, ending):
    got, why = issue_gates.fix_proof_bar(_result(**over))
    assert got == ending and why




_FAULT = {"kind": "test", "exit": None, "error_kind": "infrastructure",
          "error": "the sandbox could not apply the patch for a reason that is not the "
                   "patch's: error: unable to create temporary file: No space left on device"}


@pytest.mark.parametrize("checks,faulted", [
    ([_FAULT, _FAULT], True),
    ([{"kind": "test", "error_kind": "refused", "error": "cap"}, _FAULT], True),
    ([_FAULT, {"kind": "test", "exit": 20, "error_kind": None, "error": None}], False),
    ([{"kind": "compile", "exit": 20, "error_kind": "base-compile", "error": "base"}], False),
    ([{"kind": "test", "error_kind": "refused", "error": "cap"}], False),
    ([], False),
])
def test_a_give_up_is_the_machine_s_only_when_no_check_concluded(checks, faulted):
    fault = issue_gates.checks_fault(checks)
    assert bool(fault) is faulted
    if faulted:
        assert "No space left on device" in fault


# --- fix coverage ----------------------------------------------------------------

def _cov(*statuses):
    return [{"defect": f"defect {i}", "status": st, "evidence": "e"}
            for i, st in enumerate(statuses)]


@pytest.mark.parametrize("statuses,state", [
    (("addressed",), "complete"),
    (("addressed", "already-fixed"), "complete"),
    # Issue #14777: logging fixed, cancel and startup reconcile left.
    (("addressed", "not-addressed", "not-addressed"), "partial"),
])
def test_coverage_is_complete_only_when_no_reported_defect_is_left(statuses, state):
    cov = issue_gates.fix_coverage([{"lens": "scope-safety", "coverage": _cov(*statuses)}])
    assert cov.state == state and cov.closes == (state == "complete")
    assert len(cov.having("not-addressed")) == statuses.count("not-addressed")


@pytest.mark.parametrize("reviews", [
    None, [], [{"lens": "scope-safety"}], [{"coverage": []}],
    [{"coverage": [{"defect": "x", "status": "partly"}]}]])
def test_coverage_without_a_usable_account_is_unchecked_and_does_not_close(reviews):
    cov = issue_gates.fix_coverage(reviews)
    assert cov.state == "unchecked" and not cov.closes


def test_a_revision_s_coverage_supersedes_the_review_it_answered():
    reviews = [{"coverage": _cov("addressed", "not-addressed")},
               {"coverage": _cov("addressed", "addressed")}]
    assert issue_gates.fix_coverage(reviews).state == "complete"
    assert issue_gates.fix_coverage(reviews[::-1]).state == "partial"


# --- review comments ---------------------------------------------------------------

def test_a_rejection_and_a_left_defect_are_both_comments_only_the_rejection_rejects():
    root = {"lens": "root-cause", "verdict": "unsafe", "reason": "masks the symptom",
            "concerns": ["special-cases the test input"]}
    scope = {"lens": "scope-safety", "verdict": "safe",
             "coverage": _cov("addressed", "not-addressed")}
    assert issue_gates.review_comments([scope]) == (
        False, ["not addressed: defect 1 (e)"])
    assert issue_gates.review_comments([root, scope]) == (True, [
        "root-cause: masks the symptom", "root-cause: special-cases the test input",
        "not addressed: defect 1 (e)"])


def test_each_lens_s_latest_review_counts_and_a_stalled_one_asks_nothing():
    old = {"lens": "scope-safety", "verdict": "unsafe", "reason": "leaks"}
    new = {"lens": "scope-safety", "verdict": "safe", "coverage": _cov("addressed")}
    assert issue_gates.review_comments([old, new]) == (False, [])
    stalled = {"lens": "root-cause", "verdict": "unsafe", "failed": True, "reason": "timeout"}
    assert issue_gates.review_comments([stalled, new]) == (False, [])


# --- reviewers on several models ---------------------------------------------------

def _two_models(**over):
    """Every lens answered `safe` by opus and codex:gpt-x, with `over` keyed
    "<lens>@<model>" replacing an answer's fields."""
    out = []
    for lens in issue_gates.REVIEW_LENSES:
        for model in ("opus", "codex:gpt-x"):
            out.append({"lens": lens, "model": model, "verdict": "safe", "reason": "",
                        "concerns": [], **over.get(f"{lens}@{model}", {})})
    return out


def test_every_model_safe_passes_the_bar():
    assert issue_gates.fix_proof_bar(_result(reviews=_two_models()))[0] is None


@pytest.mark.parametrize("answer", [
    {"verdict": "unsafe", "reason": "relaxes the check"},
    {"verdict": "unsafe", "failed": True, "reason": "timed out"},
    {"verdict": "safe", "failed": True},
])
def test_one_model_s_rejection_or_failure_blocks_the_bar(answer):
    reviews = _two_models(**{"scope-safety@codex:gpt-x": answer})
    ending, why = issue_gates.fix_proof_bar(_result(reviews=reviews))
    assert ending == "fix-rejected" and why.startswith("scope-safety (codex:gpt-x): ")


def test_a_revision_answers_each_model_s_review_apart():
    first = _two_models(**{"root-cause@opus": {"verdict": "unsafe", "reason": "masks"}})
    assert issue_gates.fix_proof_bar(_result(reviews=first))[0] == "fix-rejected"
    revised = first + _two_models()
    assert len(issue_gates.latest_reviews(revised)) == 2 * len(issue_gates.REVIEW_LENSES)
    assert issue_gates.fix_proof_bar(_result(reviews=revised))[0] is None


def test_any_model_s_rejection_rejects_and_every_model_s_concerns_are_combined():
    reviews = _two_models(**{
        "root-cause@opus": {"verdict": "unsafe", "reason": "masks", "concerns": ["a"]},
        "root-cause@codex:gpt-x": {"verdict": "unsafe", "reason": "masks", "concerns": ["b"]},
        "scope-safety@opus": {"verdict": "unsafe", "failed": True, "reason": "timed out"}})
    assert issue_gates.review_comments(reviews) == (True, [
        "root-cause (opus): masks", "root-cause (opus): a",
        "root-cause (codex:gpt-x): masks", "root-cause (codex:gpt-x): b"])


def test_a_model_that_did_not_finish_beside_a_safe_one_neither_rejects_nor_passes():
    reviews = _two_models(**{"root-cause@codex:gpt-x": {"verdict": "unsafe", "failed": True}})
    assert issue_gates.review_comments(reviews) == (False, [])
    assert issue_gates.fix_proof_bar(_result(reviews=reviews))[0] == "fix-rejected"


def _account(model, *entries):
    return {"lens": "scope-safety", "model": model, "verdict": "safe",
            "coverage": [{"defect": d, "status": st, "evidence": f"{model}:{d}"}
                         for d, st in entries]}


def test_a_defect_is_covered_only_when_no_model_says_it_is_left():
    reviews = [_account("opus", ("Cancel hangs", "addressed"), ("logs leak", "addressed")),
               _account("codex:gpt-x", ("cancel  HANGS", "not-addressed"),
                        ("logs leak", "already-fixed"))]
    cov = issue_gates.fix_coverage(reviews)
    assert cov.state == "partial"
    assert [(d["defect"], d["status"]) for d in cov.defects] == [
        ("cancel  HANGS", "not-addressed"), ("logs leak", "addressed")]


def test_coverage_unions_the_defects_each_model_names():
    reviews = [_account("opus", ("a", "addressed")),
               _account("codex:gpt-x", ("a", "addressed"), ("b", "already-fixed"))]
    cov = issue_gates.fix_coverage(reviews)
    assert cov.state == "complete" and [d["defect"] for d in cov.defects] == ["a", "b"]


def test_each_model_s_latest_account_counts_and_a_defect_both_leave_is_one_claim():
    reviews = [_account("opus", ("a", "not-addressed"), ("b", "not-addressed")),
               _account("codex:gpt-x", ("A", "not-addressed")),
               _account("opus", ("a", "not-addressed"), ("b", "addressed"))]
    assert issue_gates.fix_coverage(reviews).having("not-addressed") == [
        {"defect": "a", "status": "not-addressed", "evidence": "opus:a"}]
    assert issue_gates.review_comments(reviews) == (False, ["not addressed: a (opus:a)"])


def test_a_fix_without_a_design_review_falls_short():
    """#669: a fix that works but a maintainer would not merge is rejected like
    one that misses the cause."""
    reviews = [r for r in PROVEN["reviews"] if r["lens"] != "design"]
    assert issue_gates.fix_proof_bar(_result(reviews=reviews)) == ("fix-rejected", "no design review")
    reviews.append({"lens": "design", "verdict": "unsafe", "concerns": [],
                    "reason": "writes the same predicate three times"})
    ending, why = issue_gates.fix_proof_bar(_result(reviews=reviews))
    assert ending == "fix-rejected" and "three times" in why


@pytest.mark.parametrize("related, words", [
    ({"run": {"exit": 0}}, "passed"),
    ({"run": {"exit": 20}, "base_fails": True},
     "failed, and fail on the base too, so they cannot show whether the change breaks them"),
    ({"run": {"exit": 20}}, "failed (exit 20)"),
])
def test_related_tests_are_worded_by_their_outcome(related, words):
    assert issue_gates.related_tests_outcome(related) == words
