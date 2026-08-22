"""ANALYZE driver: pending-cluster selection, bundles, validated commits."""
from pipeline import analyze_driver as ad
from pipeline.testsupport import set_section
from pipeline.store import Store
from pipeline.testsupport import reviews_section

NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, head="h1", analysis=None, summary=True, draft=False, author="a"):
    rec = {"pr": n, "meta": {"title": f"t{n}", "author": author, "state": "open", "draft": draft,
                             "head_sha": head, "checked_at": NOW},
           "signals": {"ci": "passing", "mergeable": True,
                       "checked_at": NOW, "against_head_sha": head},
           "reviews": reviews_section(head, NOW),
           "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head}}
    if summary:
        rec["summary"] = {"one_liner": f"does {n}", "subsystem": "ui", "mechanism": "m",
                          "identifiers": [], "paths": [], "primary_change": f"primary {n}",
                          "secondary_changes": [], "checked_at": NOW, "against_head_sha": head}
    if analysis:
        rec["analysis"] = dict(analysis, checked_at=NOW)
    store.save_pr(rec)


def _cluster(store, cid, prs, outcome=None):
    store.save_cluster({"id": cid, "root_problem": "x", "prs": [],
                        "outcome": outcome, "checked_at": NOW})
    store.edit_cluster(cid).set_members(prs)   # wires cluster.prs AND each pr.cluster_ids
    return store.load_cluster(cid)


class TestDraftMembers:
    def test_draft_is_an_active_member(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2, draft=True)
        _cluster(s, 1, [1, 2])
        assert [m.n for m in ad._active_members(s, s.load_cluster(1))] == [1, 2]

    def test_bundle_carries_draft_flag(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2, draft=True)
        _cluster(s, 1, [1, 2])
        b = ad.bundle(s, 1)
        flags = {m["pr"]: m["draft"] for m in b["members"]}
        assert flags == {1: False, 2: True}

    def test_bundle_carries_trusted_flag(self, tmp_path, monkeypatch):
        from pipeline import profile
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(trusted_authors=("trusted-dev",)))
        s = Store(tmp_path)
        _pr(s, 1, author="trusted-dev"); _pr(s, 2, author="rando")
        _cluster(s, 1, [1, 2])
        b = ad.bundle(s, 1)
        flags = {m["pr"]: m["trusted"] for m in b["members"]}
        assert flags == {1: True, 2: False}


class TestPending:
    def test_unanalyzed_cluster_is_pending(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2)
        _cluster(s, 1, [1, 2])
        assert [c.id for c in ad.pending(s)] == [1]

    def test_fully_analyzed_cluster_not_pending(self, tmp_path):
        s = Store(tmp_path)
        a = {"disposition": "merge", "rationale": "r", "against_head_sha": "h1"}
        _pr(s, 1, analysis=a); _pr(s, 2, analysis=dict(a, disposition="close-dup", canonical=1))
        _cluster(s, 1, [1, 2], outcome="merge-ready")
        assert ad.pending(s) == []

    def test_stale_member_analysis_makes_pending(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, head="NEW", analysis={"disposition": "merge", "rationale": "r",
                                        "against_head_sha": "OLD"})
        _pr(s, 2, analysis={"disposition": "close-dup", "canonical": 1, "rationale": "r",
                            "against_head_sha": "h1"})
        _cluster(s, 1, [1, 2], outcome="merge-ready")
        assert [c.id for c in ad.pending(s)] == [1]


class TestBundle:
    def test_bundle_shape(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2)
        _cluster(s, 1, [1, 2])
        b = ad.bundle(s, 1)
        assert b["cluster"]["id"] == 1
        assert [m["pr"] for m in b["members"]] == [1, 2]
        assert b["members"][0]["reviews"]["greptile"]["score"] == 5
        assert b["members"][0]["reviews"]["greptile"]["status"] == "pass"
        assert b["members"][0]["one_liner"] == "does 1"
        assert b["members"][0]["primary_change"] == "primary 1"
        assert b["members"][0]["secondary_changes"] == []

    def test_bundle_includes_greptile_review(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2)
        _cluster(s, 1, [1, 2])
        s.load_pr(1).set_greptile_review({
            "severity": "defects",
            "findings": [{"headline": "h", "class": "substantive", "why": "w"}],
            "summary": "s",
        })
        b = ad.bundle(s, 1)
        by_pr = {m["pr"]: m["greptile_review"] for m in b["members"]}
        assert by_pr[1]["severity"] == "defects"
        assert by_pr[1]["findings"] == [{"headline": "h", "class": "substantive", "why": "w"}]
        assert by_pr[2] is None

    def test_already_on_master_none_without_tree(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        _cluster(s, 1, [1])
        b = ad.bundle(s, 1)
        assert b["members"][0]["already_on_master"] is None

    def test_already_on_master_computed_from_cached_diff(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        _pr(s, 1)
        _cluster(s, 1, [1])
        diffs = tmp_path / "diffs"
        diffs.mkdir()
        (diffs / "h1.diff").write_text(
            "diff --git a/x.ts b/x.ts\n"
            "index 1111111..2222222 100644\n"
            "--- a/x.ts\n"
            "+++ b/x.ts\n"
            "@@ -1,2 +1,3 @@\n"
            " const a = [\n"
            "+  1,\n"
            " ];\n")
        monkeypatch.setattr(ad, "DIFFS", diffs)

        class _Tree:
            def read(self, path):
                return "const a = [\n  1,\n];\n"

        b = ad.bundle(s, 1, master=_Tree())
        assert b["members"][0]["already_on_master"] == {
            "hunks": 1, "redundant": 1, "unchecked": 0,
            "files": {"x.ts": {"hunks": 1, "redundant": 1}}}


class TestCommitAnalysis:
    def _setup(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2); _pr(s, 3)
        _cluster(s, 1, [1, 2, 3])
        return s

    def _payload(self, **over):
        p = {"cluster_id": 1, "outcome": "merge-ready", "rationale": "1 is best",
             "prs": [
                 {"pr": 1, "disposition": "merge", "rationale": "best impl", "head_sha": "h1"},
                 {"pr": 2, "disposition": "close-dup", "canonical": 1, "rationale": "subset", "head_sha": "h1"},
                 {"pr": 3, "disposition": "request-changes", "asks": ["add a test"],
                  "rationale": "complementary but untested", "head_sha": "h1"},
             ]}
        p.update(over)
        return p

    def test_valid_commit(self, tmp_path):
        s = self._setup(tmp_path)
        payload = self._payload()
        payload["prs"][1].update({
            "disposition": "close-fixed",
            "upstream_pr": 6008,
            "upstream_commit": "f3db7b88ea38a89546719ef2e0e2101127e55480",
            "upstream_date": "2026-06-10",
        })
        payload["prs"][1].pop("canonical")
        errs = ad.commit_analysis(s, payload)
        assert errs == []
        assert s.load_pr(1).section("analysis")["disposition"] == "merge"
        assert s.load_pr(2).section("analysis")["upstream_pr"] == 6008
        assert s.load_pr(2).section("analysis")["upstream_commit"] == "f3db7b88ea38a89546719ef2e0e2101127e55480"
        assert s.load_pr(2).section("analysis")["upstream_date"] == "2026-06-10"
        c = s.load_cluster(1)
        assert c.outcome == "merge-ready" and c.rationale == "1 is best"

    def test_close_on_trusted_member_rejected(self, tmp_path, monkeypatch):
        # a maintainer's open PR is intentional — never routed to a close
        from pipeline import profile
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(trusted_authors=("trusted-dev",)))
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2, author="trusted-dev"); _pr(s, 3)
        _cluster(s, 1, [1, 2, 3])
        errs = ad.commit_analysis(s, self._payload())    # payload closes 2 as dup
        assert any("trusted" in e for e in errs)
        assert s.load_cluster(1).outcome is None         # nothing committed

    def test_non_close_on_trusted_member_commits(self, tmp_path, monkeypatch):
        from pipeline import profile
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(trusted_authors=("trusted-dev",)))
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2, author="trusted-dev"); _pr(s, 3)
        _cluster(s, 1, [1, 2, 3])
        payload = self._payload()
        payload["prs"][1] = {"pr": 2, "disposition": "request-changes",
                             "asks": ["rebase"], "rationale": "r", "head_sha": "h1"}
        assert ad.commit_analysis(s, payload) == []

    def test_merge_on_draft_member_rejected(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, draft=True); _pr(s, 2); _pr(s, 3)
        _cluster(s, 1, [1, 2, 3])
        p = self._payload()                       # PR 1 is proposed "merge"
        errs = ad.commit_analysis(s, p)
        assert any("draft" in e and "merge" in e for e in errs)
        assert s.load_cluster(1).outcome is None  # all-or-nothing: nothing written

    def test_missing_member_disposition_rejected(self, tmp_path):
        s = self._setup(tmp_path)
        p = self._payload()
        p["prs"] = p["prs"][:2]
        errs = ad.commit_analysis(s, p)
        assert any("3" in e for e in errs)
        assert s.load_cluster(1).outcome is None  # nothing written

    def test_close_dup_canonical_can_be_a_kept_member(self, tmp_path):
        s = self._setup(tmp_path)
        p = self._payload()
        p["prs"][1]["canonical"] = 3  # 3 is request-changes — allowed (kept)
        assert ad.commit_analysis(s, p) == []

    def test_close_dup_canonical_may_point_at_a_closed_non_dup(self, tmp_path):
        # abandoned pair: close 2 as dup of 1, close 1 as stale — both close, valid
        s = self._setup(tmp_path)
        p = self._payload(outcome="close-out")
        p["prs"][0]["disposition"] = "close-stale"; p["prs"][0].pop("asks", None)
        p["prs"][1]["canonical"] = 1
        p["prs"][2]["disposition"] = "close-stale"; p["prs"][2].pop("asks", None)
        assert ad.commit_analysis(s, p) == []

    def test_close_dup_canonical_cannot_be_another_close_dup(self, tmp_path):
        # chain: 2 dup-of-1, 1 dup-of-3 — 1 is itself a close-dup, rejected
        s = self._setup(tmp_path)
        p = self._payload(outcome="close-out")
        p["prs"][0]["disposition"] = "close-dup"; p["prs"][0]["canonical"] = 3
        p["prs"][0].pop("asks", None)
        p["prs"][1]["canonical"] = 1
        p["prs"][2]["disposition"] = "close-stale"; p["prs"][2].pop("asks", None)
        assert any("close-dup" in e for e in ad.commit_analysis(s, p))

    def test_merge_ready_requires_a_merge(self, tmp_path):
        s = self._setup(tmp_path)
        p = self._payload()
        for row in p["prs"]:
            row["disposition"] = "close-stale"
            row.pop("canonical", None); row.pop("asks", None)
        errs = ad.commit_analysis(s, p)
        assert any("merge-ready" in e for e in errs)

    def test_request_changes_requires_asks(self, tmp_path):
        s = self._setup(tmp_path)
        p = self._payload()
        p["prs"][2]["asks"] = []
        assert any("asks" in e for e in ad.commit_analysis(s, p))

    def test_analysis_stamped_to_analyzed_head(self, tmp_path):
        s = self._setup(tmp_path)
        ad.commit_analysis(s, self._payload())
        assert s.load_pr(1).section("analysis")["against_head_sha"] == "h1"

    def test_stale_echoed_head_sha_ignored(self, tmp_path):
        # An agent that echoes a wrong head_sha must not stamp a never-fresh
        # analysis: the commit pins to the member's current head regardless.
        from pipeline.freshness import is_current
        s = self._setup(tmp_path)
        p = self._payload()
        for row in p["prs"]:
            row["head_sha"] = "STALE"
        assert ad.commit_analysis(s, p) == []
        for n in (1, 2, 3):
            assert s.load_pr(n).section("analysis")["against_head_sha"] == "h1"
            assert is_current(s.load_pr(n), "analysis")
        assert s.load_cluster(1).proposal_for(1)["head_sha"] == "h1"


class TestSalvageActionItems:
    def _pr_with_rationale(self, store, n, disposition, rationale):
        store.save_pr({
            "pr": n,
            "meta": {"title": f"t{n}", "author": "a", "state": "open", "draft": False,
                     "head_sha": "h1", "checked_at": NOW},
            "analysis": {"disposition": disposition, "rationale": rationale,
                         "checked_at": NOW, "against_head_sha": "h1"},
        })

    def test_backfill_emits_salvage_for_spinoff_language(self, tmp_path):
        s = Store(tmp_path)
        self._pr_with_rationale(s, 4983, "needs-human",
            "Bundles a legitimate crash-fix inside an unwanted Spanish translation. "
            "The salvageable null-guard fix is being spun off as a separate clean "
            "English-only first-party PR.")
        self._pr_with_rationale(s, 5000, "close-stale",
            "Abandoned and obsolete, nothing to keep.")  # no salvage language
        n = ad.backfill_salvage_items(s, today=NOW)
        reg = s.load_action_items()
        ids = {i["id"] for i in reg["items"]}
        assert n == 1
        assert "salvage-fix:4983" in ids
        assert "salvage-fix:5000" not in ids

    def test_backfill_is_idempotent_and_status_preserving(self, tmp_path):
        from pipeline import actions
        s = Store(tmp_path)
        self._pr_with_rationale(s, 4983, "needs-human",
            "salvageable fix; spun off as a separate first-party PR")
        ad.backfill_salvage_items(s, today=NOW)
        reg = s.load_action_items()
        actions.set_status(reg, "salvage-fix:4983", "done")
        s.save_action_items(reg)
        ad.backfill_salvage_items(s, today=NOW)
        done = {i["id"]: i for i in s.load_action_items()["items"]}
        assert done["salvage-fix:4983"]["status"] == "done"


class TestMergeBarReads:
    def test_secret_leak_reads_request_changes(self, tmp_path):
        # An otherwise-clean merge pick that leaks a credential must not read as
        # a merge: the derived route is request-changes with a remove-the-secret
        # ask. (Gate-clean except for the threat signal.)
        s = Store(tmp_path)
        _pr(s, 521, analysis={"disposition": "merge", "rationale": "good feature",
                              "against_head_sha": "h1"})
        set_section(s, 521, "threat",
                    {"verdict": "suspicious", "signatures": ["secret-leak"]},
                    against_head_sha="h1")
        _cluster(s, 7, [521], outcome="merge-ready")
        pr = s.load_pr(521)
        assert pr.disposition == "request-changes"
        assert any("rotate" in ask.lower() for ask in pr.asks)

    def test_draft_flip_reads_request_changes_with_ready_ask(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 530, draft=True, analysis={"disposition": "merge", "rationale": "was clean",
                                          "against_head_sha": "h1"})
        _cluster(s, 8, [530], outcome="merge-ready")
        pr = s.load_pr(530)
        assert pr.disposition == "request-changes"
        assert any("ready for review" in ask.lower() for ask in pr.asks)


def _standalone(store, n, **over):
    """A PR a clustering pass left standalone: a current `cluster` stamp, no id."""
    _pr(store, n, **over)
    set_section(store, n, "cluster", {})   # stamps checked_at + against_head_sha (no id)


class TestDispositionOrphans:
    def test_clean_orphan_gets_merge(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1)
        assert ad.disposition_orphans(s) == 1
        assert s.load_pr(1).section("analysis")["disposition"] == "merge"

    def test_subbar_orphan_stores_merge_and_reads_request_changes(self, tmp_path):
        # The stored verdict is merge; the gate gap derives request-changes with
        # asks at read time, and heals in place when the signal clears.
        s = Store(tmp_path)
        _standalone(s, 1)
        rec = s.load_pr(1); rec.raw["reviews"]["greptile"]["score"] = 4; s.save_pr(rec)
        assert ad.disposition_orphans(s) == 1
        pr = s.load_pr(1)
        assert pr.section("analysis")["disposition"] == "merge"
        assert pr.disposition == "request-changes"
        assert any("greptile" in ask.lower() for ask in pr.asks)

    def test_already_fixed_orphan_gets_close_fixed(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1)
        rec = s.load_pr(1); rec.raw["drift"]["state"] = "already-fixed"; s.save_pr(rec)
        assert ad.disposition_orphans(s) == 1
        assert s.load_pr(1).section("analysis")["disposition"] == "close-fixed"

    def test_malicious_orphan_gets_needs_human(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1)
        set_section(s, 1, "threat", {"verdict": "malicious", "signatures": ["capability-smuggle"]})
        assert ad.disposition_orphans(s) == 1
        assert s.load_pr(1).section("analysis")["disposition"] == "needs-human"

    def test_idempotent(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1)
        assert ad.disposition_orphans(s) == 1
        assert ad.disposition_orphans(s) == 0   # already dispositioned at this head

    def test_head_move_redispositions(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1)
        ad.disposition_orphans(s)
        # head moves; re-summarize + re-stamp standalone at the new head, so the
        # old analysis is stale and the PR is still a confirmed singleton.
        rec = s.load_pr(1); rec.raw["meta"]["head_sha"] = "h2"; s.save_pr(rec)
        set_section(s, 1, "summary", dict(rec.section("summary")), against_head_sha="h2")
        set_section(s, 1, "cluster", {}, against_head_sha="h2")
        assert ad.disposition_orphans(s) == 1
        assert s.load_pr(1).section("analysis")["against_head_sha"] == "h2"

    def test_skips_clustered_pr(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        set_section(s, 1, "cluster", {"ids": [5]})   # a real cluster member
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis") is None

    def test_skips_not_yet_clustered(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)                                   # no cluster section at all
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis") is None

    def test_skips_stale_cluster_stamp(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        set_section(s, 1, "cluster", {}, against_head_sha="OLD")   # not current
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis") is None

    def test_skips_already_dispositioned(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1)
        set_section(s, 1, "analysis", {"disposition": "needs-human", "rationale": "by hand"})
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis")["rationale"] == "by hand"

    def test_close_dup_on_standalone_gets_redispositioned(self, tmp_path):
        # A PR was clustered with another, got close-dup analysis, then the cluster
        # was dissolved leaving it standalone. The stale close-dup (which names a
        # canonical PR that no longer shares a cluster) must not survive — the orphan
        # pass should overwrite it with a proper standalone disposition.
        s = Store(tmp_path)
        _standalone(s, 1)
        set_section(s, 1, "analysis", {"disposition": "close-dup", "rationale": "dup of #99",
                                       "canonical": 99})
        assert ad.disposition_orphans(s) == 1
        a = s.load_pr(1).section("analysis")
        assert a["disposition"] != "close-dup"  # re-dispositioned away from the stale dup

    def test_non_dup_already_dispositioned_still_skipped(self, tmp_path):
        # Other dispositions with a current analysis are still idempotently skipped.
        s = Store(tmp_path)
        _standalone(s, 1)
        set_section(s, 1, "analysis", {"disposition": "merge", "rationale": "clean"})
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis")["disposition"] == "merge"

    def test_clean_standalone_draft_is_surface_only(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1, draft=True)               # clean draft, no sticky signal
        _standalone(s, 2); rec = s.load_pr(2); rec.raw["meta"]["state"] = "merged"; s.save_pr(rec)
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis") is None   # no recommendation
        assert s.load_pr(2).section("analysis") is None

    def test_standalone_draft_never_gets_request_changes(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1, draft=True)
        rec = s.load_pr(1); rec.raw["reviews"]["greptile"]["score"] = 4; s.save_pr(rec)  # below bar
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(1).section("analysis") is None   # surface-only, not request-changes

    def test_malicious_standalone_draft_gets_needs_human(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1, draft=True)
        set_section(s, 1, "threat", {"verdict": "malicious", "signatures": ["capability-smuggle"]})
        assert ad.disposition_orphans(s) == 1
        assert s.load_pr(1).section("analysis")["disposition"] == "needs-human"

    def test_already_fixed_standalone_draft_gets_close_fixed(self, tmp_path):
        s = Store(tmp_path)
        _standalone(s, 1, draft=True)
        rec = s.load_pr(1); rec.raw["drift"]["state"] = "already-fixed"; s.save_pr(rec)
        assert ad.disposition_orphans(s) == 1
        assert s.load_pr(1).section("analysis")["disposition"] == "close-fixed"


class TestOrphansSkipsDependabotBumps:
    def _dependabot_standalone(self, store, n, head, diff_text, tmp, monkeypatch):
        from pipeline import diff_cache
        diffs = tmp / "diffs"; diffs.mkdir(exist_ok=True)
        monkeypatch.setattr(diff_cache, "DIFFS", diffs)
        (diffs / f"{head}.diff").write_text(diff_text)
        store.save_pr({"pr": n, "meta": {
            "title": f"bump {n}", "author": "dependabot[bot]", "state": "open",
            "draft": False, "head_sha": head, "checked_at": NOW}})
        set_section(store, n, "cluster", {})        # marked standalone

    def test_lockfile_bump_left_undispositioned(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        self._dependabot_standalone(
            s, 10, "hb", "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n+x\n",
            tmp_path, monkeypatch)
        assert ad.disposition_orphans(s) == 0
        assert s.load_pr(10).section("analysis") is None

    def test_dependabot_touching_source_still_dispositioned(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        self._dependabot_standalone(
            s, 11, "hc",
            "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n+x\n"
            "diff --git a/src/app.ts b/src/app.ts\n+evil\n",
            tmp_path, monkeypatch)
        assert ad.disposition_orphans(s) == 1     # shape guard fails → normal path
        assert s.load_pr(11).section("analysis")["disposition"] == "merge"


class TestCommitAnalysesDir:
    def test_commits_every_cluster_file_and_runs_post_steps(self, tmp_path):
        import json
        s = Store(tmp_path)
        for n in (1, 2, 3, 4):
            _pr(s, n)
        _cluster(s, 1, [1, 2]); _cluster(s, 2, [3, 4])
        # a confirmed-standalone PR — disposition_orphans (a post-step) must dispatch it
        _pr(s, 9)
        set_section(s, 9, "cluster", {})   # standalone stamp (no id), current
        outdir = tmp_path / "aout"
        outdir.mkdir()
        (outdir / "cluster-001.json").write_text(json.dumps({"cluster_id": 1, "outcome": "merge-ready",
            "rationale": "1 wins", "prs": [
                {"pr": 1, "disposition": "merge", "rationale": "best", "head_sha": "h1"},
                {"pr": 2, "disposition": "close-dup", "canonical": 1, "rationale": "dup", "head_sha": "h1"}]}))
        (outdir / "cluster-002.json").write_text(json.dumps({"cluster_id": 2, "outcome": "awaiting-authors",
            "rationale": "needs work", "prs": [
                {"pr": 3, "disposition": "request-changes", "asks": ["add a test"], "rationale": "x", "head_sha": "h1"},
                {"pr": 4, "disposition": "close-stale", "rationale": "old", "head_sha": "h1"}]}))
        res = ad.commit_analyses_dir(s, outdir)
        assert res["committed"] == 2 and res["failed"] == 0
        assert s.load_pr(1).section("analysis")["disposition"] == "merge"
        assert s.load_cluster(2).outcome == "awaiting-authors"
        # post-step ran: the standalone PR got a deterministic disposition
        assert res["orphans"] == 1 and s.load_pr(9).section("analysis") is not None

    def test_stray_non_payload_file_is_ignored(self, tmp_path):
        # an ANALYZE agent's failed API call can save a non-JSON page into the
        # out-dir (seen: a 502 Bad Gateway HTML saved as blame.json). The commit
        # reads only its own cluster-NNN.json payloads, so the stray is skipped.
        import json
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2)
        _cluster(s, 1, [1, 2])
        outdir = tmp_path / "aout"; outdir.mkdir()
        (outdir / "cluster-001.json").write_text(json.dumps({"cluster_id": 1, "outcome": "close-out",
            "rationale": "all stale", "prs": [
                {"pr": 1, "disposition": "close-stale", "rationale": "x", "head_sha": "h1"},
                {"pr": 2, "disposition": "close-stale", "rationale": "x", "head_sha": "h1"}]}))
        (outdir / "blame.json").write_text("<html><body>502 Bad Gateway</body></html>")
        res = ad.commit_analyses_dir(s, outdir)
        assert res["committed"] == 1 and res["failed"] == 0

    def test_partial_dir_commits_finished_clusters(self, tmp_path):
        import json
        s = Store(tmp_path)
        _pr(s, 1); _pr(s, 2)
        _cluster(s, 1, [1, 2])
        outdir = tmp_path / "aout"; outdir.mkdir()
        (outdir / "cluster-001.json").write_text(json.dumps({"cluster_id": 1, "outcome": "close-out",
            "rationale": "all stale", "prs": [
                {"pr": 1, "disposition": "close-stale", "rationale": "x", "head_sha": "h1"},
                {"pr": 2, "disposition": "close-stale", "rationale": "x", "head_sha": "h1"}]}))
        assert ad.commit_analyses_dir(s, outdir)["committed"] == 1

    def test_missing_dir_is_noop(self, tmp_path):
        s = Store(tmp_path)
        res = ad.commit_analyses_dir(s, tmp_path / "nope")
        assert res["committed"] == 0 and res["failed"] == 0


def test_reconcile_pr_picks_most_blocking_across_clusters(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1)
    _cluster(s, 7, [1])
    _cluster(s, 9, [1])
    s.edit_cluster(7).set_proposals([{"pr": 1, "disposition": "merge", "rationale": "keep"}])
    s.edit_cluster(9).set_proposals([{"pr": 1, "disposition": "close-dup",
                                      "rationale": "dup", "canonical": 2}])
    ad.reconcile_pr(s, 1)
    pr = s.load_pr(1)
    assert pr.disposition == "close-dup"          # close beats merge
    assert pr.raw["analysis"]["from_cluster"] == 9


def test_reconcile_pr_noop_without_proposals(tmp_path):
    s = Store(tmp_path)
    _pr(s, 1)
    ad.reconcile_pr(s, 1)                          # no clusters → no-op
    assert s.load_pr(1).disposition is None


def test_pending_does_not_load_pr_per_member(tmp_path):
    """pending() reads cluster members from one bulk all_prs(), not a load_pr per
    member — otherwise it is O(clustered PRs) network round-trips."""
    import unittest.mock as mock
    s = Store(tmp_path)
    for n in (1, 2, 3, 4):
        _pr(s, n)
    _cluster(s, 10, [1, 2])
    _cluster(s, 11, [3, 4])
    orig = s.load_pr
    calls: list[int] = []
    with mock.patch.object(s, "load_pr", side_effect=lambda n: (calls.append(n), orig(n))[1]):
        result = ad.pending(s)
    assert {c.id for c in result} == {10, 11}  # behavior unchanged
    assert calls == []                          # no per-member load_pr
