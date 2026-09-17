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
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues())
    assert ok, reason


def test_eligibility_allows_canonical_closed_as_completed(tmp_path):
    """A fixed canonical remains a valid explanation for its duplicates."""
    st, iss, cl = _confirmed_dup(tmp_path)
    canon = st.edit_issue(4)
    canon.set_meta({**META, "state": "closed", "state_reason": "completed"})
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues())
    assert ok, reason


def test_eligibility_passes_with_open_canonical(tmp_path):
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues())
    assert ok, reason


def test_eligibility_blocks_live_closed_dup(tmp_path):
    """#411: the store says open but the duplicate itself is closed upstream —
    the live check blocks it."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(
        iss, cl, st.all_issues(), live_state=lambda n: "closed" if n == 5 else "open")
    assert not ok and "#5" in reason and "already closed" in reason


def test_eligibility_does_not_live_check_canonical(tmp_path):
    """Only the issue being closed needs a live state check; the canonical's
    current state is irrelevant to the duplicate relationship."""
    st, iss, cl = _confirmed_dup(tmp_path)
    calls: list[int] = []

    def fetch(n: int) -> str:
        calls.append(n)
        return "open" if n == 5 else "closed"

    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues(), live_state=fetch)
    assert calls == [5]
    assert ok, reason


def test_eligibility_fails_open_when_live_unreachable(tmp_path):
    """A live fetcher that returns None (GitHub unreachable) falls back to the
    store's state and does not block."""
    st, iss, cl = _confirmed_dup(tmp_path)
    ok, reason = issue_gates.close_dup_eligibility(iss, cl, st.all_issues(), live_state=lambda n: None)
    assert ok, reason


def test_eligibility_skips_live_fetch_when_statically_blocked(tmp_path):
    """The live fetcher is only consulted once the static checks pass."""
    st, iss, cl = _confirmed_dup(tmp_path)
    cl.set_needs_review(True)
    calls: list[int] = []

    def fetch(n: int) -> str | None:
        calls.append(n)
        return "open"

    ok, _ = issue_gates.close_dup_eligibility(iss, st.load_issue_cluster(1), st.all_issues(), live_state=fetch)
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


PROVEN = {"proof": {"red": {"exit": 20, "exit_confirm": 20},
                    "green": {"exit": 0, "exit_confirm": 0}, "compile": None,
                    "related_tests": None},
          "reviews": [{"lens": "root-cause", "verdict": "safe", "reason": "", "concerns": []},
                      {"lens": "scope-safety", "verdict": "safe", "reason": "", "concerns": []}]}


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


def test_related_tests_the_base_fails_too_do_not_count_against_the_fix():
    related = {"files": ["a.test.ts"], "run": {"exit": 20}, "base_fails": True}
    assert issue_gates.fix_proof_bar(_result(proof__related_tests=related))[0] is None


@pytest.mark.parametrize("over,ending", [
    ({"proof__green": {"exit": 20, "exit_confirm": None}}, "fix-unproven"),
    ({"proof__green": {"exit": 0, "exit_confirm": 20}}, "fix-unproven"),
    ({"proof__red": {"exit": 0, "exit_confirm": None}}, "fix-unproven"),
    ({"proof__compile": {"exit": 20, "error_excerpt": "TS2304"}}, "fix-unproven"),
    ({"proof__compile": {"refused": "empty"}}, "fix-unproven"),
    ({"proof__related_tests": {"files": ["a.test.ts"], "run": {"exit": 20}}}, "fix-unproven"),
    ({"reviews": PROVEN["reviews"][:1]}, "fix-rejected"),
    ({"reviews": [PROVEN["reviews"][0],
                  {"lens": "scope-safety", "verdict": "unsafe", "reason": "widens auth",
                   "concerns": []}]}, "fix-rejected"),
])
def test_the_bar_names_the_shortfall(over, ending):
    got, why = issue_gates.fix_proof_bar(_result(**over))
    assert got == ending and why


