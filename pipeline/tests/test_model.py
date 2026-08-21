import pytest

from pipeline import freshness
from pipeline import store as S
from pipeline.store import Store


def _base_pr(n=1, head="sha1"):
    return {"pr": n, "meta": {"title": "t", "state": "open", "head_sha": head}}


def _seed(tmp_path, rec):
    st = S.Store(tmp_path)
    st.save_pr(rec)
    return st


def test_route_to_writes_analysis(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).route_to("request-changes", "needs work", asks=["fix tests"])
    a = st.load_pr(1).section("analysis")
    assert a["disposition"] == "request-changes"
    assert a["rationale"] == "needs work"
    assert a["asks"] == ["fix tests"]
    assert a["against_head_sha"] == "sha1"
    assert "checked_at" in a


def test_route_to_merge_stays_merge_when_clean(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).route_to("merge", "clean")
    assert st.load_pr(1).section("analysis")["disposition"] == "merge"


def test_route_to_merge_reads_needs_human_on_red(tmp_path):
    rec = _base_pr()
    rec["security"] = {"verdict": "RED", "findings": [{"title": "authz bypass"}],
                       "override": None, "checked_at": "2026-06-20T00:00:00+00:00",
                       "against_head_sha": "sha1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    pr = st.load_pr(1)
    assert pr.section("analysis")["disposition"] == "merge"   # stored verbatim
    assert pr.disposition == "needs-human"                    # derived route
    assert "authz bypass" in pr.rationale


def test_route_to_merge_reads_request_changes_on_yellow(tmp_path):
    rec = _base_pr()
    rec["security"] = {"verdict": "YELLOW", "findings": [{"title": "weak check"}],
                       "override": None, "checked_at": "2026-06-20T00:00:00+00:00",
                       "against_head_sha": "sha1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    assert st.load_pr(1).disposition == "request-changes"


def test_route_to_merge_survives_overridden_red(tmp_path):
    rec = _base_pr()
    rec["security"] = {"verdict": "RED", "findings": [],
                       "override": {"reason": "fp", "by": "op"},
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "clean")
    assert st.load_pr(1).disposition == "merge"


def test_route_to_merge_survives_stale_red(tmp_path):
    rec = _base_pr(head="sha2")  # current head is sha2
    rec["security"] = {"verdict": "RED", "findings": [{"title": "x"}],
                       "override": None, "checked_at": "2026-06-20T00:00:00+00:00",
                       "against_head_sha": "sha1"}  # stale: stamped against sha1
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "clean")
    assert st.load_pr(1).disposition == "merge"  # stale → no route derived


def _verify(outcome, head="sha1", **over):
    return dict({"outcome": outcome, "signals": {}, "findings": [], "tier": 0,
                 "against_base_sha": "base1", "checked_at": "2026-06-20T00:00:00+00:00",
                 "against_head_sha": head}, **over)


def test_route_to_merge_reads_needs_human_on_escalate(tmp_path):
    rec = _base_pr()
    rec["verify"] = _verify("escalate")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    pr = st.load_pr(1)
    assert pr.disposition == "needs-human"
    assert "escalated" in pr.rationale


def test_route_to_merge_reads_request_changes_on_not_verified(tmp_path):
    rec = _base_pr()
    rec["verify"] = _verify("not-verified")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    assert st.load_pr(1).disposition == "request-changes"


def test_route_to_merge_survives_a_verified_fix(tmp_path):
    rec = _base_pr()
    rec["verify"] = _verify("verified-fix")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "clean")
    assert st.load_pr(1).disposition == "merge"


def test_route_to_merge_survives_a_null_outcome(tmp_path):
    # The blind-committed state: judged for adequacy, not yet run. It forces no route.
    rec = _base_pr()
    rec["verify"] = _verify(None)
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "clean")
    assert st.load_pr(1).disposition == "merge"


def test_route_to_merge_survives_stale_escalate(tmp_path):
    rec = _base_pr(head="sha2")                     # current head is sha2
    rec["verify"] = _verify("escalate", head="sha1")  # stale: stamped against sha1
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "clean")
    assert st.load_pr(1).disposition == "merge"


def test_route_to_merge_stores_merge_only_fields_through_a_verify_route(tmp_path):
    # The stored section is ANALYZE's verdict verbatim — the verify route shows
    # only in the derived read, and clears with the outcome.
    rec = _base_pr()
    rec["verify"] = _verify("needs-rebase")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "clean", asks=["x"], upstream_pr=42)
    pr = st.load_pr(1)
    a = pr.section("analysis")
    assert a["disposition"] == "merge"
    assert a["asks"] == ["x"] and a["upstream_pr"] == 42
    assert pr.disposition == "request-changes"
    assert "Rebase onto main" in pr.rationale


def test_route_to_merge_takes_the_more_blocking_of_verify_and_security(tmp_path):
    # YELLOW alone routes to request-changes; escalate alone to needs-human. The
    # most-blocking wins by gates.DISPOSITION_PRECEDENCE, so verify decides here.
    rec = _base_pr()
    rec["security"] = {"verdict": "YELLOW", "findings": [{"title": "weak check"}],
                       "override": None, "checked_at": "2026-06-20T00:00:00+00:00",
                       "against_head_sha": "sha1"}
    rec["verify"] = _verify("escalate")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    pr = st.load_pr(1)
    assert pr.disposition == "needs-human"
    assert "Dynamic verification escalated" in pr.rationale


def test_route_to_merge_prefers_security_when_both_force_the_same_route(tmp_path):
    # RED and escalate both force needs-human. The tie is broken deterministically
    # toward security — the higher-stakes finding.
    rec = _base_pr()
    rec["security"] = {"verdict": "RED", "findings": [{"title": "authz bypass"}],
                       "override": None, "checked_at": "2026-06-20T00:00:00+00:00",
                       "against_head_sha": "sha1"}
    rec["verify"] = _verify("escalate")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    pr = st.load_pr(1)
    assert pr.disposition == "needs-human"
    assert "authz bypass" in pr.rationale


def test_route_to_merge_takes_security_over_a_lesser_verify_route(tmp_path):
    # The other direction: RED (needs-human) outranks not-verified (request-changes).
    rec = _base_pr()
    rec["security"] = {"verdict": "RED", "findings": [{"title": "authz bypass"}],
                       "override": None, "checked_at": "2026-06-20T00:00:00+00:00",
                       "against_head_sha": "sha1"}
    rec["verify"] = _verify("not-verified")
    st = _seed(tmp_path, rec)
    st.edit_pr(1).route_to("merge", "looks clean")
    pr = st.load_pr(1)
    assert pr.disposition == "needs-human"
    assert "authz bypass" in pr.rationale


def test_edit_pr_missing_raises(tmp_path):
    st = S.Store(tmp_path)
    with pytest.raises(KeyError):
        st.edit_pr(999)


def _merge_pr(n=1, head="sha1"):
    rec = _base_pr(n, head)
    rec["analysis"] = {"disposition": "merge", "rationale": "clean",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": head}
    return rec


def test_record_security_red_reads_needs_human(tmp_path):
    st = _seed(tmp_path, _merge_pr())
    st.edit_pr(1).record_security("RED", [{"title": "RCE in handler"}])
    rec = st.load_pr(1)
    assert rec.section("security")["verdict"] == "RED"
    assert rec.section("analysis")["disposition"] == "merge"   # stored verbatim
    assert rec.disposition == "needs-human"                    # derived route
    assert "RCE in handler" in rec.rationale


def test_record_security_yellow_reads_request_changes(tmp_path):
    st = _seed(tmp_path, _merge_pr())
    st.edit_pr(1).record_security("YELLOW", [{"title": "weak input check"}])
    rec = st.load_pr(1)
    assert rec.section("security")["verdict"] == "YELLOW"
    assert rec.disposition == "request-changes"


def test_record_security_green_leaves_merge(tmp_path):
    st = _seed(tmp_path, _merge_pr())
    st.edit_pr(1).record_security("GREEN", [])
    rec = st.load_pr(1)
    assert rec.section("security")["verdict"] == "GREEN"
    assert rec.disposition == "merge"


def test_log_security_override_annotates_verdict_in_place(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_security("YELLOW", [{"title": "drops log fields"}])
    before = st.load_pr(1).section("security")
    st.edit_pr(1).log_security_override("mirrors master's stdout ignore list", by="operator")
    sec = st.load_pr(1).section("security")
    assert sec["override"]["reason"] == "mirrors master's stdout ignore list"
    assert sec["override"]["by"] == "operator"
    assert sec["override"]["at"]
    # the override annotates the verdict: same recency stamp, same head, same verdict
    assert sec["checked_at"] == before["checked_at"]
    assert sec["against_head_sha"] == before["against_head_sha"]
    assert sec["verdict"] == "YELLOW"


def test_log_security_override_requires_reason(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_security("YELLOW", [{"title": "x"}])
    with pytest.raises(ValueError):
        st.edit_pr(1).log_security_override("   ")


def test_log_security_override_requires_a_verdict(tmp_path):
    st = _seed(tmp_path, _base_pr())
    with pytest.raises(ValueError):
        st.edit_pr(1).log_security_override("no verdict to override")


def test_rerecord_security_clears_override(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).record_security("YELLOW", [{"title": "x"}])
    st.edit_pr(1).log_security_override("operator judgment")
    st.edit_pr(1).record_security("YELLOW", [{"title": "x"}])
    assert st.load_pr(1).section("security")["override"] is None


def test_record_security_non_merge_keeps_disposition(tmp_path):
    rec = _base_pr()
    rec["analysis"] = {"disposition": "close-fixed", "rationale": "x",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(1).record_security("RED", [{"title": "x"}])
    rec = st.load_pr(1)
    assert rec.section("security")["verdict"] == "RED"
    assert rec.section("analysis")["disposition"] == "close-fixed"


def test_record_security_leaves_the_analysis_stamp_alone(tmp_path):
    # The analysis section is ANALYZE's — recording a verdict touches only the
    # security section; the route shows in the derived read.
    rec = _base_pr(head="sha2")                       # current head is sha2
    rec["analysis"] = {"disposition": "merge", "rationale": "clean",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(1).record_security("RED", [{"title": "x"}], head_sha="sha2")
    pr = st.load_pr(1)
    a = pr.section("analysis")
    assert a["disposition"] == "merge" and a["against_head_sha"] == "sha1"
    assert pr.disposition == "needs-human"


def test_mark_standalone_stamps_empty_ids(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).mark_standalone()
    cl = st.load_pr(1).section("cluster")
    assert cl["ids"] == []
    assert cl["against_head_sha"] == "sha1" and "checked_at" in cl
    assert st.load_pr(1).cluster_ids == []
    assert st.load_pr(1).primary_cluster_id is None


def _seed_cluster(tmp_path, members):
    st = S.Store(tmp_path)
    for n in members:
        st.save_pr(_base_pr(n))
    st.save_cluster({"id": 7, "root_problem": "rp", "prs": [], "outcome": None,
                     "checked_at": "2026-06-20T00:00:00+00:00"})
    return st


def test_set_outcome(tmp_path):
    st = _seed_cluster(tmp_path, [])
    st.edit_cluster(7).set_outcome("awaiting-authors")
    assert st.load_cluster(7).outcome == "awaiting-authors"
    st.edit_cluster(7).set_outcome(None)
    assert st.load_cluster(7).outcome is None


def test_add_member_links_both_sides(tmp_path):
    st = _seed_cluster(tmp_path, [1, 2])
    cl = st.edit_cluster(7)
    cl.add_member(1)
    cl.add_member(2)
    assert st.load_cluster(7).prs == [1, 2]
    assert st.load_pr(1).cluster_ids == [7]
    assert st.load_pr(2).cluster_ids == [7]


def test_add_member_idempotent(tmp_path):
    st = _seed_cluster(tmp_path, [1])
    st.edit_cluster(7).add_member(1)
    st.edit_cluster(7).add_member(1)
    assert st.load_cluster(7).prs == [1]


def test_remove_last_membership_leaves_empty_ids(tmp_path):
    st = _seed_cluster(tmp_path, [1])
    st.edit_cluster(7).add_member(1)
    st.edit_cluster(7).remove_member(1)
    assert st.load_pr(1).cluster_ids == []           # standalone, not absent
    assert st.load_pr(1).section("cluster")["ids"] == []


def test_remove_member_leaves_foreign_backref(tmp_path):
    st = _seed_cluster(tmp_path, [1])
    rec = st.load_pr(1)
    rec.raw["cluster"] = {"ids": [99], "checked_at": "2026-06-20T00:00:00+00:00",
                          "against_head_sha": "sha1"}
    st.save_pr(rec)
    st.edit_cluster(7).remove_member(1)              # 7 not in [99] → untouched
    assert st.load_pr(1).cluster_ids == [99]


def test_pr_in_multiple_clusters(tmp_path):
    st = _seed_cluster(tmp_path, [1])
    st.save_cluster({"id": 9, "root_problem": "rp2", "prs": [], "outcome": None,
                     "checked_at": "2026-06-20T00:00:00+00:00"})
    st.edit_cluster(7).add_member(1)
    st.edit_cluster(9).add_member(1)
    assert st.load_pr(1).cluster_ids == [7, 9]
    assert st.load_pr(1).primary_cluster_id == 7
    assert st.load_cluster(7).prs == [1] and st.load_cluster(9).prs == [1]


def test_remove_member_leaves_other_memberships(tmp_path):
    st = _seed_cluster(tmp_path, [1])
    st.save_cluster({"id": 9, "root_problem": "rp2", "prs": [], "outcome": None,
                     "checked_at": "2026-06-20T00:00:00+00:00"})
    st.edit_cluster(7).add_member(1)
    st.edit_cluster(9).add_member(1)
    st.edit_cluster(7).remove_member(1)
    assert st.load_pr(1).cluster_ids == [9]          # only 7 removed
    assert st.load_cluster(7).prs == []


def test_cluster_proposals_roundtrip(tmp_path):
    st = _seed_cluster(tmp_path, [1, 2])
    rows = [{"pr": 1, "disposition": "merge", "rationale": "a"},
            {"pr": 2, "disposition": "close-stale", "rationale": "b"}]
    st.edit_cluster(7).set_proposals(rows)
    c = st.load_cluster(7)
    assert c.proposals == rows
    assert c.proposal_for(1)["disposition"] == "merge"
    assert c.proposal_for(999) is None


def test_route_to_records_from_cluster(tmp_path):
    st = _seed(tmp_path, _base_pr())
    st.edit_pr(1).route_to("merge", "ok", from_cluster=42)
    assert st.load_pr(1).raw["analysis"]["from_cluster"] == 42


def test_validate_allows_merge_with_current_red_security():
    # Stored merge + a blocking fact is the canonical representation — the route
    # derives on read (see test_derived_disposition).
    rec = _base_pr()
    rec["analysis"] = {"disposition": "merge", "rationale": "r",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    rec["security"] = {"verdict": "RED", "findings": [], "override": None,
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    S.validate_pr(rec)


def test_validate_allows_merge_with_green_security():
    rec = _base_pr()
    rec["analysis"] = {"disposition": "merge", "rationale": "r",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    rec["security"] = {"verdict": "GREEN", "findings": [], "override": None,
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    S.validate_pr(rec)


def test_validate_allows_merge_with_overridden_red():
    rec = _base_pr()
    rec["analysis"] = {"disposition": "merge", "rationale": "r",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    rec["security"] = {"verdict": "RED", "findings": [], "override": "approved by op",
                       "checked_at": "2026-06-20T00:00:00+00:00", "against_head_sha": "sha1"}
    S.validate_pr(rec)


def test_pr_read_properties(tmp_path):
    rec = _base_pr(5, "h1")
    rec["meta"].update({"author": "alice", "url": "u", "draft": False})
    rec["signals"] = {"ci": "passing", "mergeable": True,
                      "checked_at": "t", "against_head_sha": "h1"}
    rec["reviews"] = {"greptile": {"kind": "review", "score": 5, "reviewed_sha": "h1"},
                      "checked_at": "t", "against_head_sha": "h1"}
    rec["analysis"] = {"disposition": "merge", "rationale": "ok", "asks": ["a"],
                       "checked_at": "t", "against_head_sha": "h1"}
    rec["security"] = {"verdict": "GREEN", "findings": [{"title": "f"}], "override": None,
                       "checked_at": "t", "against_head_sha": "h1"}
    rec["drift"] = {"state": "applicable", "checked_at": "t", "against_head_sha": "h1"}
    rec["threat"] = {"verdict": "clear", "signatures": [], "checked_at": "t", "against_head_sha": "h1"}
    rec["cluster"] = {"ids": [9], "checked_at": "t", "against_head_sha": "h1"}
    rec["issues"] = {"linked": [101], "checked_at": "t", "against_head_sha": "h1"}
    st = _seed(tmp_path, rec)
    pr = st.edit_pr(5)
    assert pr.number == 5
    assert pr.state == "open" and pr.head_sha == "h1" and pr.draft is False
    assert pr.author == "alice" and pr.url == "u"
    assert pr.greptile == 5 and pr.ci == "passing" and pr.mergeable is True
    assert pr.disposition == "merge" and pr.rationale == "ok" and pr.asks == ["a"]
    assert pr.security_verdict == "GREEN" and pr.findings == [{"title": "f"}]
    assert pr.security_override is None
    assert pr.drift_state == "applicable"
    assert pr.threat_verdict == "clear" and pr.threat_signatures == []
    assert pr.cluster_ids == [9] and pr.linked_issues == [101]
    assert pr.section("analysis")["disposition"] == "merge"
    assert pr.section("nonesuch") is None


def test_pr_section_returns_dict_or_none(tmp_path):
    """section() is the only sanctioned sub-dict accessor; shim is gone."""
    rec = _base_pr(20, "h1")
    rec["analysis"] = {"disposition": "merge", "rationale": "ok",
                       "checked_at": "t", "against_head_sha": "h1"}
    pr = _seed(tmp_path, rec).edit_pr(20)
    assert pr.section("analysis")["rationale"] == "ok"
    assert pr.section("meta")["title"] == "t"
    assert pr.section("security") is None   # absent → None, not KeyError


def test_pr_read_properties_none_safe(tmp_path):
    pr = _seed(tmp_path, _base_pr(6, "h1")).edit_pr(6)
    assert pr.disposition is None and pr.rationale is None and pr.asks is None
    assert pr.security_verdict is None and pr.findings == []
    assert pr.drift_state is None and pr.threat_verdict is None
    assert pr.threat_signatures == [] and pr.linked_issues == []
    assert pr.cluster_ids == [] and pr.draft is False


def test_cluster_read_properties(tmp_path):
    st = _seed_cluster(tmp_path, [1, 2])
    st.edit_cluster(7).add_member(1)
    cl = st.edit_cluster(7)
    assert cl.id == 7 and cl.prs == [1] and cl.root_problem == "rp"
    assert cl.outcome is None
    st.edit_cluster(7).set_outcome("awaiting-authors")
    cl2 = st.edit_cluster(7)
    assert cl2.outcome == "awaiting-authors"
    assert cl2.prs == [1] and cl2.root_problem == "rp"


def test_pr_base_property(tmp_path):
    rec = _base_pr(11, "h1")
    rec["meta"]["base"] = "main"
    pr = _seed(tmp_path, rec).edit_pr(11)
    assert pr.base == "main"
    assert _seed(tmp_path, _base_pr(12, "h1")).edit_pr(12).base is None


def test_record_live_state_persists_state_and_mergeable(tmp_path):
    rec = _base_pr(13, "h1")
    rec["signals"] = {"greptile": 5, "ci": "passing", "mergeable": True,
                      "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "h1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(13).record_live_state(state="closed", mergeable=False)
    reloaded = st.load_pr(13)
    assert reloaded.state == "closed"                              # meta.state persisted
    assert reloaded.mergeable is False                            # signals.mergeable persisted
    assert reloaded.section("signals")["against_head_sha"] == "h1"  # freshness stamp kept
    assert reloaded.section("signals")["greptile"] == 5           # other signals untouched


def test_record_live_state_state_only_leaves_signals(tmp_path):
    rec = _base_pr(14, "h1")
    rec["signals"] = {"mergeable": True, "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": "h1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(14).record_live_state(state="merged")   # no mergeable arg
    reloaded = st.load_pr(14)
    assert reloaded.state == "merged"
    assert reloaded.mergeable is True                  # signals section untouched


def test_record_live_state_persists_diffstat_and_has_tests(tmp_path):
    rec = _base_pr(15, "h1")
    rec["signals"] = {"greptile": 5, "mergeable": True,
                      "checked_at": "2026-06-10T00:00:00+00:00", "against_head_sha": "h1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(15).record_live_state(
        diffstat={"additions": 10, "deletions": 2, "changed_files": 3}, has_tests=True)
    sig = st.load_pr(15).section("signals")
    assert sig["diffstat"] == {"additions": 10, "deletions": 2, "changed_files": 3}
    assert sig["has_tests"] is True
    assert sig["against_head_sha"] == "h1"   # freshness stamp kept
    assert sig["greptile"] == 5              # other signals untouched


def test_record_live_state_persists_ci(tmp_path):
    rec = _base_pr(16, "h1")
    rec["signals"] = {"greptile": 5, "mergeable": True,
                      "checked_at": "2026-06-10T00:00:00+00:00",
                      "against_head_sha": "h1"}
    st = _seed(tmp_path, rec)

    st.edit_pr(16).record_live_state(ci="passing")

    sig = st.load_pr(16).section("signals")
    assert sig["ci"] == "passing"
    assert sig["against_head_sha"] == "h1"
    assert sig["greptile"] == 5


def test_cluster_set_members_reconciles(tmp_path):
    st = _seed_cluster(tmp_path, [1, 2, 3])
    cl = st.edit_cluster(7)
    cl.set_members([1, 2])
    assert st.load_cluster(7).prs == [1, 2]
    assert st.load_pr(1).cluster_ids == [7] and st.load_pr(2).cluster_ids == [7]
    st.edit_cluster(7).set_members([2, 3])
    assert st.load_cluster(7).prs == [2, 3]
    assert st.load_pr(1).cluster_ids == []              # removed from cluster 7
    assert st.load_pr(3).cluster_ids == [7]             # backref wired
    st.edit_cluster(7).set_members([2, 3])
    assert st.load_cluster(7).prs == [2, 3]             # idempotent


def test_cluster_set_root_problem(tmp_path):
    st = _seed_cluster(tmp_path, [])
    st.edit_cluster(7).set_root_problem("a clearer root cause")
    assert st.load_cluster(7).root_problem == "a clearer root cause"


def test_cluster_record_analysis(tmp_path):
    st = _seed_cluster(tmp_path, [])
    st.edit_cluster(7).record_analysis("merge-ready", "rationale text", "notes text")
    cl = st.load_cluster(7)
    assert cl.outcome == "merge-ready"
    assert cl.rationale == "rationale text"
    assert cl.notes == "notes text"
    assert "checked_at" in cl.raw


def test_pr_clear_cluster_removes_section(tmp_path):
    rec = _base_pr(1, "h1")
    rec["cluster"] = {"ids": [7], "checked_at": "t", "against_head_sha": "h1"}
    st = _seed(tmp_path, rec)
    st.edit_pr(1).clear_cluster()
    assert "cluster" not in st.load_pr(1).raw           # gone entirely, not {}


def test_store_create_cluster(tmp_path):
    from pipeline import store as S, model
    st = S.Store(tmp_path)
    cl = st.create_cluster(9, "root cause text")
    assert isinstance(cl, model.Cluster)
    assert cl.id == 9 and cl.root_problem == "root cause text"
    assert cl.prs == [] and cl.outcome is None
    on_disk = st.load_cluster(9)
    assert on_disk.id == 9 and on_disk.root_problem == "root cause text"
    assert "checked_at" in on_disk.raw
    st.create_cluster(10, "rp2", outcome="merge-ready")
    assert st.load_cluster(10).outcome == "merge-ready"


def test_pr_section_setters(tmp_path):
    st = _seed(tmp_path, _base_pr(1, "h1"))
    pr = st.edit_pr(1)
    pr.set_signals({"ci": "passing", "mergeable": True})
    pr.set_reviews({"greptile": {"kind": "review", "score": 5}})
    pr.set_drift({"state": "applicable"})
    pr.set_issues([101, 102])
    pr.set_threat({"verdict": "clear", "signatures": []})
    pr.set_summary({"subsystem": "auth"}, head_sha="h1")
    rec = st.load_pr(1)
    assert rec.greptile == 5 and rec.drift_state == "applicable"
    assert rec.linked_issues == [101, 102]
    assert rec.threat_verdict == "clear"
    assert rec.section("summary")["subsystem"] == "auth"
    for s in ("signals", "drift", "issues", "threat", "summary"):
        assert rec.section(s)["against_head_sha"] == "h1"
        assert "checked_at" in rec.section(s)


def test_apply_facts_single_save_writes_all_sections(tmp_path, monkeypatch):
    st = _seed(tmp_path, _base_pr(1, "h1"))
    saves: list[int] = []
    orig = st.save_pr
    monkeypatch.setattr(st, "save_pr", lambda rec: (saves.append(1), orig(rec))[1])
    st.edit_pr(1).apply_facts(
        {"title": "t", "state": "open", "head_sha": "h1"},
        signals={"ci": "passing"},
        drift={"state": "applicable"},
        issues=[101, 102],
        reviews={"greptile": {"kind": "review", "score": 5}},
    )
    assert len(saves) == 1  # one write for the whole ingest, not one per section
    rec = st.load_pr(1)
    assert rec.greptile == 5
    assert rec.drift_state == "applicable"
    assert rec.linked_issues == [101, 102]
    for s in ("signals", "drift", "issues"):
        assert rec.section(s)["against_head_sha"] == "h1"
        assert "checked_at" in rec.section(s)


def test_apply_facts_meta_only_leaves_other_sections_absent(tmp_path):
    st = _seed(tmp_path, _base_pr(1, "h1"))
    st.edit_pr(1).apply_facts({"title": "t2", "state": "open", "head_sha": "h2"})
    rec = st.load_pr(1)
    assert rec.section("meta")["head_sha"] == "h2"
    assert rec.section("signals") is None
    assert rec.section("drift") is None
    assert rec.section("issues") is None


def test_stage_facts_defers_persistence_for_bulk_save(tmp_path):
    st = _seed(tmp_path, _base_pr(1, "h1"))
    pr = st.edit_pr(1)
    pr.stage_facts({"title": "staged", "state": "open", "head_sha": "h2"})
    assert st.load_pr(1).head_sha == "h1"

    st.save_prs_many([pr])
    assert st.load_pr(1).head_sha == "h2"


def test_pr_set_meta_no_against_head_sha(tmp_path):
    st = _seed(tmp_path, _base_pr(1, "h1"))
    st.edit_pr(1).set_meta({"title": "t2", "state": "closed", "head_sha": "h2"})
    meta = st.load_pr(1).section("meta")
    assert meta["state"] == "closed" and meta["head_sha"] == "h2"
    assert "checked_at" in meta and "against_head_sha" not in meta


def test_store_create_pr(tmp_path):
    from pipeline import store as S, model
    st = S.Store(tmp_path)
    pr = st.create_pr(5, {"title": "t", "state": "open", "head_sha": "h1"})
    assert isinstance(pr, model.Pr) and pr.number == 5 and pr.state == "open"
    on_disk = st.load_pr(5)
    assert on_disk.head_sha == "h1"
    assert "checked_at" in on_disk.section("meta")
    assert "against_head_sha" not in on_disk.section("meta")


def test_cluster_checked_at(tmp_path):
    st = _seed_cluster(tmp_path, [])
    st.edit_cluster(7).set_outcome("close-out")
    assert st.load_cluster(7).checked_at is not None


def test_greptile_stale_true_when_reviewed_sha_differs(tmp_path):
    rec = _base_pr(1, "headsha")
    st = _seed(tmp_path, rec)
    pr = st.edit_pr(1)
    pr.set_reviews({"greptile": {"kind": "review", "score": 3, "reviewed_sha": "oldsha"}})
    loaded = st.load_pr(1)
    assert loaded.greptile_reviewed_sha == "oldsha"
    assert loaded.greptile_stale is True


def test_greptile_stale_false_when_reviewed_sha_matches_head(tmp_path):
    rec = _base_pr(1, "abc123")
    st = _seed(tmp_path, rec)
    pr = st.edit_pr(1)
    pr.set_reviews({"greptile": {"kind": "review", "score": 5, "reviewed_sha": "abc123"}})
    loaded = st.load_pr(1)
    assert loaded.greptile_stale is False


def test_greptile_stale_none_when_reviewed_sha_absent(tmp_path):
    rec = _base_pr(1, "head1")
    st = _seed(tmp_path, rec)
    pr = st.edit_pr(1)
    pr.set_reviews({"greptile": {"kind": "review", "score": 4}})
    loaded = st.load_pr(1)
    assert loaded.greptile_reviewed_sha is None
    assert loaded.greptile_stale is None


def test_set_greptile_review_stamps_severity_in_section(tmp_path):
    st = S.Store(tmp_path)
    pr = st.create_pr(1, {"title": "t", "state": "open", "head_sha": "abc"})
    pr.set_signals({"greptile": 3})
    pr.set_greptile_review({"severity": "defects",
                            "findings": [{"headline": "h", "class": "substantive", "why": "w"}],
                            "summary": "s"})
    rec = st.load_pr(1)
    assert rec.greptile_review["severity"] == "defects"
    assert rec.greptile_review["against_head_sha"] == "abc"
    assert rec.greptile_severity == "defects"   # read from the section


def test_set_greptile_review_leaves_signals_untouched(tmp_path):
    st = S.Store(tmp_path)
    pr = st.create_pr(1, {"title": "t", "state": "open", "head_sha": "OLD"})
    pr.set_signals({"greptile": 3, "ci": "passing"})  # fetched against OLD head
    pr.set_meta({"title": "t", "state": "open", "head_sha": "NEW"})  # head moves
    moved = st.load_pr(1)
    assert freshness.is_current(moved, "signals") is False

    st.edit_pr(1).set_greptile_review({"severity": "defects", "findings": [], "summary": "s"},
                                       head_sha="OLD")

    rec = st.load_pr(1)
    assert freshness.is_current(rec, "signals") is False  # signals untouched
    assert rec.greptile_severity == "defects"  # read from the section


class TestRecordVerify:
    """The verify section is stamped with BOTH SHAs: the PR head (so an author
    push makes it stale) and the pinned base (so a re-pin re-queues it)."""

    def _pr(self, store, n=1, head="h1", disposition="merge"):
        rec = {"pr": n,
               "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                        "head_sha": head, "checked_at": "2026-06-10T00:00:00+00:00"},
               "analysis": {"disposition": disposition, "rationale": "r",
                            "checked_at": "2026-06-10T00:00:00+00:00",
                            "against_head_sha": head}}
        store.save_pr(rec)
        return store.edit_pr(n)

    def test_stamps_both_shas(self, tmp_path):
        s = Store(tmp_path)
        pr = self._pr(s)
        pr.record_verify("verified-fix", {"red_green": {"red_exit": 20}},
                         tier=1, base_sha="basesha", head_sha="h1")
        got = s.load_pr(1)
        assert got is not None
        assert got.verify_outcome == "verified-fix"
        assert got.verify_base_sha == "basesha"
        assert got.section("verify")["against_head_sha"] == "h1"
        assert got.section("verify")["tier"] == 1
        assert got.verify_signals["red_green"]["red_exit"] == 20

    def test_head_sha_defaults_to_meta(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s, head="h9").record_verify(
            "verified-fix", {}, base_sha="b")
        assert s.load_pr(1).section("verify")["against_head_sha"] == "h9"

    def test_null_outcome_is_the_blind_committed_state(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify(
            None, {"blind_adequacy": {"faithful": True}}, base_sha="b")
        got = s.load_pr(1)
        assert got.verify_outcome is None
        assert got.verify_signals["blind_adequacy"]["faithful"] is True

    def test_escalate_demotes_merge_to_needs_human(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("escalate", {}, base_sha="b")
        assert s.load_pr(1).disposition == "needs-human"

    def test_not_verified_demotes_merge_to_request_changes(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("not-verified", {}, base_sha="b")
        assert s.load_pr(1).disposition == "request-changes"

    def test_needs_rebase_demotes_merge_to_request_changes(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("needs-rebase", {}, base_sha="b")
        assert s.load_pr(1).disposition == "request-changes"

    def test_regressed_demotes_merge_to_request_changes(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("regressed", {}, base_sha="b")
        assert s.load_pr(1).disposition == "request-changes"

    def test_deps_touched_demotes_merge_to_needs_human(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("deps-touched", {}, base_sha="b")
        assert s.load_pr(1).disposition == "needs-human"

    def test_unverifiable_leaves_disposition_alone(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("unverifiable-no-test", {}, base_sha="b")
        assert s.load_pr(1).disposition == "merge"

    def test_verified_fix_leaves_disposition_alone(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s).record_verify("verified-fix", {}, base_sha="b")
        assert s.load_pr(1).disposition == "merge"

    def test_non_merge_disposition_is_untouched(self, tmp_path):
        s = Store(tmp_path)
        self._pr(s, disposition="close-stale").record_verify(
            "escalate", {}, base_sha="b")
        assert s.load_pr(1).disposition == "close-stale"


def test_greptile_accessors_read_reviews_section():
    from pipeline.model import Pr
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": "h2"},
                   "reviews": {"greptile": {"kind": "review", "score": 4, "reviewed_sha": "h1"}}})
    assert pr.greptile == 4 and pr.greptile_reviewed_sha == "h1" and pr.greptile_stale is True
    assert pr.review_entry("greptile")["score"] == 4 and pr.review_entry("socket") is None
    assert not hasattr(pr, "review_score")


def test_stage_facts_stamps_reviews():
    from pipeline.model import Pr
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": "h"}})
    pr.stage_facts({"title": "t", "state": "open", "head_sha": "h"}, reviews={"greptile": {"kind": "review"}})
    assert pr.rec["reviews"]["against_head_sha"] == "h" and "checked_at" in pr.rec["reviews"]
    assert "reviews" in freshness.SHA_BOUND
