"""Path-keyed JSONL records of an authoring agent's sandbox runs."""
from __future__ import annotations

import json

from pipeline import check_records


def _rec(kind: str = "test") -> check_records.CheckRecord:
    return {"kind": kind, "files": ["a.test.ts"], "cmd": "npx vitest run a.test.ts",
            "exit": 20, "error_kind": None, "error": None, "error_excerpt": "boom",
            "duration_s": 1.5, "at": "2026-09-17T00:00:00+00:00"}


def test_append_then_collect_round_trips_oldest_first_and_consumes(tmp_path):
    p = tmp_path / "deep" / "issue-7.checks.jsonl"
    check_records.append(p, _rec("typecheck"))
    check_records.append(p, _rec("test"))
    got = check_records.collect(p, 20)
    assert [r["kind"] for r in got] == ["typecheck", "test"]
    assert not p.exists()
    assert check_records.collect(p, 20) == []


def test_collect_skips_lines_that_are_not_json_objects_and_caps(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text("not json\n[1]\n" + "".join(json.dumps(_rec()) + "\n" for _ in range(5)))
    assert len(check_records.collect(p, 3)) == 3


def test_append_to_an_unwritable_path_reports_and_does_not_raise(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    check_records.append(blocker / "child.jsonl", _rec())
    assert capsys.readouterr().err.startswith("sandbox-check: the run could not be recorded")


def test_the_calling_tool_names_itself_in_the_report(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    check_records.append(blocker / "child.jsonl", _rec(), tool="issue-check")
    assert capsys.readouterr().err.startswith("issue-check: the run could not be recorded")
