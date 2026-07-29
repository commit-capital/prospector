"""INGEST transforms: gh / issue-link payloads → store sections.
Network calls live in thin helpers; everything tested here is pure."""
from types import SimpleNamespace

import pytest

from pipeline import greptile
from pipeline import ingest
from pipeline import settings
from pipeline.testsupport import set_section
from pipeline.store import Store


GH_PR = {
    "number": 5001,
    "title": "fix(heartbeat): clear stale lock",
    "body": "Fixes #123",
    "user": {"login": "contributor1"},
    "state": "open",
    "draft": False,
    "head": {"sha": "deadbeef"},
    "base": {"ref": "main"},
    "html_url": "https://github.com/test-owner/test-repo/pull/5001",
    "created_at": "2026-05-01T00:00:00Z",
    "updated_at": "2026-06-09T00:00:00Z",
    "merged_at": None,
}


class TestMetaFromGh:
    def test_maps_fields(self):
        meta = ingest.meta_from_gh(GH_PR)
        assert meta["title"].startswith("fix(heartbeat)")
        assert meta["author"] == "contributor1"
        assert meta["head_sha"] == "deadbeef"
        assert meta["state"] == "open"
        assert meta["draft"] is False

    def test_merged_state(self):
        merged = dict(GH_PR, state="closed", merged_at="2026-06-01T00:00:00Z")
        assert ingest.meta_from_gh(merged)["state"] == "merged"

    def test_captures_comments_and_reactions(self):
        pr = dict(GH_PR, comments=7, reactions={"total_count": 3, "+1": 3})
        meta = ingest.meta_from_gh(pr)
        assert meta["comments"] == 7
        assert meta["reactions_total"] == 3

    def test_missing_comments_and_reactions_are_none(self):
        meta = ingest.meta_from_gh(GH_PR)
        assert meta["comments"] is None
        assert meta["reactions_total"] is None

    def test_reactions_without_total_count_is_none(self):
        pr = dict(GH_PR, reactions={"url": "https://api.github.com/..."})
        assert ingest.meta_from_gh(pr)["reactions_total"] is None


class TestDriftFromSignals:
    def test_applicable_when_mergeable(self):
        assert ingest.drift_from_signals({"mergeable": True})["state"] == "applicable"

    def test_conflicts_when_not(self):
        assert ingest.drift_from_signals({"mergeable": False})["state"] == "conflicts"

    def test_unknown_mergeable_defaults_applicable(self):
        assert ingest.drift_from_signals({})["state"] == "applicable"


class TestCiFromGithubData:
    """GitHub's authoritative CI verdict, combining the check-runs API and the
    legacy commit-status API the way the PR Checks tab does."""

    def test_all_checks_success_is_passing(self):
        runs = [{"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "success"}]
        assert ingest._ci_from_github_data(runs, []) == "passing"

    def test_any_failed_run_is_failing(self):
        runs = [{"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "failure"}]
        assert ingest._ci_from_github_data(runs, []) == "failing"

    def test_incomplete_run_is_pending(self):
        runs = [{"status": "completed", "conclusion": "success"},
                {"status": "in_progress", "conclusion": None}]
        assert ingest._ci_from_github_data(runs, []) == "pending"

    def test_skipped_and_neutral_do_not_fail(self):
        runs = [{"status": "completed", "conclusion": "skipped"},
                {"status": "completed", "conclusion": "neutral"},
                {"status": "completed", "conclusion": "success"}]
        assert ingest._ci_from_github_data(runs, []) == "passing"

    def test_failure_outranks_pending(self):
        runs = [{"status": "in_progress", "conclusion": None},
                {"status": "completed", "conclusion": "failure"}]
        assert ingest._ci_from_github_data(runs, []) == "failing"

    def test_legacy_status_failure_is_failing(self):
        assert ingest._ci_from_github_data([], [{"state": "failure"}]) == "failing"
        assert ingest._ci_from_github_data([], [{"state": "error"}]) == "failing"

    def test_legacy_status_pending_is_pending(self):
        assert ingest._ci_from_github_data([], [{"state": "pending"}]) == "pending"

    def test_combines_check_runs_and_statuses(self):
        runs = [{"status": "completed", "conclusion": "success"}]
        statuses = [{"state": "success"}]
        assert ingest._ci_from_github_data(runs, statuses) == "passing"

    def test_no_signal_returns_none(self):
        # GitHub has nothing to say → caller keeps the stored value.
        assert ingest._ci_from_github_data([], []) is None


class TestIssueLinks:
    def test_pr_to_issue_map(self):
        links = [
            {"issue": 42, "pain": 0.9, "candidates": [
                {"pr": 5001, "how": "explicit"}, {"pr": 5002, "how": "subsystem"}]},
            {"issue": 43, "pain": 0.5, "candidates": [{"pr": 5001, "how": "subsystem"}]},
        ]
        by_pr = ingest.issues_by_pr(links)
        assert by_pr[5001] == [{"issue": 42, "pain": 0.9, "how": "explicit"},
                               {"issue": 43, "pain": 0.5, "how": "subsystem"}]
        assert by_pr[5002] == [{"issue": 42, "pain": 0.9, "how": "subsystem"}]

    def test_load_from_issue_store(self, tmp_path):
        """load_issue_links inverts the SQL issue store: each issue's candidate PRs
        become PR→issue entries, carrying the issue's cluster pain."""
        from issue_triage import issue_store
        st = issue_store.IssueStore(tmp_path)
        i42 = st.create_issue(42, {"title": "crash", "state": "open", "updated_at": "T"})
        i42.set_links([{"pr": 5001, "how": "explicit"}, {"pr": 5002, "how": "subsystem"}])
        i43 = st.create_issue(43, {"title": "slow", "state": "open", "updated_at": "T"})
        i43.set_links([{"pr": 5001, "how": "subsystem"}])
        st.create_issue(44, {"title": "no candidates", "state": "open", "updated_at": "T"})
        cl = st.create_issue_cluster(9, "crashes")
        cl.set_members([42, 43])
        cl.set_pain(0.7)

        by_pr = ingest.load_issue_links(st)
        assert by_pr[5001] == [{"issue": 42, "pain": 0.7, "how": "explicit"},
                               {"issue": 43, "pain": 0.7, "how": "subsystem"}]
        assert by_pr[5002] == [{"issue": 42, "pain": 0.7, "how": "subsystem"}]
        # issue 44 has no candidate PRs → it links to nothing
        assert 44 not in {e["issue"] for links in by_pr.values() for e in links}


class TestBuildSignals:
    def test_override_copies_existing(self):
        sig = ingest._build_signals(existing_signals={"greptile": 4, "mergeable": True},
                                    ci_override="passing",
                                    greptile_override=5, greptile_reviewed_sha="deadbeef")
        assert sig == {"greptile": 5, "mergeable": True, "ci": "passing",
                       "greptile_reviewed_sha": "deadbeef"}

    def test_no_override_is_none(self):
        assert ingest._build_signals(existing_signals=None, ci_override=None,
                                     greptile_override=None,
                                     greptile_reviewed_sha=None) is None

    def test_no_override_leaves_existing_unwritten(self):
        # The bulk path passes no overrides; stored signals stay untouched.
        assert ingest._build_signals(existing_signals={"greptile": 5},
                                     ci_override=None, greptile_override=None,
                                     greptile_reviewed_sha=None) is None

    def test_refresh_drops_diff_fields_when_fresh_values_are_unavailable(self):
        sig = ingest._build_signals(
            existing_signals={"ci": "failing", "has_tests": False,
                              "diffstat": {"additions": 1, "deletions": 0,
                                           "changed_files": 1}},
            ci_override="passing", greptile_override=None,
            greptile_reviewed_sha=None)
        assert sig == {"ci": "passing"}


class TestUpsert:
    def test_upsert_new_pr(self, tmp_path):
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR, issues=[{"issue": 42, "pain": 0.9, "how": "explicit"}])
        rec = store.load_pr(5001)
        assert rec.head_sha == "deadbeef"
        assert rec.section("issues")["linked"][0]["issue"] == 42

    def test_upsert_writes_row_once_per_pr(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        writes: list[int] = []
        orig = store._prs.save
        monkeypatch.setattr(store._prs, "save", lambda rec: (writes.append(1), orig(rec))[1])
        issues = [{"issue": 42, "pain": 0.9, "how": "explicit"}]
        ingest.upsert_pr(store, GH_PR, issues=issues)
        assert len(writes) == 1  # new PR: a single row write, not one per section
        writes.clear()
        ingest.upsert_pr(store, GH_PR, issues=issues)
        assert len(writes) == 1  # existing-PR refresh: still one write

    def test_head_move_stales_old_sections(self, tmp_path):
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR, ci_override="passing")
        set_section(store, 5001, "analysis", {"disposition": "merge", "rationale": "r"})
        moved = dict(GH_PR, head={"sha": "NEW000"})
        ingest.upsert_pr(store, moved, ci_override="passing")
        rec = store.load_pr(5001)
        from pipeline.freshness import is_current, stale_sections
        assert not is_current(rec, "analysis")          # analysis was for old head
        assert is_current(rec, "signals")               # signals refreshed at upsert
        assert "analysis" in stale_sections(rec)

    def test_persists_new_draft(self, tmp_path):
        store = Store(tmp_path)
        pr = ingest.upsert_pr(store, dict(GH_PR, draft=True))
        assert pr is not None
        rec = store.load_pr(5001)
        assert rec is not None
        assert rec.raw["meta"]["draft"] is True

    def test_existing_pr_gone_draft_is_marked(self, tmp_path):
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR)
        ingest.upsert_pr(store, dict(GH_PR, draft=True))
        assert store.load_pr(5001).raw["meta"]["draft"] is True

    def test_greptile_reviewed_sha_override_stored(self, tmp_path):
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR, greptile_reviewed_sha="abc000ff")
        rec = store.load_pr(5001)
        assert rec.section("signals")["greptile_reviewed_sha"] == "abc000ff"

    def test_applies_overrides(self, tmp_path):
        st = Store(tmp_path)
        ingest.upsert_pr(st, GH_PR, ci_override="passing",
                         greptile_override=5, greptile_reviewed_sha="deadbeef")
        sig = st.load_pr(5001).signals
        assert sig["ci"] == "passing"
        assert sig["greptile"] == 5
        assert sig["greptile_reviewed_sha"] == "deadbeef"
        # overrides also derive drift from the fresh signals
        assert st.load_pr(5001).section("drift")["state"] == "applicable"

    def test_no_overrides_writes_no_signals(self, tmp_path):
        st = Store(tmp_path)
        ingest.upsert_pr(st, GH_PR)
        assert st.load_pr(5001).signals is None

    def test_bulk_reingest_preserves_existing_signals(self, tmp_path):
        # A corpus re-upsert carries no overrides, so signals stamped earlier
        # (live sweep, Greptile scrape, single-PR refresh) survive untouched.
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR, greptile_override=5,
                         greptile_reviewed_sha="deadbeef")
        ingest.upsert_pr(store, GH_PR)
        sig = store.load_pr(5001).section("signals")
        assert sig["greptile"] == 5
        assert sig["greptile_reviewed_sha"] == "deadbeef"


class TestUpsertAll:
    def test_counts_upserted_drafts_and_open_ids(self, tmp_path):
        st = Store(tmp_path)
        prs = [GH_PR, dict(GH_PR, number=5002, draft=True)]
        with st.batch():
            existing = st.all_prs()
            res = ingest._upsert_all(st, prs, issue_links={}, existing_prs=existing)
        assert res["upserted"] == 2
        assert res["drafts"] == 1
        assert res["open_ids"] == {5001, 5002}

    def test_reuses_snapshot_and_writes_bounded_chunks(self, tmp_path, monkeypatch):
        st = Store(tmp_path)
        ingest.upsert_pr(st, GH_PR, greptile_override=5)
        existing = st.all_prs()
        prs = [dict(GH_PR, number=n, head={"sha": f"sha-{n}"})
               for n in (5001, 5002, 5003)]
        batches: list[list[int]] = []
        save_many = st.save_prs_many

        monkeypatch.setattr(
            st, "load_pr", lambda n: pytest.fail("bulk ingest must not load per PR"))
        monkeypatch.setattr(
            st, "save_prs_many",
            lambda prs: (batches.append([pr.number for pr in prs]), save_many(prs))[1])

        with st.batch():
            ingest._upsert_all(
                st, prs, issue_links={}, existing_prs=existing, batch_size=2)

        assert batches == [[5001, 5002], [5003]]
        assert st.all_prs()[5001].greptile == 5  # non-ingest facts survive staging


class TestTargetedIngest:
    def test_parse_pr_ids(self):
        assert ingest._parse_pr_ids("1, 2 ,3") == {1, 2, 3}

    def test_select_new_prs(self):
        prs = [dict(GH_PR, number=1), dict(GH_PR, number=2)]
        out = ingest._select_new_prs(prs, {1})
        assert [p["number"] for p in out] == [2]

    def test_main_new_ingests_only_absent(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR)  # seed 5001
        new_pr = dict(GH_PR, number=6002, head={"sha": "newsha"})
        monkeypatch.setattr(ingest, "fetch_open_prs", lambda mx=None: [GH_PR, new_pr])
        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        rc = ingest.main(["--new", "--store", str(tmp_path)])
        assert rc == 0
        assert store.load_pr(6002) is not None
        assert store.runs()[-1].raw["stats"]["upserted"] == 1

    def test_main_prs_ingests_requested_only(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        b = dict(GH_PR, number=7002, head={"sha": "s2"})
        fetched: list[int] = []
        monkeypatch.setattr(ingest, "fetch_open_prs",
                            lambda mx=None: pytest.fail("--prs must not fetch the open corpus"))
        monkeypatch.setattr(ingest, "fetch_pr",
                            lambda n: fetched.append(n) or b)
        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
        monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (5, "s2"))
        rc = ingest.main(["--prs", "7002", "--store", str(tmp_path)])
        assert rc == 0
        assert fetched == [7002]
        rec = store.load_pr(7002)
        assert rec.ci == "passing"
        assert rec.greptile == 5
        assert rec.greptile_reviewed_sha == "s2"
        assert store.runs()[-1].raw["stats"]["upserted"] == 1

    def test_refresh_skips_greptile_fetch_when_no_provider(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "REVIEW_PROVIDER", "none")
        store = Store(tmp_path)
        b = dict(GH_PR, number=7003, head={"sha": "s3"})
        monkeypatch.setattr(ingest, "fetch_pr", lambda n: b)
        monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        monkeypatch.setattr(greptile, "fetch_greptile_verdict",
                            lambda n: pytest.fail("must not fetch Greptile when provider is none"))
        out = ingest.refresh_prs(store, [7003])
        assert out[0]["pr"] == 7003
        assert store.load_pr(7003).greptile is None

    def test_main_prs_and_new_refreshes_only_absent_requested(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        ingest.upsert_pr(store, dict(GH_PR, number=7001))
        refreshed: list[int] = []
        monkeypatch.setattr(
            ingest, "refresh_prs",
            lambda st, numbers: refreshed.extend(numbers) or [])

        rc = ingest.main(["--new", "--prs", "7001,7002", "--store", str(tmp_path)])

        assert rc == 0
        assert refreshed == [7002]

    def test_main_full_chains_issue_ingest(self, tmp_path, monkeypatch):
        """A full ingest runs the issue ingest after the PR sweep; --skip-issues
        and the targeted modes do not."""
        from issue_triage import issue_ingest
        monkeypatch.setattr(ingest, "fetch_open_prs", lambda mx=None: [GH_PR])
        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        calls: list[list[str] | None] = []
        monkeypatch.setattr(issue_ingest, "main", lambda argv=None: calls.append(argv))
        assert ingest.main(["--store", str(tmp_path)]) == 0
        assert calls == [[]]
        assert ingest.main(["--store", str(tmp_path), "--skip-issues"]) == 0
        assert calls == [[]]
        assert ingest.main(["--new", "--store", str(tmp_path)]) == 0
        assert calls == [[]]

    def test_main_full_loads_pr_corpus_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ingest, "fetch_open_prs", lambda mx=None: [GH_PR])
        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        loads: list[int] = []
        all_prs = Store.all_prs

        def counting_all_prs(store):
            loads.append(1)
            return all_prs(store)

        monkeypatch.setattr(Store, "all_prs", counting_all_prs)
        assert ingest.main(["--store", str(tmp_path), "--skip-issues"]) == 0
        assert loads == [1]

    def test_main_full_reconciles_closure_from_loaded_snapshot(self, tmp_path, monkeypatch):
        store = Store(tmp_path)
        ingest.upsert_pr(store, GH_PR)
        closed = dict(GH_PR, state="closed")
        monkeypatch.setattr(ingest, "fetch_open_prs", lambda mx=None: [])
        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        monkeypatch.setattr(ingest, "fetch_pr", lambda n: closed)
        monkeypatch.setattr(
            Store, "edit_pr", lambda self, n: pytest.fail("closure must use the snapshot"))

        assert ingest.main(["--store", str(tmp_path), "--skip-issues"]) == 0
        assert store.load_pr(5001).state == "closed"
        assert store.runs()[-1].raw["stats"]["state_transitions"] == 1


class TestRefreshPrs:
    def test_refreshes_ci_and_greptile(self, tmp_path, monkeypatch):
        import json as _json
        st = Store(tmp_path)

        monkeypatch.setattr(ingest, "load_issue_links", lambda: {})
        monkeypatch.setattr(ingest, "gh_ci_status", lambda sha: "passing")
        monkeypatch.setattr(greptile, "fetch_greptile_verdict", lambda n: (5, "deadbeef"))
        monkeypatch.setattr(ingest.subprocess, "run",
                            lambda *a, **k: SimpleNamespace(returncode=0, stdout=_json.dumps(GH_PR)))

        ingest.refresh_prs(st, [5001])
        sig = st.load_pr(5001).signals
        assert sig["ci"] == "passing"
        assert sig["greptile"] == 5
        assert sig["greptile_reviewed_sha"] == "deadbeef"
