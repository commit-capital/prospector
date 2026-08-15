# prospector_app/backend/test_filters.py
"""filters.matches(): spec → bool over a pr_row dict. Rows are plain dicts in the
pr_row shape (service.pr_row), so tests build minimal rows directly."""
from prospector_app.backend import filters


def _row(**over):
    row = {
        "number": 1, "title": "fix thing", "author": "alice", "clusters": [7],
        "disposition": "merge", "safety": "GREEN", "safety_fresh": True,
        "drift_state": "applicable", "clean": True, "trusted_author": False,
        "signals": {"greptile": 5, "ci": "passing", "conflicts": False,
                    "has_tests": True, "additions": 4, "deletions": 1, "changed_files": 1},
    }
    row.update(over)
    return row


def test_empty_spec_matches_everything():
    assert filters.matches(_row(), {}) is True


def test_exact_fields():
    r = _row(safety="RED", disposition="needs-human", clusters=[7])
    assert filters.matches(r, {"safety": "RED"})
    assert not filters.matches(r, {"safety": "GREEN"})
    assert filters.matches(r, {"disposition": "needs-human", "cluster": 7})
    assert not filters.matches(r, {"cluster": 9})


def test_cluster_filter_matches_any_membership():
    # a straddler (#196) belongs to several clusters; the cluster filter keeps it
    # under each of them, and excludes a cluster it is not in.
    r = _row(clusters=[7, 9])
    assert filters.matches(r, {"cluster": 7})
    assert filters.matches(r, {"cluster": 9})
    assert not filters.matches(r, {"cluster": 8})


def test_cluster_filter_excludes_unclustered():
    assert not filters.matches(_row(clusters=[]), {"cluster": 7})


def test_cluster_none_filter():
    # cluster_none: True matches PRs with no cluster assignment
    assert filters.matches(_row(clusters=[]), {"cluster_none": True})
    assert not filters.matches(_row(clusters=[7]), {"cluster_none": True})
    # a straddler (multi-cluster) is also excluded
    assert not filters.matches(_row(clusters=[7, 9]), {"cluster_none": True})
    # falsy cluster_none is inert
    assert filters.matches(_row(clusters=[7]), {"cluster_none": False})


def test_author_is_case_insensitive_prefix():
    assert filters.matches(_row(author="Alice"), {"author": "alice"})
    assert filters.matches(_row(author="dependabot[bot]"), {"author": "depend"})
    assert not filters.matches(_row(author="Alice"), {"author": "lice"})


def test_q_matches_title_author_or_number():
    assert filters.matches(_row(), {"q": "thing"})
    assert filters.matches(_row(), {"q": "ALICE"})
    assert filters.matches(_row(number=42), {"q": "42"})
    assert not filters.matches(_row(), {"q": "absent"})


def test_greptile_numeric_compare():
    r = _row(signals={"greptile": 2})
    assert filters.matches(r, {"greptile": {"op": "<", "value": 3}})
    assert not filters.matches(r, {"greptile": {"op": ">=", "value": 5}})


def test_greptile_missing_treated_as_zero():
    # not yet reviewed by Greptile ("—" in the UI): counts as 0, so it's below
    # any positive threshold and never above one
    r = _row(signals={"greptile": None})
    assert filters.matches(r, {"greptile": {"op": "<", "value": 3}})
    assert not filters.matches(r, {"greptile": {"op": ">", "value": 0}})


def test_greptile_stale_filter_is_symmetric_and_excludes_unknown():
    # "stale" (True) keeps only known-stale; "current" (False) keeps only
    # known-current. Unknown staleness (None — no reviewed SHA stored) matches
    # NEITHER, so the "Greptile current" checkbox is a real guarantee that the
    # score reflects the head, not merely "not known to be stale".
    stale = _row(signals={"greptile_stale": True})
    current = _row(signals={"greptile_stale": False})
    unknown = _row(signals={"greptile_stale": None})

    assert filters.matches(current, {"greptile_stale": False})
    assert not filters.matches(stale, {"greptile_stale": False})
    assert not filters.matches(unknown, {"greptile_stale": False})

    assert filters.matches(stale, {"greptile_stale": True})
    assert not filters.matches(current, {"greptile_stale": True})
    assert not filters.matches(unknown, {"greptile_stale": True})


def test_greptile_severity_filter_excludes_clean_and_unclassified():
    defects = _row(signals={"greptile_severity": "defects"})
    nits = _row(signals={"greptile_severity": "nits"})
    clean = _row(signals={"greptile_severity": "clean"})
    unclassified = _row(signals={"greptile_severity": None})

    assert filters.matches(defects, {"greptile_severity": "defects"})
    assert not filters.matches(nits, {"greptile_severity": "defects"})
    assert not filters.matches(clean, {"greptile_severity": "defects"})
    assert not filters.matches(unclassified, {"greptile_severity": "defects"})

    assert filters.matches(nits, {"greptile_severity": "nits"})
    assert not filters.matches(defects, {"greptile_severity": "nits"})


def test_safety_not_run_means_absent_or_stale():
    assert filters.matches(_row(safety=None), {"safety": "not-run"})
    assert filters.matches(_row(safety="GREEN", safety_fresh=False), {"safety": "not-run"})
    assert not filters.matches(_row(safety="GREEN", safety_fresh=True), {"safety": "not-run"})


def test_clean_and_conflicts_and_ci():
    assert filters.matches(_row(clean=True), {"clean": True})
    assert not filters.matches(_row(clean=False), {"clean": True})
    assert filters.matches(_row(signals={"ci": "failing"}), {"ci": "failing"})
    assert filters.matches(_row(signals={"conflicts": True}), {"conflicts": True})


def test_size_and_age_predicates():
    r = _row(age_days=90, signals={"additions": 3, "deletions": 2, "changed_files": 1})
    assert filters.matches(r, {"max_total_lines": 10, "max_files": 2})
    assert not filters.matches(r, {"max_files": 0})
    assert filters.matches(r, {"age_days": {"op": ">=", "value": 60}})
    assert not filters.matches(r, {"age_days": {"op": "<", "value": 30}})


def test_age_days_missing_treated_as_zero():
    # no computable age ("—" in the UI): counts as 0, so it's below any
    # positive threshold and never above one
    r = _row(age_days=None)
    assert filters.matches(r, {"age_days": {"op": "<", "value": 30}})
    assert not filters.matches(r, {"age_days": {"op": ">", "value": 0}})


def test_files_count_compare():
    big = _row(signals={"changed_files": 25})
    assert filters.matches(big, {"files": {"op": ">", "value": 20}})
    assert not filters.matches(big, {"files": {"op": "<", "value": 20}})
    # an open control with no value yet doesn't filter anything out
    assert filters.matches(big, {"files": {"op": ">"}})
    # a row with no size data never matches a files compare
    assert not filters.matches(_row(signals={"changed_files": None}), {"files": {"op": ">", "value": 1}})


def test_risk_tier_filter():
    assert filters.matches(_row(risk_tier=3), {"risk_tier": 3})
    assert not filters.matches(_row(risk_tier=0), {"risk_tier": 3})
    assert filters.matches(_row(risk_tier=1), {"risk_tier": [0, 1]})
    # tier 0 is a real value, not a missing one
    assert filters.matches(_row(risk_tier=0), {"risk_tier": 0})
    # unknown tier (no cached diff) never matches a tier filter
    assert not filters.matches(_row(risk_tier=None), {"risk_tier": 3})
    assert not filters.matches(_row(), {"risk_tier": 3})


def test_merge_ok_filter():
    assert filters.matches(_row(merge_gate={"ok": True, "reason": ""}), {"merge_ok": True})
    assert not filters.matches(_row(merge_gate={"ok": False, "reason": "x"}), {"merge_ok": True})
    assert filters.matches(_row(merge_gate={"ok": False, "reason": "x"}), {"merge_ok": False})
    # a row with no merge gate counts as not-ok
    assert not filters.matches(_row(), {"merge_ok": True})


def test_has_summary_filter():
    with_summary = _row(summary={"one_liner": "Fixes the frobnicator", "primary_change": None})
    empty_summary = _row(summary={"one_liner": None, "primary_change": None})
    assert filters.matches(with_summary, {"has_summary": True})
    assert not filters.matches(with_summary, {"has_summary": False})
    assert filters.matches(_row(), {"has_summary": False})
    assert filters.matches(empty_summary, {"has_summary": False})
    assert not filters.matches(empty_summary, {"has_summary": True})


def test_has_issues_filter():
    linked = _row(issues=[{"number": 12}])
    assert filters.matches(linked, {"has_issues": True})
    assert not filters.matches(linked, {"has_issues": False})
    assert filters.matches(_row(issues=[]), {"has_issues": False})
    assert filters.matches(_row(), {"has_issues": False})


def test_threat_filter_matches_row_verdict():
    assert filters.matches(_row(threat="malicious"), {"threat": "malicious"})
    assert not filters.matches(_row(threat="clear"), {"threat": "malicious"})
    # a row with no threat verdict does not match a malicious filter
    assert not filters.matches(_row(threat=None), {"threat": "malicious"})


# --- loc (lines-of-code) filter -------------------------------------------
# Effective = human-written lines (source + test, artifacts stripped): a PR with
# 90/10 source + 10/10 test lines = 100/20 effective, plus a 130-line lockfile.
# raw = 250 (100 effective + 20 + 130 lockfile).
_BREAKDOWN = {"effective": 120, "raw": 250, "artifact": 130, "dominant_artifact": "lockfile",
              "by_category": {"source": {"additions": 90, "deletions": 10, "files": 2},
                              "test": {"additions": 10, "deletions": 10, "files": 1},
                              "lockfile": {"additions": 130, "deletions": 0, "files": 1}}}
_SPLIT = {"non_test": {"additions": 100, "deletions": 20, "files": 3},
          "test": {"additions": 5, "deletions": 5, "files": 1}, "removes_tests": False}


def _loc(metric="both", scope="effective", op=">", value=0):
    return {"loc": {"metric": metric, "scope": scope, "op": op, "value": value}}


def test_loc_effective_strips_artifacts():
    # raw diffstat is 250, but effective (source+test) is only 120
    r = _row(loc_breakdown=_BREAKDOWN, signals={"additions": 230, "deletions": 20})
    assert filters.matches(r, _loc("both", "effective", ">", 100))      # 120 > 100
    assert not filters.matches(r, _loc("both", "effective", ">", 200))  # 120 !> 200, not fooled by raw 250


def test_loc_added_vs_removed():
    r = _row(loc_breakdown=_BREAKDOWN)
    assert filters.matches(r, _loc("additions", "effective", ">", 50))      # 100 > 50
    assert not filters.matches(r, _loc("deletions", "effective", ">", 50))  # 20 !> 50


def test_loc_scope_all_counts_raw_diffstat():
    r = _row(loc_breakdown=_BREAKDOWN, signals={"additions": 230, "deletions": 20})
    assert filters.matches(r, _loc("both", "all", ">", 240))            # raw 250 > 240
    assert not filters.matches(r, _loc("both", "effective", ">", 240))  # effective 120 !> 240


def test_loc_less_than():
    r = _row(loc_breakdown=_BREAKDOWN)
    assert filters.matches(r, _loc("both", "effective", "<", 200))      # 120 < 200
    assert not filters.matches(r, _loc("both", "effective", "<", 100))  # 120 !< 100


def test_loc_effective_falls_back_when_no_breakdown():
    # no breakdown yet → effective falls back to the non-test split, then the total
    r = _row(loc_breakdown=None, size_split=_SPLIT, signals={"additions": 105, "deletions": 25})
    assert filters.matches(r, _loc("both", "effective", ">", 100))     # non-test 120 > 100
    r2 = _row(loc_breakdown=None, size_split=None, signals={"additions": 200, "deletions": 50})
    assert filters.matches(r2, _loc("both", "effective", ">", 100))    # aggregate 250 > 100


def test_loc_no_size_data_never_matches():
    r = _row(loc_breakdown=None, size_split=None, signals={"additions": None, "deletions": None})
    assert not filters.matches(r, _loc("both", "effective", ">", 1))


def test_loc_open_control_with_no_value_is_inert():
    spec = {"loc": {"metric": "both", "scope": "effective", "op": ">"}}  # value not entered yet
    assert filters.matches(_row(loc_breakdown=_BREAKDOWN), spec)
    assert filters.matches(_row(loc_breakdown=None, size_split=None, signals={"additions": None, "deletions": None}), spec)


# --- artifact_dominated ("mostly generated") filter -----------------------
def test_artifact_dominated_filter():
    # big + mostly generated: 1800 source of 9000 raw → 80% artifact, qualifies
    big_noisy = _row(loc_breakdown={"effective": 1800, "raw": 9000, "artifact": 7200,
                                    "dominant_artifact": "migrations", "by_category": {}})
    assert filters.matches(big_noisy, {"artifact_dominated": True})
    # big but real (mostly source) → not artifact-dominated
    big_real = _row(loc_breakdown={"effective": 8000, "raw": 9000, "artifact": 1000,
                                   "dominant_artifact": "lockfile", "by_category": {}})
    assert not filters.matches(big_real, {"artifact_dominated": True})
    # mostly generated but small (below the raw floor) → not flagged
    small = _row(loc_breakdown={"effective": 6, "raw": 700, "artifact": 694,
                                "dominant_artifact": "lockfile", "by_category": {}})
    assert not filters.matches(small, {"artifact_dominated": True})
    # no breakdown yet → never matches
    assert not filters.matches(_row(loc_breakdown=None), {"artifact_dominated": True})


# --- paths filter (substring over a PR's changed files) -------------------
def test_paths_substring_case_insensitive():
    r = _row(changed_paths=["src/billing/Invoice.ts", "src/billing/Invoice.test.ts"])
    assert filters.matches(r, {"paths": "billing"})
    assert filters.matches(r, {"paths": "INVOICE.ts"})       # case-insensitive
    assert filters.matches(r, {"paths": "src/billing/inv"})  # partial path
    assert not filters.matches(r, {"paths": "auth"})


def test_paths_no_diff_never_matches():
    assert not filters.matches(_row(changed_paths=[]), {"paths": "anything"})
    assert not filters.matches(_row(), {"paths": "anything"})  # changed_paths absent


# --- numbers filter (explicit PR-number set, for Deep Search results) -----
def test_numbers_restricts_to_set():
    assert filters.matches(_row(number=42), {"numbers": [7, 42, 99]})
    assert not filters.matches(_row(number=5), {"numbers": [7, 42, 99]})
    assert not filters.matches(_row(number=5), {"numbers": []})  # empty set matches nothing


# --- pain filter (community pain score) -----------------------------------
def test_pain_numeric_compare():
    high = _row(pain_score=3.5)
    assert filters.matches(high, {"pain": {"op": ">", "value": 2.0}})
    assert not filters.matches(high, {"pain": {"op": ">", "value": 5.0}})
    assert filters.matches(high, {"pain": {"op": "<", "value": 5.0}})
    assert not filters.matches(high, {"pain": {"op": "<", "value": 2.0}})


def test_pain_missing_treated_as_zero():
    # no community-pain signal ("—" in the UI): counts as 0, so it's below any
    # positive threshold and never above one
    assert filters.matches(_row(pain_score=None), {"pain": {"op": "<", "value": 1}})
    assert filters.matches(_row(), {"pain": {"op": "<", "value": 1}})
    assert not filters.matches(_row(pain_score=None), {"pain": {"op": ">", "value": 0}})
    assert not filters.matches(_row(), {"pain": {"op": ">", "value": 0}})


# --- author_rate filter (historical merge rate, 0–1 decimal) ---------------
def test_author_rate_numeric_compare():
    high = _row(author_stats={"handle": "alice", "url": "", "merge_rate": 0.8})
    assert filters.matches(high, {"author_rate": {"op": ">", "value": 0.5}})
    assert not filters.matches(high, {"author_rate": {"op": ">", "value": 0.9}})
    assert filters.matches(high, {"author_rate": {"op": "<", "value": 0.9}})
    assert not filters.matches(high, {"author_rate": {"op": "<", "value": 0.5}})


def test_author_rate_missing_treated_as_zero():
    # no author_stats at all: treated as 0% — below any positive threshold,
    # never above one
    r = _row()
    assert filters.matches(r, {"author_rate": {"op": "<", "value": 0.8}})
    assert not filters.matches(r, {"author_rate": {"op": ">", "value": 0}})
    # author_stats present but merge_rate absent (new author, no decided PRs yet)
    bob = _row(author_stats={"handle": "bob", "url": "", "merge_rate": None})
    assert filters.matches(bob, {"author_rate": {"op": "<", "value": 0.8}})
    assert not filters.matches(bob, {"author_rate": {"op": ">", "value": 0}})


# --- multi-value (array) enum filters -------------------------------------

def test_disposition_array_matches_any_member():
    r = _row(disposition="close-dup")
    assert filters.matches(r, {"disposition": ["close-dup", "close-stale"]})
    assert not filters.matches(r, {"disposition": ["close-fixed", "close-stale"]})
    # single-string form still works
    assert filters.matches(r, {"disposition": "close-dup"})
    assert not filters.matches(r, {"disposition": "close-stale"})


def test_safety_array_matches_any_member():
    r_green = _row(safety="GREEN", safety_fresh=True)
    r_red = _row(safety="RED", safety_fresh=True)
    r_stale = _row(safety="GREEN", safety_fresh=False)
    assert filters.matches(r_green, {"safety": ["GREEN", "YELLOW"]})
    assert not filters.matches(r_red, {"safety": ["GREEN", "YELLOW"]})
    assert filters.matches(r_red, {"safety": ["GREEN", "RED"]})
    # "not-run" can appear alongside concrete values in the list
    assert filters.matches(r_stale, {"safety": ["GREEN", "not-run"]})
    assert not filters.matches(r_green, {"safety": ["YELLOW", "not-run"]})


def test_drift_array_matches_any_member():
    r = _row(drift_state="conflicts")
    assert filters.matches(r, {"drift": ["conflicts", "applicable"]})
    assert not filters.matches(r, {"drift": ["applicable", "already-fixed"]})


def test_ci_array_matches_any_member():
    r = _row(signals={**_row()["signals"], "ci": "failing"})
    assert filters.matches(r, {"ci": ["failing", "unknown"]})
    assert not filters.matches(r, {"ci": ["passing", "unknown"]})


def _checks_row(*checks):
    return _row(checks={"checks": [{"key": k, "status": s} for k, s in checks], "passed": 0, "total": 0})


def test_checks_clause_matches_pass():
    r = _checks_row(("security", "pass"))
    assert filters.matches(r, {"checks": [{"key": "security", "status": "pass"}]})
    assert not filters.matches(r, {"checks": [{"key": "security", "status": "fail"}]})


def test_checks_clause_warn_counts_as_fail():
    # "warn" (a caution short of a clean pass, e.g. a stale verdict) is not a
    # clean pass, so the "failed" bucket matches it.
    r = _checks_row(("verify", "warn"))
    assert filters.matches(r, {"checks": [{"key": "verify", "status": "fail"}]})
    assert not filters.matches(r, {"checks": [{"key": "verify", "status": "pass"}]})


def test_checks_clause_na_status_is_never_ran():
    r = _checks_row(("ci", "na"))
    assert filters.matches(r, {"checks": [{"key": "ci", "status": "never_ran"}]})


def test_checks_clause_missing_key_is_never_ran():
    # A check absent from the row's rollup entirely (the phase never ran) is
    # indistinguishable from an explicit "na" status for filtering purposes.
    r = _checks_row(("security", "pass"))
    assert filters.matches(r, {"checks": [{"key": "verify", "status": "never_ran"}]})
    assert not filters.matches(r, {"checks": [{"key": "verify", "status": "pass"}]})


def test_checks_clauses_and_together():
    r = _checks_row(("mergeable", "pass"), ("security", "pass"))
    spec = {"checks": [
        {"key": "mergeable", "status": "pass"},
        {"key": "security", "status": "pass"},
        {"key": "verify", "status": "never_ran"},
    ]}
    assert filters.matches(r, spec)
    r2 = _checks_row(("mergeable", "pass"), ("security", "fail"))
    assert not filters.matches(r2, spec)


def test_checks_clause_status_list_ors_within_clause():
    r = _checks_row(("review", "warn"))
    assert filters.matches(r, {"checks": [{"key": "review", "status": ["pass", "fail"]}]})
    assert filters.matches(_checks_row(("review", "pass")), {"checks": [{"key": "review", "status": ["pass", "fail"]}]})


def test_responses_array_matches_any_member():
    resp = {"reopened": True, "new_commits": False, "replied": False, "resubmitted": False}
    r = _row(responses=resp)
    assert filters.matches(r, {"responses": ["reopened", "new_commits"]})
    assert not filters.matches(r, {"responses": ["new_commits", "replied"]})
    # "any" in the list matches whenever a response exists
    assert filters.matches(r, {"responses": ["any", "new_commits"]})
    # no response at all → never matches an array filter
    assert not filters.matches(_row(responses=None), {"responses": ["reopened", "new_commits"]})


# --- acked response signals leave the responses filter ----------------------
# An acked signal still renders (muted) in the Updated column but stops
# demanding attention, so it must not match the responses filter.
_ACK = {"at": "2026-07-15T00:00:00+00:00", "by": "Ada"}


def _resp(ack=None):
    return {"replied": True, "reopened": False, "new_commits": False,
            "resubmitted": False, "ack": ack}


def test_unacked_response_matches():
    assert filters.matches(_row(responses=_resp()), {"responses": "any"}) is True


def test_acked_response_does_not_match_any():
    assert filters.matches(_row(responses=_resp(_ACK)), {"responses": "any"}) is False


def test_acked_response_does_not_match_a_specific_signal():
    assert filters.matches(_row(responses=_resp(_ACK)), {"responses": "replied"}) is False
