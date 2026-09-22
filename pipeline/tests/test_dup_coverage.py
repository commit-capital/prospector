"""Concern-level dup coverage (#326): the coverage-map shape, the commit-time
sanity checks, the read-time close-dup demotion, and the fail-closed
unattended-close bar."""
from pipeline import analyze_driver as ad
from pipeline import gates
from pipeline.model import Pr
from pipeline.store import Store
from pipeline.testsupport import reviews_section

NOW = "2026-06-10T00:00:00+00:00"

COVERED = [{"label": "the fix", "paths": ["src/a.ts"], "coverage": "pr",
            "covered_by": 1, "evidence": "same hunk in #1"}]


def _rec(n=2, *, analysis=None, paths=None, diffstat=None, state="open", author="a"):
    rec = {"pr": n, "meta": {"title": f"t{n}", "author": author, "state": state,
                             "draft": False, "head_sha": "h1", "checked_at": NOW}}
    signals: dict = {"ci": "passing", "mergeable": True,
                     "checked_at": NOW, "against_head_sha": "h1"}
    if diffstat is not None:
        signals["diffstat"] = diffstat
    rec["signals"] = signals
    if paths is not None:
        rec["summary"] = {"one_liner": "x", "subsystem": "ui", "mechanism": "m",
                          "identifiers": [], "paths": paths, "primary_change": "p",
                          "secondary_changes": [], "checked_at": NOW,
                          "against_head_sha": "h1"}
    if analysis is not None:
        rec["analysis"] = dict(analysis, checked_at=NOW, against_head_sha="h1")
    return rec


def _pr_view(**kw) -> Pr:
    return Pr(None, _rec(**kw))


class TestConcernErrors:
    def test_valid_map(self):
        assert gates.concern_errors(COVERED) == []
        assert gates.concern_errors([{"label": "x", "coverage": "landed",
                                      "landed_sha": "abc123"}]) == []
        assert gates.concern_errors([{"label": "x", "coverage": "unique"}]) == []

    def test_empty_or_non_list(self):
        assert gates.concern_errors(None) == ["concerns: must be a non-empty list"]
        assert gates.concern_errors([]) == ["concerns: must be a non-empty list"]
        assert gates.concern_errors("nope") == ["concerns: must be a non-empty list"]

    def test_shape_errors(self):
        errs = gates.concern_errors([
            {"coverage": "pr"},                       # no label, no covered_by
            {"label": "y", "coverage": "landed"},     # no landed_sha
            {"label": "z", "coverage": "elsewhere"},  # bad coverage
            {"label": "w", "coverage": "unique", "paths": "src"},  # paths not a list
        ])
        assert any("label" in e for e in errs)
        assert any("covered_by" in e for e in errs)
        assert any("landed_sha" in e for e in errs)
        assert any("coverage" in e for e in errs)
        assert any("paths" in e for e in errs)


class TestSanityTrips:
    def test_comparable_twins_have_no_trips(self):
        dup = _pr_view(paths=["src/a.ts"], diffstat={"additions": 5, "deletions": 1, "changed_files": 1})
        canon = _pr_view(n=1, paths=["src/a.ts"], diffstat={"additions": 6, "deletions": 2, "changed_files": 1})
        assert gates.dup_sanity_trips(dup, canon) == []

    def test_much_larger_dup_trips(self):
        dup = _pr_view(paths=["src/a.ts"], diffstat={"additions": 400, "deletions": 100, "changed_files": 1})
        canon = _pr_view(n=1, paths=["src/a.ts"], diffstat={"additions": 10, "deletions": 2, "changed_files": 1})
        assert any("lines" in t for t in gates.dup_sanity_trips(dup, canon))

    def test_many_more_files_trips(self):
        dup = _pr_view(paths=["src/a.ts"], diffstat={"additions": 5, "deletions": 1, "changed_files": 9})
        canon = _pr_view(n=1, paths=["src/a.ts"], diffstat={"additions": 5, "deletions": 1, "changed_files": 2})
        assert any("files" in t for t in gates.dup_sanity_trips(dup, canon))

    def test_extra_paths_trip(self):
        dup = _pr_view(paths=["src/a.ts", "src/b.ts"])
        canon = _pr_view(n=1, paths=["src/a.ts"])
        trips = gates.dup_sanity_trips(dup, canon)
        assert any("src/b.ts" in t for t in trips)

    def test_tests_the_canonical_lacks_trip(self):
        dup = _pr_view(paths=["src/a.ts", "src/a.test.ts"])
        canon = _pr_view(n=1, paths=["src/a.ts", "src/a.test.ts"])
        assert gates.dup_sanity_trips(dup, canon) == []
        canon_untested = _pr_view(n=1, paths=["src/a.ts"])
        trips = gates.dup_sanity_trips(dup, canon_untested)
        assert any("tests" in t for t in trips)

    def test_missing_comparison_basis_trips(self):
        dup = _pr_view(paths=None)
        canon = _pr_view(n=1, paths=["src/a.ts"], diffstat={"additions": 1, "deletions": 0, "changed_files": 1})
        trips = gates.dup_sanity_trips(dup, canon)
        assert any("cannot compare changed paths" in t for t in trips)


class TestDupDemotion:
    def test_fully_covered_map_keeps_close_dup(self):
        pr = _pr_view(analysis={"disposition": "close-dup", "canonical": 1,
                                "rationale": "dup", "concerns": COVERED})
        assert gates.dup_demotion(pr) is None
        assert pr.disposition == "close-dup"

    def test_legacy_analysis_without_map_keeps_close_dup(self):
        pr = _pr_view(analysis={"disposition": "close-dup", "canonical": 1, "rationale": "dup"})
        assert pr.disposition == "close-dup"

    def test_unique_concern_reads_request_changes_split(self):
        # The partial-dup case: a covered small fix plus an uncovered big one is
        # never close-dup — it reads as a split ask.
        pr = _pr_view(analysis={
            "disposition": "close-dup", "canonical": 1, "rationale": "dup",
            "concerns": COVERED + [{"label": "the retry queue", "coverage": "unique"}]})
        assert pr.disposition == "request-changes"
        assert "the retry queue" in (pr.rationale or "")
        assert any("split" in a.lower() for a in pr.asks or [])

    def test_sanity_trip_reads_needs_human(self):
        pr = _pr_view(analysis={
            "disposition": "close-dup", "canonical": 1, "rationale": "dup",
            "concerns": COVERED,
            "sanity_trips": ["duplicate touches paths the canonical does not: src/b.ts"]})
        assert pr.disposition == "needs-human"
        assert "src/b.ts" in (pr.rationale or "")

    def test_trip_wins_over_unique(self):
        pr = _pr_view(analysis={
            "disposition": "close-dup", "canonical": 1, "rationale": "dup",
            "concerns": [{"label": "u", "coverage": "unique"}],
            "sanity_trips": ["duplicate carries tests the canonical lacks"]})
        assert pr.disposition == "needs-human"

    def test_other_dispositions_untouched(self):
        pr = _pr_view(analysis={"disposition": "close-fixed", "upstream_pr": 9,
                                "rationale": "landed"})
        assert gates.dup_demotion(pr) is None
        assert pr.disposition == "close-fixed"


class TestAutocloseEligibility:
    def _clean_dup(self):
        return _pr_view(analysis={"disposition": "close-dup", "canonical": 1,
                                  "rationale": "dup", "concerns": COVERED})

    def test_fails_closed_without_shadow_signal(self):
        canon = _pr_view(n=1, state="merged")
        allowed, reasons = gates.dup_autoclose_eligibility(self._clean_dup(), canon)
        assert not allowed
        assert any("shadow" in r for r in reasons)
        assert len(reasons) == 1   # everything else passes

    def test_allows_only_with_everything_affirmed(self):
        canon = _pr_view(n=1, state="merged")
        allowed, reasons = gates.dup_autoclose_eligibility(
            self._clean_dup(), canon, shadow_agreement_ok=True)
        assert allowed and reasons == []

    def test_each_condition_blocks(self):
        canon = _pr_view(n=1, state="merged")
        pr = self._clean_dup()
        pr.rec["analysis"]["concerns"] = COVERED + [{"label": "u", "coverage": "unique"}]
        assert not gates.dup_autoclose_eligibility(pr, canon, shadow_agreement_ok=True)[0]

        pr = self._clean_dup()
        pr.rec["analysis"]["sanity_trips"] = ["duplicate carries tests the canonical lacks"]
        assert not gates.dup_autoclose_eligibility(pr, canon, shadow_agreement_ok=True)[0]

        pr = self._clean_dup()
        pr.rec["meta"]["state"] = "closed"
        assert not gates.dup_autoclose_eligibility(pr, canon, shadow_agreement_ok=True)[0]

        pr = self._clean_dup()
        pr.rec["analysis"]["against_head_sha"] = "OLD"
        allowed, reasons = gates.dup_autoclose_eligibility(pr, canon, shadow_agreement_ok=True)
        assert not allowed and any("stale" in r for r in reasons)

        allowed, reasons = gates.dup_autoclose_eligibility(
            self._clean_dup(), None, shadow_agreement_ok=True)
        assert not allowed and any("canonical" in r for r in reasons)

        gone = _pr_view(n=1, state="closed")
        assert not gates.dup_autoclose_eligibility(
            self._clean_dup(), gone, shadow_agreement_ok=True)[0]

    def test_trusted_author_never_eligible(self, monkeypatch):
        from pipeline import profile
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(trusted_authors=("maint",)))
        canon = _pr_view(n=1, state="merged")
        pr = Pr(None, _rec(analysis={"disposition": "close-dup", "canonical": 1,
                                     "rationale": "dup", "concerns": COVERED},
                           author="maint"))
        allowed, reasons = gates.dup_autoclose_eligibility(pr, canon, shadow_agreement_ok=True)
        assert not allowed and any("trusted" in r for r in reasons)


# ── commit-time behavior ──────────────────────────────────────────────────────

def _store_pr(store, n, head="h1", paths=(), diffstat=None, author="a"):
    signals = {"ci": "passing", "mergeable": True, "checked_at": NOW,
               "against_head_sha": head}
    if diffstat is not None:
        signals["diffstat"] = diffstat
    store.save_pr({
        "pr": n,
        "meta": {"title": f"t{n}", "author": author, "state": "open", "draft": False,
                 "head_sha": head, "checked_at": NOW},
        "signals": signals,
        "reviews": reviews_section(head, NOW),
        "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head},
        "summary": {"one_liner": f"does {n}", "subsystem": "ui", "mechanism": "m",
                    "identifiers": [], "paths": list(paths), "primary_change": "p",
                    "secondary_changes": [], "checked_at": NOW, "against_head_sha": head},
    })


def _cluster(store, cid, prs):
    store.save_cluster({"id": cid, "root_problem": "x", "prs": [],
                        "outcome": None, "checked_at": NOW})
    store.edit_cluster(cid).set_members(prs)


def _payload(concerns):
    return {"cluster_id": 1, "outcome": "merge-ready", "rationale": "1 wins",
            "prs": [
                {"pr": 1, "disposition": "merge", "rationale": "best", "head_sha": "h1"},
                {"pr": 2, "disposition": "close-dup", "canonical": 1, "rationale": "dup",
                 "head_sha": "h1", **({"concerns": concerns} if concerns is not None else {})},
            ]}


class TestCommitRequiresCoverageMap:
    def test_close_dup_without_concerns_rejected(self, tmp_path):
        s = Store(tmp_path)
        _store_pr(s, 1, paths=["src/a.ts"]); _store_pr(s, 2, paths=["src/a.ts"])
        _cluster(s, 1, [1, 2])
        errs = ad.commit_analysis(s, _payload(None))
        assert any("concerns" in e for e in errs)
        assert s.load_cluster(1).outcome is None      # nothing committed

    def test_malformed_concern_rejected(self, tmp_path):
        s = Store(tmp_path)
        _store_pr(s, 1, paths=["src/a.ts"]); _store_pr(s, 2, paths=["src/a.ts"])
        _cluster(s, 1, [1, 2])
        errs = ad.commit_analysis(s, _payload([{"label": "x", "coverage": "pr"}]))
        assert any("covered_by" in e for e in errs)

    def test_map_lands_in_analysis_and_reads_close_dup(self, tmp_path):
        s = Store(tmp_path)
        _store_pr(s, 1, paths=["src/a.ts"]); _store_pr(s, 2, paths=["src/a.ts"])
        _cluster(s, 1, [1, 2])
        assert ad.commit_analysis(s, _payload(COVERED)) == []
        pr = s.load_pr(2)
        assert pr.section("analysis")["concerns"] == COVERED
        assert "sanity_trips" not in pr.section("analysis")
        assert pr.disposition == "close-dup"

    def test_sanity_trips_computed_never_trusted(self, tmp_path):
        # The dup touches a path the canonical does not; the agent's own
        # sanity_trips claim is dropped and the computed one stored.
        s = Store(tmp_path)
        _store_pr(s, 1, paths=["src/a.ts"])
        _store_pr(s, 2, paths=["src/a.ts", "src/extra.ts"])
        _cluster(s, 1, [1, 2])
        p = _payload(COVERED)
        p["prs"][1]["sanity_trips"] = ["agent-invented trip"]
        assert ad.commit_analysis(s, p) == []
        pr = s.load_pr(2)
        trips = pr.section("analysis")["sanity_trips"]
        assert any("src/extra.ts" in t for t in trips)
        assert "agent-invented trip" not in trips
        assert pr.disposition == "needs-human"        # sanity forces a person

    def test_unique_concern_salvages(self, tmp_path):
        s = Store(tmp_path)
        _store_pr(s, 1, paths=["src/a.ts"]); _store_pr(s, 2, paths=["src/a.ts"])
        _cluster(s, 1, [1, 2])
        p = _payload(COVERED + [{"label": "the retry queue", "coverage": "unique"}])
        assert ad.commit_analysis(s, p) == []
        assert s.load_pr(2).disposition == "request-changes"
        ad.backfill_salvage_items(s, today=NOW[:10])
        items = {i["id"]: i for i in s.load_action_items()["items"]}
        assert "salvage-fix:2" in items
        assert "the retry queue" in items["salvage-fix:2"]["detail"]
