"""store_edit: the sanctioned bulk-edit path — dry-run planning, mandatory
pre-image snapshot, runs-ledger entry on live apply (#401)."""
import json

import pytest

from pipeline import store_edit
from pipeline import storekit
from pipeline.store import Store


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "db")
    for n in (1, 2, 3):
        st.save_pr(_pr(n))
    return st


def _pr(n):
    return {
        "pr": n,
        "meta": {
            "title": f"fix {n}", "state": "open", "head_sha": f"sha{n}",
            "body": f"body {n}",
            "checked_at": "2026-07-09T00:00:00+00:00",
        },
    }


def _retitle(rec):
    rec["meta"]["title"] = rec["meta"]["title"].upper()
    return rec


def _delete_pr2(rec):
    return store_edit.DELETE if rec["pr"] == 2 else None


def _noop_returning_same(rec):
    return rec


class TestPlan:
    def test_plan_collects_changes_without_applying(self, store):
        report = store_edit.plan_edit(store, _retitle, "prs")
        assert report.examined == 3
        assert set(report.changed) == {1, 2, 3}
        assert report.sections == {"meta": 3}
        assert store.load_pr(1).raw["meta"]["title"] == "fix 1"  # untouched

    def test_plan_reads_full_records_body_intact(self, store):
        report = store_edit.plan_edit(store, _retitle, "prs")
        assert report.pre_images[1]["meta"]["body"] == "body 1"
        assert report.changed[1]["meta"]["body"] == "body 1"

    def test_unchanged_and_none_results_are_skipped(self, store):
        assert store_edit.plan_edit(store, _noop_returning_same, "prs").changed == {}
        assert store_edit.plan_edit(store, lambda rec: None, "prs").changed == {}

    def test_delete_sentinel_collected(self, store):
        report = store_edit.plan_edit(store, _delete_pr2, "prs")
        assert report.deleted == [2]
        assert report.changed == {}

    def test_bad_transform_output_fails_in_plan(self, store):
        def corrupt(rec):
            rec["meta"]["state"] = "weird"
            return rec
        with pytest.raises(store_edit.ValidationError):
            store_edit.plan_edit(store, corrupt, "prs")


class TestApply:
    def test_apply_snapshots_applies_and_logs(self, store, tmp_path):
        report = store_edit.plan_edit(store, _retitle, "prs")
        report = store_edit.apply_edit(store, report, "_retitle", backup_dir=tmp_path / "bk")
        assert report.applied
        assert store.load_pr(1).raw["meta"]["title"] == "FIX 1"
        backup = json.loads(report.backup.read_text())
        assert backup["transform"] == "_retitle"
        assert [r["pr"] for r in backup["records"]] == [1, 2, 3]
        assert backup["records"][0]["meta"]["title"] == "fix 1"  # pre-image
        runs = store.runs()
        assert isinstance(runs[-1], storekit.StoreEdit)
        assert runs[-1].backup == str(report.backup)

    def test_apply_deletes(self, store, tmp_path):
        report = store_edit.plan_edit(store, _delete_pr2, "prs")
        store_edit.apply_edit(store, report, "_delete_pr2", backup_dir=tmp_path / "bk")
        assert store.load_pr(2) is None
        assert store.load_pr(1) is not None

    def test_apply_with_no_changes_writes_nothing(self, store, tmp_path):
        report = store_edit.plan_edit(store, lambda rec: None, "prs")
        report = store_edit.apply_edit(store, report, "noop", backup_dir=tmp_path / "bk")
        assert not report.applied
        assert report.backup is None
        assert not (tmp_path / "bk").exists()

    def test_stale_schema_refuses_apply_before_snapshot(self, store, tmp_path):
        from pipeline import schema, storekit
        report = store_edit.plan_edit(store, _retitle, "prs")
        storekit._stamp_schema_version(store.engine, schema.STORE_SCHEMA_VERSION + 1)
        storekit.refresh_schema_guard(store.engine)
        with pytest.raises(storekit.StaleSchemaError):
            store_edit.apply_edit(store, report, "_retitle", backup_dir=tmp_path / "bk")
        assert store.load_pr(1).raw["meta"]["title"] == "fix 1"
        assert not (tmp_path / "bk").exists()  # refused before the snapshot


class TestCli:
    def test_dry_run_is_default(self, store, tmp_path, capsys):
        store_edit.main([f"{__name__}:_retitle", "--store", str(tmp_path / "db"),
                         "--backup-dir", str(tmp_path / "bk")])
        assert store.load_pr(1).raw["meta"]["title"] == "fix 1"
        out = capsys.readouterr().out
        assert "dry run" in out
        assert not (tmp_path / "bk").exists()

    def test_live_requires_confirmation(self, store, tmp_path, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "wrong")
        with pytest.raises(SystemExit):
            store_edit.main([f"{__name__}:_retitle", "--live",
                             "--store", str(tmp_path / "db"),
                             "--backup-dir", str(tmp_path / "bk")])
        assert store.load_pr(1).raw["meta"]["title"] == "fix 1"

    def test_live_with_yes_applies(self, store, tmp_path):
        store_edit.main([f"{__name__}:_retitle", "--live", "--yes",
                         "--store", str(tmp_path / "db"),
                         "--backup-dir", str(tmp_path / "bk")])
        assert store.load_pr(1).raw["meta"]["title"] == "FIX 1"
        assert list((tmp_path / "bk").glob("store-edit-_retitle-*.json"))
