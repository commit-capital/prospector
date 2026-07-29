"""The derived disposition layer: the stored analysis section holds ANALYZE's
verdict verbatim, and every consequence — a security verdict, a verify outcome,
the quality-gate merge bar — derives at read time through Pr.disposition /
Pr.rationale / Pr.asks. A fact that changes on re-run, override, or signal
refresh is reflected immediately, with nothing stale left in the store."""

from pipeline import store as S
from pipeline.store import Store


HEAD = "h1"
NOW = "2026-06-10T00:00:00+00:00"


def _clean_rec(n=1, head=HEAD):
    return {
        "pr": n,
        "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                 "head_sha": head, "checked_at": NOW},
        "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                    "has_tests": True, "checked_at": NOW, "against_head_sha": head},
        "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head},
    }


def _seed(tmp_path, rec=None) -> Store:
    st = S.Store(tmp_path)
    st.save_pr(rec or _clean_rec())
    return st


class TestVerifyConsequenceDerives:
    def test_not_verified_then_verified_fix_heals(self, tmp_path):
        # The #7524 class: a failed verify run must not leave a permanent mark.
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "solid fix for the release bug")
        st.edit_pr(1).record_verify("not-verified", {}, base_sha="b1")
        pr = st.load_pr(1)
        assert pr.disposition == "request-changes"
        assert "did not confirm the fix" in pr.rationale
        st.edit_pr(1).record_verify("verified-fix", {}, base_sha="b1")
        pr = st.load_pr(1)
        assert pr.disposition == "merge"
        assert pr.rationale == "solid fix for the release bug"

    def test_escalate_reads_needs_human(self, tmp_path):
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).record_verify("escalate", {}, base_sha="b1")
        assert st.load_pr(1).disposition == "needs-human"

    def test_verify_override_heals(self, tmp_path):
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).record_verify("escalate", {}, base_sha="b1")
        st.edit_pr(1).log_verify_override("harness defect, judged by hand", by="op")
        assert st.load_pr(1).disposition == "merge"

    def test_stored_analysis_section_is_untouched(self, tmp_path):
        # The store keeps ANALYZE's verdict; only the read is routed.
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "solid")
        st.edit_pr(1).record_verify("not-verified", {}, base_sha="b1")
        a = st.load_pr(1).section("analysis")
        assert a["disposition"] == "merge"
        assert a["rationale"] == "solid"


class TestSecurityConsequenceDerives:
    def test_yellow_then_green_heals(self, tmp_path):
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "clean refactor")
        st.edit_pr(1).record_security("YELLOW", [{"title": "weak input check"}])
        pr = st.load_pr(1)
        assert pr.disposition == "request-changes"
        assert "weak input check" in pr.rationale
        st.edit_pr(1).record_security("GREEN", [])
        pr = st.load_pr(1)
        assert pr.disposition == "merge"
        assert pr.rationale == "clean refactor"

    def test_red_reads_needs_human(self, tmp_path):
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).record_security("RED", [{"title": "authz bypass"}])
        pr = st.load_pr(1)
        assert pr.disposition == "needs-human"
        assert "authz bypass" in pr.rationale

    def test_security_override_heals(self, tmp_path):
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).record_security("RED", [{"title": "fp"}])
        st.edit_pr(1).log_security_override("false positive", by="op")
        assert st.load_pr(1).disposition == "merge"


class TestMergeBarDerives:
    def test_failing_ci_reads_request_changes_with_asks(self, tmp_path):
        rec = _clean_rec()
        rec["signals"]["ci"] = "failing"
        st = _seed(tmp_path, rec)
        st.edit_pr(1).route_to("merge", "good change")
        pr = st.load_pr(1)
        assert pr.disposition == "request-changes"
        assert pr.rationale == "good change"          # bar keeps the analyze rationale
        assert any("CI" in a for a in pr.asks or [])

    def test_signal_refresh_heals_bar_demotion(self, tmp_path):
        # The GitHub-refresh path: the live sweep rewrites signals in place.
        rec = _clean_rec()
        rec["signals"]["ci"] = "failing"
        st = _seed(tmp_path, rec)
        st.edit_pr(1).route_to("merge", "good change")
        assert st.load_pr(1).disposition == "request-changes"
        st.edit_pr(1).set_signals({"greptile": 5, "ci": "passing", "mergeable": True,
                                   "has_tests": True})
        pr = st.load_pr(1)
        assert pr.disposition == "merge"
        assert pr.asks is None

    def test_stale_signals_derive_nothing(self, tmp_path):
        # An unmeasured gate is not a failure: staleness routes to re-analysis via
        # is_current, never to a fabricated request-changes.
        rec = _clean_rec()
        st = _seed(tmp_path, rec)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).set_meta({"title": "t", "author": "a", "state": "open",
                                "draft": False, "head_sha": "h2"})
        assert st.load_pr(1).disposition == "merge"

    def test_bar_asks_append_to_stored_asks(self, tmp_path):
        rec = _clean_rec()
        rec["signals"]["mergeable"] = False
        st = _seed(tmp_path, rec)
        st.edit_pr(1).route_to("merge", "r", asks=["also rename the flag"])
        asks = st.load_pr(1).asks
        assert asks is not None and asks[0] == "also rename the flag"
        assert any("conflict" in a.lower() for a in asks[1:])


class TestMostBlockingWins:
    def test_red_outranks_bar(self, tmp_path):
        rec = _clean_rec()
        rec["signals"]["ci"] = "failing"
        st = _seed(tmp_path, rec)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).record_security("RED", [{"title": "x"}])
        assert st.load_pr(1).disposition == "needs-human"

    def test_yellow_rationale_with_bar_asks(self, tmp_path):
        rec = _clean_rec()
        rec["signals"]["ci"] = "failing"
        st = _seed(tmp_path, rec)
        st.edit_pr(1).route_to("merge", "r")
        st.edit_pr(1).record_security("YELLOW", [{"title": "weak check"}])
        pr = st.load_pr(1)
        assert pr.disposition == "request-changes"
        assert "weak check" in pr.rationale
        assert any("CI" in a for a in pr.asks or [])


class TestStoreInvariant:
    def test_stored_merge_with_current_red_saves(self, tmp_path):
        # Stored merge + a blocking fact is the canonical representation now;
        # the read derives the route, so the save must not be refused.
        rec = _clean_rec()
        rec["analysis"] = {"disposition": "merge", "rationale": "r",
                           "checked_at": NOW, "against_head_sha": HEAD}
        rec["security"] = {"verdict": "RED", "findings": [{"title": "x"}],
                           "override": None, "checked_at": NOW,
                           "against_head_sha": HEAD}
        st = S.Store(tmp_path)
        st.save_pr(rec)                     # must not raise
        assert st.load_pr(1).disposition == "needs-human"

    def test_non_merge_disposition_passes_through(self, tmp_path):
        st = _seed(tmp_path)
        st.edit_pr(1).route_to("close-stale", "inactive for a year")
        st.edit_pr(1).record_security("RED", [{"title": "x"}])
        pr = st.load_pr(1)
        assert pr.disposition == "close-stale"
        assert pr.rationale == "inactive for a year"
