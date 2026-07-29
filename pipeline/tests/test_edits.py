"""Named store_edit transforms (pipeline.edits)."""
from pipeline import edits


def _rec(disposition, rationale):
    return {"pr": 1,
            "meta": {"title": "t", "state": "open", "head_sha": "h1"},
            "analysis": {"disposition": disposition, "rationale": rationale,
                         "checked_at": "t", "against_head_sha": "h1"}}


class TestRestoreDemotedMergePicks:
    def test_restores_a_stored_verify_route(self):
        rec = _rec("request-changes",
                   "Dynamic verification did not confirm the fix — the test did not "
                   "reproduce the defect on pinned main, or did not pass after the fix.")
        out = edits.restore_demoted_merge_picks(rec)
        assert out is not None
        assert out["analysis"]["disposition"] == "merge"
        assert "restored" in out["analysis"]["rationale"]

    def test_restores_a_stored_security_route(self):
        rec = _rec("needs-human",
                   "Security review is RED — authz bypass. Not a merge candidate.")
        out = edits.restore_demoted_merge_picks(rec)
        assert out is not None and out["analysis"]["disposition"] == "merge"

    def test_leaves_a_genuine_analyze_verdict_alone(self):
        rec = _rec("request-changes", "The retry loop never backs off; add a cap.")
        assert edits.restore_demoted_merge_picks(rec) is None

    def test_leaves_non_routed_dispositions_alone(self):
        rec = _rec("close-dup", "Security review is RED — echoed text, wrong shape.")
        assert edits.restore_demoted_merge_picks(rec) is None

    def test_leaves_records_without_analysis_alone(self):
        assert edits.restore_demoted_merge_picks(
            {"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "h1"}}) is None
