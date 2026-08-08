"""CLUSTER driver: wave selection, summary commits, stable-ID cluster commits."""
from pipeline import cluster_driver as cd
from pipeline import diff_cache
from pipeline.testsupport import set_section
from pipeline.freshness import SECTION_SCHEMA_VERSION, is_current
from pipeline.store import Store


NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, head="h1", state="open", draft=False, summary_sha=None):
    rec = {"pr": n, "meta": {"title": f"t{n}", "author": "a", "state": state, "draft": draft,
                             "head_sha": head, "checked_at": NOW}}
    if summary_sha:
        rec["summary"] = {"one_liner": "x", "subsystem": "ui",
                          "checked_at": NOW, "against_head_sha": summary_sha,
                          "schema_version": SECTION_SCHEMA_VERSION["summary"]}
    store.save_pr(rec)


def _spr(store, n, subsystem="ui", primary="p", head=None):
    """A summarized (current) PR with a given subsystem + primary_change."""
    head = head or f"h{n}"
    store.save_pr({"pr": n, "meta": {"title": f"t{n}", "author": "a", "state": "open",
                   "draft": False, "head_sha": head, "checked_at": NOW},
        "summary": {"one_liner": f"o{n}", "subsystem": subsystem, "mechanism": "m",
                    "identifiers": [], "paths": [], "primary_change": primary,
                    "secondary_changes": [], "checked_at": NOW, "against_head_sha": head,
                    "schema_version": SECTION_SCHEMA_VERSION["summary"]}})


def _read_unit_prs(cd):
    import json
    out = []
    for p in cd.STRADDLE_UNIT_DIR.glob("unit-*.json"):
        out.extend(json.loads(p.read_text())["prs"])
    return out


class TestWave:
    def test_picks_open_prs_without_current_summary(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)                      # no summary → needs one
        _pr(s, 2, summary_sha="h1")    # current summary → skip
        _pr(s, 3, summary_sha="OLD")   # stale summary → needs one
        _pr(s, 4, state="merged")      # not open → skip
        _pr(s, 5, draft=True)          # draft → included (no summary yet)
        assert [p.pr for p in cd.wave(s)] == [1, 3, 5]

    def test_max_caps_wave(self, tmp_path):
        s = Store(tmp_path)
        for n in range(1, 6):
            _pr(s, n)
        assert len(cd.wave(s, max_n=2)) == 2


class TestCommitSummaries:
    def test_writes_section_stamped_to_given_sha(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        n_ok, errs = cd.commit_summaries(s, [
            {"pr": 1, "head_sha": "h1", "one_liner": "fixes lock leak",
             "subsystem": "execution-locks", "mechanism": "clears field on close",
             "identifiers": ["executionRunId"], "paths": ["server/src/locks.ts"]},
        ])
        assert n_ok == 1 and errs == []
        rec = s.load_pr(1)
        assert rec.section("summary")["one_liner"] == "fixes lock leak"
        assert rec.section("summary")["against_head_sha"] == "h1"

    def test_stamps_current_schema_version_so_summary_reads_fresh(self, tmp_path):
        # a freshly committed summary must satisfy is_current — otherwise the
        # next wave would re-summarize a PR it just summarized, forever.
        s = Store(tmp_path)
        _pr(s, 1)
        cd.commit_summaries(s, [
            {"pr": 1, "head_sha": "h1", "one_liner": "x", "subsystem": "ui"}])
        assert is_current(s.load_pr(1), "summary")

    def test_rejects_unknown_pr_and_unknown_subsystem(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        n_ok, errs = cd.commit_summaries(s, [
            {"pr": 99, "head_sha": "h1", "one_liner": "x", "subsystem": "ui"},
            {"pr": 1, "head_sha": "h1", "one_liner": "x", "subsystem": "not-a-subsystem"},
        ])
        assert n_ok == 0 and len(errs) == 2

    def test_sha_mismatch_marks_immediately_stale_but_records(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, head="NEWER")
        n_ok, errs = cd.commit_summaries(s, [
            {"pr": 1, "head_sha": "h1", "one_liner": "x", "subsystem": "ui"}])
        assert n_ok == 1
        # stamped against the sha that was summarized, not the newer head
        assert s.load_pr(1).section("summary")["against_head_sha"] == "h1"

    def test_persists_primary_and_secondary_changes(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        cd.commit_summaries(s, [
            {"pr": 1, "head_sha": "h1", "one_liner": "include routine_execution issues",
             "subsystem": "inbox", "mechanism": "list() skips exclusion when assigneeAgentId set",
             "identifiers": ["assigneeAgentId"], "paths": ["server/src/services/issues.ts"],
             "primary_change": "agent inbox queries include routine_execution issues",
             "secondary_changes": ["sidebar badge excludes read issues"]},
        ])
        sm = s.load_pr(1).section("summary")
        assert sm["primary_change"] == "agent inbox queries include routine_execution issues"
        assert sm["secondary_changes"] == ["sidebar badge excludes read issues"]

    def test_primary_change_defaults_to_one_liner_when_absent(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        cd.commit_summaries(s, [
            {"pr": 1, "head_sha": "h1", "one_liner": "fixes lock leak", "subsystem": "ui"},
        ])
        sm = s.load_pr(1).section("summary")
        assert sm["primary_change"] == "fixes lock leak"
        assert sm["secondary_changes"] == []


class TestCommitSummariesDir:
    def test_commits_every_batch_file_in_dir(self, tmp_path):
        import json
        s = Store(tmp_path)
        _pr(s, 1)
        _pr(s, 2)
        outdir = tmp_path / "out"
        outdir.mkdir()
        (outdir / "batch-000.json").write_text(json.dumps(
            [{"pr": 1, "head_sha": "h1", "one_liner": "a", "subsystem": "ui"}]))
        (outdir / "batch-001.json").write_text(json.dumps(
            [{"pr": 2, "head_sha": "h1", "one_liner": "b", "subsystem": "cli"}]))
        ok, errs = cd.commit_summaries_dir(s, outdir)
        assert ok == 2 and errs == []
        assert is_current(s.load_pr(1), "summary")
        assert is_current(s.load_pr(2), "summary")

    def test_stray_non_payload_file_is_ignored(self, tmp_path):
        # a summarize agent's failed fetch can drop a non-JSON page into the
        # out-dir; the commit reads only its own batch-NNN.json payloads.
        import json
        s = Store(tmp_path)
        _pr(s, 1)
        outdir = tmp_path / "out"
        outdir.mkdir()
        (outdir / "batch-000.json").write_text(json.dumps(
            [{"pr": 1, "head_sha": "h1", "one_liner": "a", "subsystem": "ui"}]))
        (outdir / "blame.json").write_text("<html><body>502 Bad Gateway</body></html>")
        ok, errs = cd.commit_summaries_dir(s, outdir)
        assert ok == 1 and errs == []
        assert is_current(s.load_pr(1), "summary")

    def test_partial_dir_still_commits_finished_batches(self, tmp_path):
        # the durability guarantee: a run that died after batch 0 leaves batch-000
        # on disk; committing the dir still lands those PRs.
        import json
        s = Store(tmp_path)
        _pr(s, 1)
        outdir = tmp_path / "out"
        outdir.mkdir()
        (outdir / "batch-000.json").write_text(json.dumps(
            [{"pr": 1, "head_sha": "h1", "one_liner": "a", "subsystem": "ui"}]))
        ok, errs = cd.commit_summaries_dir(s, outdir)
        assert ok == 1 and is_current(s.load_pr(1), "summary")

    def test_missing_dir_is_zero_not_error(self, tmp_path):
        s = Store(tmp_path)
        ok, errs = cd.commit_summaries_dir(s, tmp_path / "nope")
        assert ok == 0 and errs == []


class TestCommitClustersDir:
    def test_commits_proposals_from_every_unit_file(self, tmp_path):
        import json
        s = Store(tmp_path)
        for n in (1, 2, 3, 4):
            _pr(s, n)
        outdir = tmp_path / "cout"
        outdir.mkdir()
        (outdir / "unit-000.json").write_text(json.dumps([{"root_problem": "a", "prs": [1, 2]}]))
        (outdir / "unit-001.json").write_text(json.dumps([{"root_problem": "b", "prs": [3, 4]}]))
        res = cd.commit_clusters_dir(s, outdir)
        assert sorted(res["created"]) == [1, 2]
        assert s.load_pr(1).cluster_ids[0] in (1, 2)
        assert {c.root_problem for c in s.all_clusters().values()} == {"a", "b"}

    def test_stray_non_payload_file_is_ignored(self, tmp_path):
        # the commit reads only its own unit-NNN.json payloads, so a non-JSON
        # file a propose-clusters agent dropped in the dir is skipped.
        import json
        s = Store(tmp_path)
        for n in (1, 2):
            _pr(s, n)
        outdir = tmp_path / "cout"
        outdir.mkdir()
        (outdir / "unit-000.json").write_text(json.dumps([{"root_problem": "a", "prs": [1, 2]}]))
        (outdir / "blame.json").write_text("<html><body>502 Bad Gateway</body></html>")
        res = cd.commit_clusters_dir(s, outdir)
        assert res["created"] == [1]   # the one unit's proposal committed; blame.json skipped
        assert s.load_pr(1).cluster_ids == s.load_pr(2).cluster_ids

    def test_missing_dir_is_noop(self, tmp_path):
        s = Store(tmp_path)
        res = cd.commit_clusters_dir(s, tmp_path / "nope")
        assert res["created"] == [] and res["updated"] == []


class TestResetClusters:
    def test_drops_clusters_and_clears_backrefs(self, tmp_path):
        s = Store(tmp_path)
        for n in (1, 2, 3):
            _pr(s, n)
        cd.commit_clusters(s, [{"root_problem": "x", "prs": [1, 2, 3]}])
        assert s.load_cluster(1) is not None and s.load_pr(1).section("cluster")
        res = cd.reset_clusters(s)
        assert res == {"clusters_removed": 1, "backrefs_cleared": 3}
        assert s.all_clusters() == {}
        assert all(s.load_pr(n).section("cluster") is None for n in (1, 2, 3))
        # PRs themselves survive the reset
        assert s.load_pr(1) is not None

    def test_idempotent_on_empty(self, tmp_path):
        s = Store(tmp_path)
        assert cd.reset_clusters(s) == {"clusters_removed": 0, "backrefs_cleared": 0}

    def test_reaps_aged_tombstones(self, tmp_path):
        from sqlalchemy import update

        from pipeline import schema
        s = Store(tmp_path)
        for n in (1, 2):
            _pr(s, n)
        cd.commit_clusters(s, [{"root_problem": "x", "prs": [1, 2]}])
        cd.reset_clusters(s)                          # tombstones cluster 1 (fresh)
        _, deleted, _ = s.clusters_since(None)
        assert deleted == [1]                          # tombstone present, not yet reaped
        with s.engine.begin() as conn:                 # age it past the reap cutoff
            conn.execute(update(schema.clusters).values(
                saved_at="2000-01-01T00:00:00.000000+00:00"))
        cd.reset_clusters(s)                           # reaps the aged tombstone out of band
        _, deleted, _ = s.clusters_since(None)
        assert deleted == []                           # row hard-removed


class TestResetStaleMemberships:
    def _move_head(self, store, n, new_head, *, resummarize=True):
        """Simulate a force-push: bump meta.head, optionally re-stamp the summary
        to the new head (a SUMMARIZE pass ran), but leave the cluster backref
        stamped against the old head — exactly the stale-membership state."""
        rec = store.load_pr(n).raw
        rec["meta"]["head_sha"] = new_head
        if resummarize:
            rec["summary"]["against_head_sha"] = new_head
        store.save_pr(rec)

    def test_detaches_stale_member_keeps_fresh_ones(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="old1")
        _spr(s, 2, head="old2")
        cd.commit_clusters(s, [{"root_problem": "r", "prs": [1, 2]}])
        assert is_current(s.load_pr(2), "cluster")
        self._move_head(s, 2, "new2")                # #2 force-pushed + re-summarized
        assert not is_current(s.load_pr(2), "cluster")  # membership now stale

        res = cd.reset_stale_memberships(s)
        assert res == {"detached": [2], "emptied_clusters": [], "standalone_cleared": []}
        assert s.load_pr(2).section("cluster") is None   # cleared → assign re-homes it
        assert s.load_pr(1).cluster_ids == [1]           # fresh member untouched
        assert s.load_cluster(1).prs == [1]              # cluster survives as singleton

    def test_deletes_a_cluster_emptied_by_the_detach(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="old1")
        _spr(s, 2, head="old2")
        cd.commit_clusters(s, [{"root_problem": "r", "prs": [1, 2]}])
        self._move_head(s, 1, "new1")
        self._move_head(s, 2, "new2")                # both stale → cluster empties

        res = cd.reset_stale_memberships(s)
        assert res["detached"] == [1, 2]
        assert res["emptied_clusters"] == [1]
        assert s.all_clusters() == {}

    def test_skips_when_summary_is_also_stale(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="old1")
        _spr(s, 2, head="old2")
        cd.commit_clusters(s, [{"root_problem": "r", "prs": [1, 2]}])
        self._move_head(s, 2, "new", resummarize=False)  # head moved, summary not refreshed

        res = cd.reset_stale_memberships(s)
        assert res == {"detached": [], "emptied_clusters": [], "standalone_cleared": []}
        assert s.load_pr(2).cluster_ids == [1]           # left for the next SUMMARIZE pass

    def test_detaches_a_straddler_from_every_cluster(self, tmp_path):
        s = Store(tmp_path)
        for n in (1, 2, 3):
            _spr(s, n, head=f"old{n}")
        cd.commit_clusters(s, [{"root_problem": "a", "prs": [1, 2]},
                               {"root_problem": "b", "prs": [2, 3]}])
        assert s.load_pr(2).cluster_ids == [1, 2]    # straddler
        self._move_head(s, 2, "new")

        res = cd.reset_stale_memberships(s)
        assert res["detached"] == [2]
        assert s.load_pr(2).section("cluster") is None
        assert s.load_cluster(1).prs == [1] and s.load_cluster(2).prs == [3]

    def test_noop_when_nothing_stale(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="h1")
        _spr(s, 2, head="h2")
        cd.commit_clusters(s, [{"root_problem": "r", "prs": [1, 2]}])
        assert cd.reset_stale_memberships(s) == {
            "detached": [], "emptied_clusters": [], "standalone_cleared": []}

    def test_clears_a_stale_standalone_stamp(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="old")
        cd.commit_clusters(s, [])                    # considered, placed nowhere → standalone
        assert s.load_pr(1).section("cluster") is not None
        self._move_head(s, 1, "new")                 # force-pushed + re-summarized

        res = cd.reset_stale_memberships(s)
        assert res["standalone_cleared"] == [1]
        assert res["detached"] == []
        assert s.load_pr(1).section("cluster") is None   # assign-pass candidate again

    def test_keeps_a_current_standalone_stamp(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="h")
        cd.commit_clusters(s, [])

        res = cd.reset_stale_memberships(s)
        assert res["standalone_cleared"] == []
        assert (s.load_pr(1).section("cluster") or {}).get("ids") == []

    def test_skips_a_stale_standalone_whose_summary_is_stale(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="old")
        cd.commit_clusters(s, [])
        self._move_head(s, 1, "new", resummarize=False)  # head moved, summary not refreshed

        res = cd.reset_stale_memberships(s)
        assert res["standalone_cleared"] == []
        # left for the next SUMMARIZE pass — no current summary to re-home on
        assert (s.load_pr(1).section("cluster") or {}).get("ids") == []


class TestStaleIngestWarning:
    def test_no_ingest_on_record_warns(self, tmp_path):
        s = Store(tmp_path)
        assert "no INGEST" in cd.stale_ingest_warning(s)

    def test_old_ingest_warns_on_age(self, tmp_path):
        s = Store(tmp_path)
        s.append_run({"phase": "ingest", "finished": "2026-06-10T00:00:00+00:00"})
        w = cd.stale_ingest_warning(s, max_age_hours=12, now="2026-06-11T00:00:00+00:00")  # +24h
        assert "INGEST was 24h ago" in w

    def test_recent_ingest_no_warning(self, tmp_path):
        s = Store(tmp_path)
        s.append_run({"phase": "ingest", "finished": "2026-06-10T00:00:00+00:00"})
        assert cd.stale_ingest_warning(s, max_age_hours=12,
                                       now="2026-06-10T01:00:00+00:00") is None  # +1h

    def test_uses_the_most_recent_ingest_run(self, tmp_path):
        s = Store(tmp_path)
        s.append_run({"phase": "ingest", "finished": "2026-06-01T00:00:00+00:00"})  # old
        s.append_run({"phase": "cluster:commit", "finished": "2026-06-10T00:00:00+00:00"})
        s.append_run({"phase": "ingest", "finished": "2026-06-10T00:00:00+00:00"})  # recent
        assert cd.stale_ingest_warning(s, max_age_hours=12,
                                       now="2026-06-10T01:00:00+00:00") is None

    def test_write_cluster_units_surfaces_the_warning(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1)
        _spr(s, 2)                                            # one ui unit; no ingest on record
        res = cd.write_cluster_units(s)
        assert res["count"] >= 1
        assert "no INGEST" in res["stale_input_warning"]


class TestCommitClusters:
    def _seed(self, tmp_path):
        s = Store(tmp_path)
        for n in (1, 2, 3, 4, 5, 6):
            _pr(s, n)
        return s

    def test_new_clusters_get_sequential_ids_and_member_backrefs(self, tmp_path):
        s = self._seed(tmp_path)
        result = cd.commit_clusters(s, [
            {"root_problem": "lock leak", "prs": [1, 2]},
            {"root_problem": "badge count", "prs": [3, 4, 5]},
        ])
        assert sorted(result["created"]) == [1, 2]
        assert s.load_cluster(1).prs == [1, 2]
        assert s.load_pr(3).cluster_ids == [2]

    def test_singletons_dropped(self, tmp_path):
        s = self._seed(tmp_path)
        result = cd.commit_clusters(s, [{"root_problem": "alone", "prs": [1]}])
        assert result["created"] == [] and result["dropped_singletons"] == 1

    def test_overlap_reuses_existing_id(self, tmp_path):
        s = self._seed(tmp_path)
        cd.commit_clusters(s, [{"root_problem": "lock leak", "prs": [1, 2, 3]}])
        result = cd.commit_clusters(s, [{"root_problem": "lock leak v2", "prs": [1, 2, 4]}])
        assert result["created"] == [] and result["updated"] == [1]
        c = s.load_cluster(1)
        assert c.prs == [1, 2, 4] and c.root_problem == "lock leak v2"
        # PR 3 left the cluster → marked standalone
        assert s.load_pr(3).cluster_ids == []
        assert s.load_pr(4).cluster_ids == [1]

    def test_disjoint_new_cluster_does_not_steal_id(self, tmp_path):
        s = self._seed(tmp_path)
        cd.commit_clusters(s, [{"root_problem": "a", "prs": [1, 2]}])
        result = cd.commit_clusters(s, [{"root_problem": "b", "prs": [5, 6]}])
        assert result["created"] == [2]


class TestMarkStandalone:
    def test_stamps_summarized_unclustered_pr_standalone(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, summary_sha="h1")          # summarized, no cluster → standalone
        n = cd.mark_standalone(s)
        assert n == 1
        rec = s.load_pr(1)
        assert rec.cluster_ids == []
        assert is_current(rec, "cluster")    # distinguishable from never-clustered

    def test_skips_unsummarized_pr(self, tmp_path):
        # not yet summarized → never an input to a clustering pass
        s = Store(tmp_path)
        _pr(s, 1)
        assert cd.mark_standalone(s) == 0
        assert s.load_pr(1).section("cluster") is None

    def test_skips_clustered_member(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, summary_sha="h1")
        _pr(s, 2, summary_sha="h1")
        cd.commit_clusters(s, [{"root_problem": "x", "prs": [1, 2]}])
        before = s.load_pr(1).section("cluster")
        assert cd.mark_standalone(s) == 0    # both are cluster members
        assert s.load_pr(1).section("cluster") == before

    def test_skips_closed_marks_draft(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, summary_sha="h1", draft=True)
        _pr(s, 2, summary_sha="h1", state="merged")
        assert cd.mark_standalone(s) == 1     # draft is standalone-eligible; closed is not
        assert is_current(s.load_pr(1), "cluster")      # draft got the standalone stamp
        assert s.load_pr(2).section("cluster") is None

    def test_idempotent(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, summary_sha="h1")
        assert cd.mark_standalone(s) == 1
        assert cd.mark_standalone(s) == 0    # already stamped at this head

    def test_restamps_when_head_moved(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, summary_sha="h1")
        cd.mark_standalone(s)
        # head moves; the stale standalone stamp must be refreshed once re-summarized
        rec = s.load_pr(1)
        rec.raw["meta"]["head_sha"] = "h2"
        rec.raw["summary"]["against_head_sha"] = "h2"
        s.save_pr(rec)
        assert cd.mark_standalone(s) == 1
        assert is_current(s.load_pr(1), "cluster")

    def test_commit_clusters_marks_leftovers_standalone(self, tmp_path):
        s = Store(tmp_path)
        for n in (1, 2, 3):
            _pr(s, n, head=f"h{n}", summary_sha=f"h{n}")
        result = cd.commit_clusters(s, [{"root_problem": "x", "prs": [1, 2]}])
        assert result["standalone"] == 1    # PR 3 was considered, left standalone
        assert s.load_pr(3).cluster_ids == []
        assert is_current(s.load_pr(3), "cluster")
        assert s.load_pr(1).cluster_ids == [1]


class TestGroups:
    def test_forwards_primary_and_secondary_changes(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        cd.commit_summaries(s, [
            {"pr": 1, "head_sha": "h1", "one_liner": "ol", "subsystem": "inbox",
             "mechanism": "m", "identifiers": ["x"], "paths": ["p"],
             "primary_change": "primary thing", "secondary_changes": ["secondary thing"]},
        ])
        entry = cd.groups(s)["inbox"][0]
        assert entry["primary_change"] == "primary thing"
        assert entry["secondary_changes"] == ["secondary thing"]


class TestIdenticalHeadMemberships:
    def test_full_cluster_adds_identical_head_pr_from_other_subsystem(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, subsystem="ui", primary="first framing", head="same")
        _spr(s, 2, subsystem="cli", primary="second framing", head="same")
        _spr(s, 3, subsystem="ui", primary="related", head="other")

        cd.commit_clusters(s, [{"root_problem": "shared", "prs": [1, 3]}])

        assert s.load_pr(1).cluster_ids == s.load_pr(2).cluster_ids
        assert s.load_pr(2).cluster_ids == s.load_pr(3).cluster_ids

    def test_incremental_assignment_overrides_duplicate_standalone_result(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1, head="existing")
        _spr(s, 2, subsystem="ui", primary="first framing", head="same")
        _spr(s, 3, subsystem="cli", primary="second framing", head="same")
        s.save_cluster({"id": 5, "root_problem": "shared", "prs": [1],
                        "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})

        cd.commit_assignments(s, {
            "joins": [{"pr": 2, "cluster_id": 5}],
            "new_clusters": [],
            "standalone": [3],
        })

        assert s.load_pr(2).cluster_ids == [5]
        assert s.load_pr(3).cluster_ids == [5]
        assert sorted(s.load_cluster(5).prs) == [1, 2, 3]
        assert s.load_cluster(5).outcome is None


class TestWaveSkipsDependabotBumps:
    def _dependabot(self, store, n, head, files):
        store.save_pr({"pr": n, "meta": {
            "title": f"bump {n}", "author": "dependabot[bot]", "state": "open",
            "draft": False, "head_sha": head, "checked_at": NOW}})

    def test_skips_lockfile_only_bump(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        monkeypatch.setattr(diff_cache, "DIFFS", diffs)
        self._dependabot(s, 10, "hb", None)
        (diffs / "hb.diff").write_text(
            "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n+x\n")
        _pr(s, 1)                       # ordinary PR still waves
        assert [p.pr for p in cd.wave(s)] == [1]

    def test_keeps_dependabot_pr_that_touches_source(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        monkeypatch.setattr(diff_cache, "DIFFS", diffs)
        self._dependabot(s, 11, "hc", None)
        (diffs / "hc.diff").write_text(
            "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n+x\n"
            "diff --git a/src/app.ts b/src/app.ts\n+evil\n")
        assert [p.pr for p in cd.wave(s)] == [11]


class TestCommitAssignments:
    """Incremental assignment: new PRs join existing (frozen) clusters, form new
    ones, or go standalone — existing clusters are only ever appended to."""

    def test_join_appends_to_existing_cluster_and_reopens_it(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 2); _spr(s, 3, primary="joins the cluster")
        s.save_cluster({"id": 5, "root_problem": "shared", "prs": [1, 2],
                        "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        set_section(s, 2, "cluster", {"ids": [5]})
        res = cd.commit_assignments(s, {"joins": [{"pr": 3, "cluster_id": 5}],
                                        "new_clusters": [], "standalone": []})
        assert res["joined"] == 1
        assert sorted(s.load_cluster(5).prs) == [1, 2, 3]   # appended, others kept
        assert s.load_pr(3).cluster_ids == [5]
        assert s.load_cluster(5).outcome is None            # reopened for re-analysis

    def test_new_cluster_from_new_prs(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 10); _spr(s, 11)
        res = cd.commit_assignments(s, {"joins": [],
            "new_clusters": [{"root_problem": "new shared problem", "prs": [10, 11]}],
            "standalone": []})
        assert res["created"] == 1
        cid = s.load_pr(10).cluster_ids[0]
        assert s.load_pr(11).cluster_ids == [cid]
        c = s.load_cluster(cid)
        assert c.root_problem == "new shared problem"
        assert sorted(c.prs) == [10, 11]

    def test_new_cluster_id_does_not_collide_with_existing(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 10); _spr(s, 11)
        s.save_cluster({"id": 42, "root_problem": "x", "prs": [99], "checked_at": NOW})
        cd.commit_assignments(s, {"joins": [],
            "new_clusters": [{"root_problem": "n", "prs": [10, 11]}], "standalone": []})
        assert s.load_pr(10).cluster_ids[0] > 42

    def test_singleton_new_cluster_is_dropped(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 10)
        res = cd.commit_assignments(s, {"joins": [],
            "new_clusters": [{"root_problem": "x", "prs": [10]}], "standalone": []})
        assert res["created"] == 0
        assert s.load_pr(10).section("cluster") is None   # left unplaced → re-considered

    def test_standalone_is_stamped_current(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 20)
        res = cd.commit_assignments(s, {"joins": [], "new_clusters": [],
                                        "standalone": [20]})
        assert res["standalone"] == 1
        assert s.load_pr(20).cluster_ids == []
        assert is_current(s.load_pr(20), "cluster")   # freshly stamped standalone

    def test_join_to_missing_cluster_errors_and_writes_nothing(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1)
        res = cd.commit_assignments(s, {"joins": [{"pr": 1, "cluster_id": 999}],
                                        "new_clusters": [], "standalone": []})
        assert res["joined"] == 0 and res["errors"]
        assert s.load_pr(1).section("cluster") is None

    def test_standalone_never_overrides_a_real_assignment(self, tmp_path):
        """If the agent slips and lists a PR in both a join and standalone, the
        real assignment wins — the standalone stamp must not clobber it."""
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 2)
        s.save_cluster({"id": 5, "root_problem": "x", "prs": [1], "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        res = cd.commit_assignments(s, {"joins": [{"pr": 2, "cluster_id": 5}],
                                        "new_clusters": [], "standalone": [2]})
        assert s.load_pr(2).cluster_ids == [5]   # join wins
        assert res["standalone"] == 0

    def test_pr_in_two_joins_lands_in_both_clusters(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 2); _spr(s, 3, primary="straddles both")
        for cid, members in ((5, [1]), (8, [2])):
            s.save_cluster({"id": cid, "root_problem": f"rp{cid}", "prs": members,
                            "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        set_section(s, 2, "cluster", {"ids": [8]})
        set_section(s, 3, "cluster", {"ids": [5]})            # already in 5; also joins 8
        res = cd.commit_assignments(s, {"joins": [{"pr": 3, "cluster_id": 8}],
                                        "new_clusters": [], "standalone": []})
        assert res["joined"] == 1
        assert s.load_pr(3).cluster_ids == [5, 8]             # additive: kept 5, gained 8
        assert sorted(s.load_cluster(8).prs) == [2, 3]
        assert s.load_cluster(8).outcome is None              # gained a member → reopened
        assert s.load_cluster(5).outcome == "merge-ready"     # untouched

    def test_join_to_cluster_already_a_member_is_idempotent(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 3)
        s.save_cluster({"id": 5, "root_problem": "rp", "prs": [1, 3],
                        "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 3, "cluster", {"ids": [5]})
        cd.commit_assignments(s, {"joins": [{"pr": 3, "cluster_id": 5}],
                                  "new_clusters": [], "standalone": []})
        assert s.load_pr(3).cluster_ids == [5]
        assert s.load_cluster(5).outcome == "merge-ready"     # already a member → NOT reopened

    def test_pr_primary_new_cluster_and_secondary_join(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 10); _spr(s, 11)
        s.save_cluster({"id": 5, "root_problem": "existing", "prs": [1],
                        "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        cd.commit_assignments(s, {
            "joins": [{"pr": 10, "cluster_id": 5}],           # 10 also straddles existing cluster 5
            "new_clusters": [{"root_problem": "fresh", "prs": [10, 11]}],
            "standalone": []})
        new_cid = next(c for c in s.load_pr(11).cluster_ids)
        assert set(s.load_pr(10).cluster_ids) == {5, new_cid} # in both its new cluster and 5
        assert s.load_cluster(5).outcome is None


class TestCommitAssignmentsDir:
    def test_commits_each_unit_payload(self, tmp_path):
        import json
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 2); _spr(s, 3); _spr(s, 4)
        s.save_cluster({"id": 5, "root_problem": "x", "prs": [1],
                        "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        outdir = tmp_path / "assignout"; outdir.mkdir()
        (outdir / "unit-000.json").write_text(json.dumps(
            {"joins": [{"pr": 2, "cluster_id": 5}], "new_clusters": [], "standalone": []}))
        (outdir / "unit-001.json").write_text(json.dumps(
            {"joins": [], "new_clusters": [{"root_problem": "n", "prs": [3, 4]}],
             "standalone": []}))
        res = cd.commit_assignments_dir(s, outdir)
        assert res["joined"] == 1 and res["created"] == 1
        assert sorted(s.load_cluster(5).prs) == [1, 2]
        assert s.load_pr(3).cluster_ids == s.load_pr(4).cluster_ids

    def test_stray_non_payload_file_is_ignored(self, tmp_path):
        # the commit reads only its own unit-NNN.json payloads, so a non-JSON
        # file an assign agent dropped in the dir is skipped.
        import json
        s = Store(tmp_path)
        _spr(s, 1); _spr(s, 2)
        s.save_cluster({"id": 5, "root_problem": "x", "prs": [1],
                        "outcome": "merge-ready", "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        outdir = tmp_path / "assignout"; outdir.mkdir()
        (outdir / "unit-000.json").write_text(json.dumps(
            {"joins": [{"pr": 2, "cluster_id": 5}], "new_clusters": [], "standalone": []}))
        (outdir / "blame.json").write_text("<html><body>502 Bad Gateway</body></html>")
        res = cd.commit_assignments_dir(s, outdir)
        assert res["joined"] == 1

    def test_missing_dir_is_noop(self, tmp_path):
        s = Store(tmp_path)
        res = cd.commit_assignments_dir(s, tmp_path / "nope")
        assert res["joined"] == 0 and res["created"] == 0


class TestStraddleUnits:
    """Backfill unit builder: already-clustered PRs + their subsystem's existing
    clusters, annotated with each PR's current memberships."""

    def _cluster(self, s, cid, members, sub="ui"):
        s.save_cluster({"id": cid, "root_problem": f"rp{cid}", "prs": members,
                        "outcome": "merge-ready", "checked_at": NOW})
        for n in members:
            set_section(s, n, "cluster", {"ids": [cid]})

    def test_includes_clustered_prs_with_current_memberships(self, tmp_path):
        import json
        s = Store(tmp_path)
        _spr(s, 1, subsystem="ui"); _spr(s, 2, subsystem="ui")
        self._cluster(s, 5, [1]); self._cluster(s, 8, [2])
        res = cd.straddle_units(s)
        assert res["prs"] == 2 and res["count"] >= 1
        units = [json.loads(p.read_text())
                 for p in cd.STRADDLE_UNIT_DIR.glob("unit-*.json")]
        ui = next(u for u in units if u["subsystem"] == "ui")
        prs = {p["pr"]: p for p in ui["prs"]}
        assert prs[1]["current_clusters"] == [5]
        assert prs[2]["current_clusters"] == [8]
        assert {c["id"] for c in ui["existing_clusters"]} == {5, 8}

    def test_excludes_never_clustered_and_standalone(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1); self._cluster(s, 5, [1])                   # clustered → included
        _spr(s, 2)                                              # never-clustered → excluded
        _spr(s, 3); set_section(s, 3, "cluster", {"ids": []})  # standalone → excluded
        res = cd.straddle_units(s)
        assert {p["pr"] for p in _read_unit_prs(cd)} == {1}
        assert res["prs"] == 1

    def test_includes_draft_excludes_closed_and_unsummarized(self, tmp_path):
        s = Store(tmp_path)
        _spr(s, 1); self._cluster(s, 5, [1])
        _spr(s, 2); self._cluster(s, 6, [2])
        rec = s.load_pr(2); rec.raw["meta"]["draft"] = True; s.save_pr(rec)   # draft → included
        _pr(s, 9, summary_sha=None)                                            # no summary → excluded
        set_section(s, 9, "cluster", {"ids": [5]})
        cd.straddle_units(s)
        assert {p["pr"] for p in _read_unit_prs(cd)} == {1, 2}

    def test_chunks_at_limit(self, tmp_path):
        s = Store(tmp_path)
        for n in range(1, 6):
            _spr(s, n, subsystem="ui"); self._cluster(s, 10 + n, [n])
        res = cd.straddle_units(s, chunk=2)
        assert res["count"] == 3          # 5 PRs / 2 per unit → 3 units
        assert res["prs"] == 5


class TestStraddleCLI:
    def test_write_straddle_units_verb(self, tmp_path, capsys):
        import json
        s = Store(tmp_path)
        _spr(s, 1)
        s.save_cluster({"id": 5, "root_problem": "rp", "prs": [1], "checked_at": NOW})
        set_section(s, 1, "cluster", {"ids": [5]})
        rc = cd.main(["write-straddle-units", "--store", str(tmp_path)])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["prs"] == 1 and out["count"] >= 1


def test_assign_units_does_not_load_pr_per_member(tmp_path):
    """assign_units() reads existing clusters' members from one bulk all_prs(),
    not a load_pr per member across every cluster."""
    import unittest.mock as mock
    s = Store(tmp_path)
    _spr(s, 1); _spr(s, 2)                     # clustered members
    _spr(s, 3, primary="brand new")           # never-clustered → assign candidate
    s.save_cluster({"id": 5, "root_problem": "rp", "prs": [], "checked_at": NOW})
    s.edit_cluster(5).set_members([1, 2])     # wires cluster.prs + each pr.cluster section
    orig = s.load_pr
    calls: list[int] = []
    with mock.patch.object(s, "load_pr", side_effect=lambda n: (calls.append(n), orig(n))[1]):
        res = cd.assign_units(s)
    assert res["new_prs"] == 1                 # PR 3 is the lone candidate (behavior unchanged)
    assert calls == []                          # no per-member load_pr
