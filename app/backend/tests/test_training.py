"""Decision capture — the labelled human-decision corpus, stored centrally."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine

from pipeline import schema
from app.backend import training


@pytest.fixture
def eng(monkeypatch, tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/store.db")
    schema.METADATA.create_all(e)
    monkeypatch.setattr(training, "_TEST_ENGINE", e)
    monkeypatch.setattr(training, "_features", lambda pr: {"title": f"PR {pr}"})
    return e


class TestCapture:
    def test_capture_writes_a_row_readable_by_stats(self, eng):
        training.capture(42, "CLOSE_DUP", reason="dupe of #7", dry_run=False)
        assert training.stats() == {"count": 1, "with_reason": 1,
                                    "decisions": {"CLOSE_DUP": 1}}

    def test_capture_returns_the_record_with_features(self, eng):
        rec = training.capture(42, "MERGE")
        assert rec["pr"] == 42
        assert rec["decision"] == "MERGE"
        assert rec["features"] == {"title": "PR 42"}

    def test_reason_is_optional(self, eng):
        training.capture(1, "MERGE")
        assert training.stats()["with_reason"] == 0

    def test_stats_counts_each_decision(self, eng):
        training.capture(1, "MERGE")
        training.capture(2, "MERGE")
        training.capture(3, "CLOSE_STALE", reason="no reply in 6mo")
        s = training.stats()
        assert s["count"] == 3
        assert s["decisions"] == {"MERGE": 2, "CLOSE_STALE": 1}
        assert s["with_reason"] == 1

    def test_stats_is_empty_on_a_fresh_store(self, eng):
        assert training.stats() == {"count": 0, "with_reason": 0, "decisions": {}}


class TestImportLocalLog:
    """One-time backfill of the pre-store decision log. Idempotent, and it never
    modifies the source file — that file is the only copy until this succeeds."""

    def _log(self, tmp_path, *recs):
        p = tmp_path / "decisions.jsonl"
        p.write_text("".join(json.dumps(r) + "\n" for r in recs))
        return p

    def test_imports_every_row(self, eng, tmp_path):
        src = self._log(tmp_path,
                        {"at": "2026-01-01T00:00:00", "pr": 1, "decision": "MERGE"},
                        {"at": "2026-01-02T00:00:00", "pr": 2, "decision": "CLOSE_DUP",
                         "reason_private": "dupe"})
        out = training.import_local_log(src)
        assert out == {"read": 2, "imported": 2, "skipped": 0}
        assert training.stats() == {"count": 2, "with_reason": 1,
                                    "decisions": {"MERGE": 1, "CLOSE_DUP": 1}}

    def test_is_idempotent(self, eng, tmp_path):
        src = self._log(tmp_path, {"at": "2026-01-01T00:00:00", "pr": 1, "decision": "MERGE"})
        training.import_local_log(src)
        out = training.import_local_log(src)
        assert out == {"read": 1, "imported": 0, "skipped": 1}
        assert training.stats()["count"] == 1

    def test_a_preview_and_its_live_action_both_import(self, eng, tmp_path):
        # The real corpus holds exactly this: PR 7067 REOPEN twice at
        # 2026-06-16T13:24:08, dry_run True then False. `at` is
        # second-resolution, so dry_run is the only field telling the preview
        # apart from the live action, and both belong in the dataset.
        src = self._log(tmp_path,
                        {"at": "2026-06-16T13:24:08", "pr": 7067, "decision": "REOPEN",
                         "dry_run": True},
                        {"at": "2026-06-16T13:24:08", "pr": 7067, "decision": "REOPEN",
                         "dry_run": False})
        assert training.import_local_log(src) == {"read": 2, "imported": 2, "skipped": 0}
        assert training.stats()["count"] == 2
        # …and re-running still imports nothing.
        assert training.import_local_log(src) == {"read": 2, "imported": 0, "skipped": 2}
        assert training.stats()["count"] == 2

    def test_leaves_the_source_file_untouched(self, eng, tmp_path):
        src = self._log(tmp_path, {"at": "2026-01-01T00:00:00", "pr": 1, "decision": "MERGE"})
        before = src.read_text()
        training.import_local_log(src)
        assert src.read_text() == before

    def test_skips_blank_and_corrupt_lines(self, eng, tmp_path):
        src = tmp_path / "decisions.jsonl"
        src.write_text('{"at": "2026-01-01T00:00:00", "pr": 1, "decision": "MERGE"}\n'
                       "\n"
                       "{not json}\n")
        assert training.import_local_log(src) == {"read": 1, "imported": 1, "skipped": 0}

    def test_missing_file_is_a_no_op(self, eng, tmp_path):
        assert training.import_local_log(tmp_path / "nope.jsonl") == {
            "read": 0, "imported": 0, "skipped": 0}

    def test_skips_a_duplicate_line_within_one_file(self, eng, tmp_path):
        rec = {"at": "2026-01-01T00:00:00", "pr": 1, "decision": "MERGE"}
        src = self._log(tmp_path, rec, rec)
        assert training.import_local_log(src) == {"read": 2, "imported": 1, "skipped": 1}
        assert training.stats()["count"] == 1

    def test_skips_a_valid_but_non_dict_json_line(self, eng, tmp_path):
        src = tmp_path / "decisions.jsonl"
        src.write_text('null\n'
                       '{"at": "2026-01-01T00:00:00", "pr": 1, "decision": "MERGE"}\n')
        out = training.import_local_log(src)
        assert out == {"read": 1, "imported": 1, "skipped": 0}
        assert training.stats()["count"] == 1
