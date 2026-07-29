"""Store contract: the ONLY accessor for pipeline/store. Everything validates
on write; reads never need defensive parsing."""
import pytest

from pipeline import storekit
from pipeline.testsupport import set_section
from pipeline.store import Store, ValidationError


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def _pr(n=1234, **over):
    rec = {
        "pr": n,
        "meta": {
            "title": "fix: a bug",
            "author": "someone",
            "state": "open",
            "draft": False,
            "head_sha": "abc123",
            "base": "main",
            "url": f"https://github.com/test-owner/test-repo/pull/{n}",
            "created_at": "2026-06-01T00:00:00+00:00",
            "updated_at": "2026-06-09T00:00:00+00:00",
            "checked_at": "2026-06-10T00:00:00+00:00",
        },
    }
    rec.update(over)
    return rec


class TestPRRoundTrip:
    def test_save_load(self, store):
        store.save_pr(_pr())
        rec = store.load_pr(1234)
        assert rec.raw["meta"]["title"] == "fix: a bug"

    def test_load_missing_returns_none(self, store):
        assert store.load_pr(999) is None

    def test_all_prs(self, store):
        store.save_pr(_pr(1))
        store.save_pr(_pr(2))
        assert set(store.all_prs()) == {1, 2}

    def test_all_prs_omits_body_but_keeps_other_meta(self, store):
        store.save_pr(_pr(1, meta=dict(_pr(1)["meta"], body="the full description")))
        recs = store.all_prs()
        assert recs[1].body is None                       # body stripped from bulk read
        assert recs[1].raw["meta"]["title"] == "fix: a bug"  # rest of meta intact

    def test_pr_bodies_hydrates_on_demand(self, store):
        store.save_pr(_pr(1, meta=dict(_pr(1)["meta"], body="the full description")))
        store.save_pr(_pr(2))  # no body
        assert store.pr_bodies([1, 2]) == {1: "the full description", 2: None}
        assert store.pr_bodies([]) == {}
        # the body is still persisted — load_pr returns the full record
        assert store.load_pr(1).body == "the full description"

    def test_write_is_atomic_no_partial_file_on_validation_error(self, store):
        with pytest.raises(ValidationError):
            store.save_pr({"pr": 7})  # no meta
        assert store.load_pr(7) is None


class TestPRValidation:
    def test_rejects_unknown_disposition(self, store):
        rec = _pr(analysis={"disposition": "stack", "checked_at": "x", "against_head_sha": "abc123"})
        with pytest.raises(ValidationError, match="disposition"):
            store.save_pr(rec)

    def test_rejects_unknown_security_verdict(self, store):
        rec = _pr(security={"verdict": "MAUVE", "findings": [], "checked_at": "x", "against_head_sha": "abc123"})
        with pytest.raises(ValidationError, match="verdict"):
            store.save_pr(rec)

    def test_rejects_unknown_drift_state(self, store):
        rec = _pr(drift={"state": "reclassified", "checked_at": "x", "against_head_sha": "abc123"})
        with pytest.raises(ValidationError, match="drift"):
            store.save_pr(rec)

    def test_close_dup_requires_canonical(self, store):
        rec = _pr(analysis={"disposition": "close-dup", "checked_at": "x", "against_head_sha": "abc123"})
        with pytest.raises(ValidationError, match="canonical"):
            store.save_pr(rec)

    def test_upstream_pr_must_be_integer(self, store):
        rec = _pr(analysis={"disposition": "close-fixed", "upstream_pr": "6008",
                            "checked_at": "x", "against_head_sha": "abc123"})
        with pytest.raises(ValidationError, match="upstream_pr"):
            store.save_pr(rec)

    def test_greptile_review_valid_severity_accepted(self, store):
        rec = _pr(greptile_review={"severity": "defects", "findings": [], "summary": "x"})
        store.save_pr(rec)

    def test_greptile_review_bad_severity_rejected(self, store):
        rec = _pr(greptile_review={"severity": "meh", "findings": [], "summary": "x"})
        with pytest.raises(ValidationError, match="greptile_review.severity"):
            store.save_pr(rec)

    def test_greptile_review_findings_must_be_list_of_dicts(self, store):
        rec = _pr(greptile_review={"severity": "nits", "findings": ["oops"], "summary": "x"})
        with pytest.raises(ValidationError, match="greptile_review.findings"):
            store.save_pr(rec)

    def test_greptile_review_finding_class_must_be_known(self, store):
        rec = _pr(greptile_review={
            "severity": "defects",
            "findings": [{"headline": "h", "class": "vibes", "why": "w"}],
            "summary": "x",
        })
        with pytest.raises(ValidationError, match="greptile_review.findings\\[\\].class"):
            store.save_pr(rec)


class TestForwardCompatibility:
    """A record written by newer code (carrying a section this checkout doesn't
    know) must round-trip through load -> mutate -> save without loss or crash."""

    def test_unknown_section_round_trips(self, store):
        rec = _pr(1)
        rec["novel_section"] = {"payload": [1, 2], "checked_at": "2026-07-09T00:00:00+00:00"}
        store.save_pr(rec)
        pr = store.edit_pr(1)
        pr.raw["meta"]["title"] = "edited"
        store.save_pr(pr.raw)
        again = store.load_pr(1)
        assert again.raw["novel_section"] == {
            "payload": [1, 2], "checked_at": "2026-07-09T00:00:00+00:00"}
        assert again.raw["meta"]["title"] == "edited"

    def test_unknown_section_warns_once_per_name(self, store, capsys):
        from pipeline import storekit
        storekit._WARNED_UNKNOWN_SECTIONS.clear()
        store.save_pr({**_pr(1), "novel_section": {}})
        store.save_pr({**_pr(2), "novel_section": {}})
        err = capsys.readouterr().err
        assert err.count("novel_section") == 1
        assert "behind the store schema" in err

    def test_malformed_known_section_still_raises(self, store):
        rec = _pr(1, threat={"verdict": "bogus", "checked_at": "x", "against_head_sha": "abc123"})
        with pytest.raises(ValidationError, match="threat.verdict"):
            store.save_pr(rec)


class TestUpdateSection:
    def test_stamps_checked_at_and_sha(self, store):
        store.save_pr(_pr())
        rec = set_section(store, 1234, "analysis", {"disposition": "merge", "rationale": "best of cluster"})
        assert rec["analysis"]["against_head_sha"] == "abc123"
        assert rec["analysis"]["checked_at"]  # stamped
        assert store.load_pr(1234).section("analysis")["disposition"] == "merge"

    def test_explicit_sha_override(self, store):
        store.save_pr(_pr())
        rec = set_section(store, 1234, "security", {"verdict": "GREEN", "findings": []},
                          against_head_sha="older999")
        assert rec["security"]["against_head_sha"] == "older999"

    def test_update_unknown_pr_raises(self, store):
        with pytest.raises(KeyError):
            set_section(store, 404, "analysis", {"disposition": "merge"})


class TestClusters:
    def test_round_trip(self, store):
        store.save_cluster({"id": 12, "root_problem": "lock fields not cleared",
                            "prs": [1, 2, 3], "outcome": "merge-ready",
                            "checked_at": "2026-06-10T00:00:00+00:00"})
        assert store.load_cluster(12).prs == [1, 2, 3]
        assert set(store.all_clusters()) == {12}

    def test_rejects_unknown_outcome(self, store):
        with pytest.raises(ValidationError, match="outcome"):
            store.save_cluster({"id": 1, "root_problem": "x", "prs": [], "outcome": "synthesize",
                                "checked_at": "x"})

    def test_delete_cluster(self, store):
        store.save_cluster({"id": 7, "root_problem": "x", "prs": [1, 2], "checked_at": "t"})
        assert store.load_cluster(7) is not None
        store.delete_cluster(7)
        assert store.load_cluster(7) is None
        assert 7 not in store.all_clusters()

    def test_delete_missing_cluster_is_noop(self, store):
        store.delete_cluster(999)  # must not raise

    def test_delete_surfaces_in_clusters_since(self, store):
        store.save_cluster({"id": 7, "root_problem": "x", "prs": [1, 2], "checked_at": "t"})
        live, deleted, _ = store.clusters_since(None)
        assert set(live) == {7} and deleted == []   # a live cluster, not a deletion
        store.delete_cluster(7)
        live, deleted, hi = store.clusters_since(None)
        assert 7 not in live                         # dropped from the live set
        assert deleted == [7]                         # surfaced to the watermark reader
        assert hi is not None                         # watermark covers the tombstone

    def test_reap_spares_fresh_tombstones_removes_aged(self, store):
        store.save_cluster({"id": 7, "root_problem": "x", "prs": [1, 2], "checked_at": "t"})
        store.delete_cluster(7)
        assert store.reap_cluster_tombstones(older_than_seconds=3600) == 0  # fresh: spared
        _, deleted, _ = store.clusters_since(None)
        assert deleted == [7]                         # still a tombstone
        assert store.reap_cluster_tombstones(older_than_seconds=-1) == 1    # aged out: reaped
        live, deleted, _ = store.clusters_since(None)
        assert 7 not in live and deleted == []        # the row is gone entirely

    def test_rejects_non_bool_deleted(self, store):
        with pytest.raises(ValidationError, match="deleted"):
            store.save_cluster({"id": 1, "root_problem": "x", "prs": [], "deleted": "yes",
                                "checked_at": "t"})


def _pr_cluster(**cluster):
    rec = {"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "h1"}}
    if cluster:
        rec["cluster"] = cluster
    return rec


class TestClusterBackref:
    def test_validate_pr_accepts_ids_list(self, store):
        store.save_pr(_pr_cluster(ids=[7, 9], checked_at="t", against_head_sha="h1"))

    def test_validate_pr_accepts_empty_ids(self, store):
        store.save_pr(_pr_cluster(ids=[], checked_at="t", against_head_sha="h1"))

    def test_validate_pr_rejects_old_id_key(self, store):
        with pytest.raises(ValidationError):
            store.save_pr(_pr_cluster(id=7, checked_at="t", against_head_sha="h1"))

    def test_validate_pr_rejects_non_int_ids(self, store):
        with pytest.raises(ValidationError):
            store.save_pr(_pr_cluster(ids=["7"], checked_at="t", against_head_sha="h1"))

    def test_validate_cluster_accepts_proposals(self, store):
        store.save_cluster({
            "id": 7, "root_problem": "rp", "prs": [1], "outcome": None,
            "proposals": [{"pr": 1, "disposition": "merge", "rationale": "x"}]})

    def test_validate_cluster_rejects_proposal_without_pr(self, store):
        with pytest.raises(ValidationError):
            store.save_cluster({
                "id": 7, "root_problem": "rp", "prs": [1],
                "proposals": [{"disposition": "merge"}]})


class TestRunsLedger:
    def test_append_and_read(self, store):
        store.append_run({"phase": "ingest", "started": "t0", "finished": "t1", "stats": {"prs": 10}})
        store.append_run({"phase": "cluster", "started": "t2", "finished": "t3", "stats": {}})
        runs = store.runs()
        assert [r.phase for r in runs] == ["ingest", "cluster"]
        assert runs[0].started == "t0" and runs[0].finished == "t1"
        assert runs[0].raw["stats"] == {"prs": 10}

    def test_ledger_ordering(self, store):
        store.append_run({"phase": "ingest", "started": "t0", "finished": "t1", "stats": {}})
        store.append_run({"phase": "cluster", "started": "t2", "finished": "t3", "stats": {}})
        runs = store.runs()
        assert runs[0].phase == "ingest"
        assert runs[1].phase == "cluster"

    def test_limit_returns_last_n_in_insertion_order(self, store):
        for i in range(5):
            store.append_run({"phase": f"phase{i}", "stats": {}})
        limited = store.runs(limit=3)
        assert [r.phase for r in limited] == ["phase2", "phase3", "phase4"]
        assert [r.phase for r in store.runs()] == [f"phase{i}" for i in range(5)]

    def test_store_edit_entry_reads_typed(self, store):
        store.append_run({"action": "store-edit", "transform": "t", "table": "prs",
                          "examined": 3, "changed": [1, 2], "deleted": [],
                          "backup": "/b", "ts": "t0"})
        (rec,) = store.runs()
        assert isinstance(rec, storekit.StoreEdit)
        assert rec.transform == "t" and rec.table == "prs"
        assert rec.changed == [1, 2] and rec.deleted == []
        assert rec.examined == 3 and rec.backup == "/b" and rec.ts == "t0"

    def test_append_rejects_unparseable_record(self, store):
        with pytest.raises(ValueError):
            store.append_run({"foo": 1})
        assert store.runs() == []

    def test_append_rejects_malformed_store_edit(self, store):
        with pytest.raises(ValueError):
            store.append_run({"action": "store-edit", "transform": "t",
                              "table": "prs", "examined": 1, "changed": "1,2",
                              "deleted": [], "backup": "/b", "ts": "t0"})
        assert store.runs() == []


class TestLiveSweep:
    def test_default_is_never_swept(self, store):
        assert store.load_live_sweep() == {"swept_at": None}

    def test_roundtrip(self, store):
        store.save_live_sweep({"swept_at": "2026-07-07T12:00:00+00:00"})
        assert store.load_live_sweep()["swept_at"] == "2026-07-07T12:00:00+00:00"


class TestBatch:
    def test_batch_writes_and_reads_are_correct(self, store):
        with store.batch():
            store.save_pr(_pr(1))
            store.save_pr(_pr(2))
            assert store.load_pr(1) is not None  # read sees a write in the same batch
        # durable after the batch closes
        assert store.load_pr(1).head_sha is not None
        assert set(store.all_prs()) == {1, 2}

    def test_batch_nested_is_safe(self, store):
        with store.batch():
            with store.batch():
                store.save_pr(_pr(1))
            store.save_pr(_pr(2))
        assert set(store.all_prs()) == {1, 2}


class TestAuthorBaseline:
    def test_default_empty(self, store):
        assert store.load_author_baseline() == {"authors": {}, "materialized_at": None}

    def test_round_trip(self, store):
        reg = {"authors": {"al": {"handle": "al", "merged": 5}}, "materialized_at": "2026-07-10T00:00:00+00:00"}
        store.save_author_baseline(reg)
        assert store.load_author_baseline() == reg

    def test_rejects_non_dict_authors(self, store):
        with pytest.raises(ValidationError):
            store.save_author_baseline({"authors": [], "materialized_at": None})


class TestVerifyValidation:
    def _rec(self, verify=None, disposition=None) -> dict:
        rec = {"pr": 1,
               "meta": {"title": "t", "state": "open", "head_sha": "h1"}}
        if verify is not None:
            rec["verify"] = verify
        if disposition is not None:
            rec["analysis"] = {"disposition": disposition, "rationale": "r",
                               "against_head_sha": "h1"}
        return rec

    def test_unknown_outcome_rejected(self, tmp_path):
        s = Store(tmp_path)
        with pytest.raises(ValidationError):
            s.save_pr(self._rec(verify={"outcome": "totally-fine",
                                        "against_head_sha": "h1"}))

    def test_null_outcome_accepted(self, tmp_path):
        s = Store(tmp_path)
        s.save_pr(self._rec(verify={"outcome": None, "against_head_sha": "h1",
                                    "against_base_sha": "b"}))
        assert s.load_pr(1).verify_outcome is None

    def test_base_sha_required_when_section_present(self, tmp_path):
        s = Store(tmp_path)
        with pytest.raises(ValidationError):
            s.save_pr(self._rec(verify={"outcome": "verified-fix",
                                        "against_head_sha": "h1"}))

    def test_base_sha_required_even_with_null_outcome(self, tmp_path):
        s = Store(tmp_path)
        with pytest.raises(ValidationError):
            s.save_pr(self._rec(verify={"outcome": None, "against_head_sha": "h1"}))

    def test_merge_coexists_with_a_blocking_verify(self, tmp_path):
        # Stored merge + a blocking outcome saves fine; the route derives on read.
        s = Store(tmp_path)
        s.save_pr(self._rec(
            verify={"outcome": "escalate", "against_head_sha": "h1",
                    "against_base_sha": "b"},
            disposition="merge"))
        assert s.load_pr(1).disposition == "needs-human"

    def test_blind_repro_rejected_accepts_a_string(self, tmp_path):
        s = Store(tmp_path)
        s.save_pr(self._rec(verify={
            "outcome": None, "against_head_sha": "h1", "against_base_sha": "b",
            "signals": {"blind_adequacy": {
                "repro_command": None,
                "repro_rejected": "repro_command targets a test the PR itself "
                                  "introduces ('src/x.test.ts')"}}}))
        blind = s.load_pr(1).verify_signals["blind_adequacy"]
        assert "src/x.test.ts" in blind["repro_rejected"]

    def test_blind_repro_rejected_rejects_a_non_string(self, tmp_path):
        s = Store(tmp_path)
        with pytest.raises(ValidationError):
            s.save_pr(self._rec(verify={
                "outcome": None, "against_head_sha": "h1", "against_base_sha": "b",
                "signals": {"blind_adequacy": {"repro_rejected": 7}}}))

    def test_merge_coexists_with_verified_fix(self, tmp_path):
        s = Store(tmp_path)
        s.save_pr(self._rec(
            verify={"outcome": "verified-fix", "against_head_sha": "h1",
                    "against_base_sha": "b"},
            disposition="merge"))
        assert s.load_pr(1).disposition == "merge"


class TestVerifyBaseRegistry:
    """The pin names the base image `run` looks for: its SHA and the tier it was
    built at, plus the captured baseline that names the regress exclusion set.
    base_sha, tier, baseline_failing, and baseline_captured_at are all
    required, so a pin can never disagree with itself or omit the baseline."""

    def test_default_is_unpinned(self, tmp_path):
        assert Store(tmp_path).load_verify_base() == {"base_sha": None, "tier": None,
                                                      "pinned_at": None,
                                                      "baseline_failing": None,
                                                      "baseline_captured_at": None}

    def test_roundtrip(self, tmp_path):
        s = Store(tmp_path)
        s.save_verify_base({"base_sha": "deadbeef", "tier": 1,
                            "pinned_at": "2026-07-14T00:00:00+00:00",
                            "baseline_failing": [], "baseline_captured_at": "2026-07-15"})
        assert s.load_verify_base()["base_sha"] == "deadbeef"
        assert s.load_verify_base()["tier"] == 1

    def test_tier_0_roundtrips(self, tmp_path):
        s = Store(tmp_path)
        s.save_verify_base({"base_sha": "deadbeef", "tier": 0, "pinned_at": None,
                            "baseline_failing": [], "baseline_captured_at": "2026-07-15"})
        assert s.load_verify_base()["tier"] == 0

    def test_blank_base_sha_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Store(tmp_path).save_verify_base({"base_sha": "", "tier": 0, "pinned_at": None,
                                              "baseline_failing": [], "baseline_captured_at": "2026-07-15"})

    def test_missing_tier_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Store(tmp_path).save_verify_base({"base_sha": "deadbeef", "pinned_at": None})

    def test_unknown_tier_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            Store(tmp_path).save_verify_base({"base_sha": "deadbeef", "tier": 7,
                                              "pinned_at": None,
                                              "baseline_failing": [], "baseline_captured_at": "2026-07-15"})

    def test_verify_base_requires_a_captured_baseline(self, tmp_path):
        with pytest.raises(ValidationError, match="baseline_failing"):
            Store(tmp_path).save_verify_base(
                {"base_sha": "deadbeef", "tier": 0, "pinned_at": None,
                 "baseline_captured_at": "2026-07-15"})

    def test_verify_base_baseline_must_be_a_list_of_paths(self, tmp_path):
        with pytest.raises(ValidationError, match="baseline_failing"):
            Store(tmp_path).save_verify_base(
                {"base_sha": "deadbeef", "tier": 0, "pinned_at": None,
                 "baseline_failing": "x.test.ts",
                 "baseline_captured_at": "2026-07-15"})

    def test_verify_base_requires_the_baseline_timestamp(self, tmp_path):
        with pytest.raises(ValidationError, match="baseline_captured_at"):
            Store(tmp_path).save_verify_base(
                {"base_sha": "deadbeef", "tier": 0, "pinned_at": None,
                 "baseline_failing": []})

    def test_verify_base_an_all_green_baseline_is_a_valid_pin(self, tmp_path):
        s = Store(tmp_path)
        s.save_verify_base({"base_sha": "deadbeef", "tier": 0, "pinned_at": None,
                            "baseline_failing": [], "baseline_captured_at": "2026-07-15"})
        assert s.load_verify_base()["baseline_failing"] == []

    def test_verify_base_default_has_no_baseline(self, tmp_path):
        assert Store(tmp_path).load_verify_base()["baseline_failing"] is None


class TestResponseAcks:
    """Which PRs' response signals operators have marked seen — shared, so one
    operator's ack clears the signal from every cockpit."""

    def test_defaults_to_empty(self, store):
        assert store.load_response_acks() == {"acks": {}}

    def test_round_trips(self, store):
        store.save_response_acks({"acks": {"537": {"at": "2026-07-15T18:22:00+00:00",
                                                   "by": "Taylor Example"}}})
        assert store.load_response_acks()["acks"]["537"]["by"] == "Taylor Example"

    def test_rejects_non_dict_acks(self, store):
        with pytest.raises(ValidationError):
            store.save_response_acks({"acks": []})
