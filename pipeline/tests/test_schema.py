from pipeline import schema
from sqlalchemy import create_engine, inspect


def test_create_all_makes_tables(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/s.db")
    schema.METADATA.create_all(eng)
    names = set(inspect(eng).get_table_names())
    assert {"prs", "clusters", "runs", "registries", "training_decisions"} <= names


def test_mirror_pr_extracts_hot_columns():
    rec = {
        "pr": 7,
        "meta": {"state": "open", "head_sha": "abc", "updated_at": "2026-06-01T00:00:00+00:00"},
        "analysis": {"disposition": "merge"},
        "security": {"verdict": "GREEN"},
    }
    assert schema.mirror_pr(rec) == {
        "pr": 7, "disposition": "merge", "state": "open",
        "security_verdict": "GREEN", "head_sha": "abc",
        "updated_at": "2026-06-01T00:00:00+00:00",
    }


def test_mirror_pr_handles_missing_sections():
    rec = {"pr": 9, "meta": {"state": "open", "head_sha": "z", "updated_at": "t"}}
    m = schema.mirror_pr(rec)
    assert m["disposition"] is None and m["security_verdict"] is None


def test_activity_chat_tables_created(tmp_path):
    from sqlalchemy import create_engine, inspect
    eng = create_engine(f"sqlite:///{tmp_path}/s.db")
    schema.METADATA.create_all(eng)
    names = set(inspect(eng).get_table_names())
    assert {"activity", "chat_messages"} <= names


def test_activity_row_extracts_hot_columns():
    ev = {"at": "2026-06-28T00:00:00+00:00", "kind": "merge", "operator": "Casey",
          "pr": 42, "dry_run": False, "action": "MERGE"}
    row = schema.activity_row(ev)
    assert row == {"at": "2026-06-28T00:00:00+00:00", "kind": "merge",
                   "operator": "Casey", "pr": 42, "dry_run": False, "data": ev}


def test_activity_row_coerces_pr_and_dry_run():
    assert schema.activity_row({"pr": "42"})["pr"] == 42
    assert schema.activity_row({})["pr"] is None
    assert schema.activity_row({})["dry_run"] is False
    assert schema.activity_row({"dry_run": True})["dry_run"] is True


def test_training_decision_row_projects_hot_columns():
    rec = {"at": "2026-07-15T10:00:00", "pr": 42, "decision": "CLOSE_DUP",
           "by": "Ada", "dry_run": False, "reason_private": "dupe of #7"}
    row = schema.training_decision_row(rec)
    assert row["at"] == "2026-07-15T10:00:00"
    assert row["pr"] == 42
    assert row["decision"] == "CLOSE_DUP"
    assert row["by"] == "Ada"
    assert row["dry_run"] is False
    assert row["data"] is rec          # full record preserved


def test_training_decision_row_coerces_string_pr_and_missing_dry_run():
    row = schema.training_decision_row({"pr": "42", "decision": "MERGE"})
    assert row["pr"] == 42
    assert row["dry_run"] is False


def test_training_decision_row_non_numeric_pr_becomes_none():
    assert schema.training_decision_row({"pr": None, "decision": "MERGE"})["pr"] is None
