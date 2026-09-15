"""objections: the shape, its signature, once-per-head, and the daily budget."""
from datetime import datetime, timezone

from pipeline import objections
from pipeline.model import Pr
from pipeline.store import Store

HEAD = "a" * 40


def test_build_signs_by_kind_and_flattened_text():
    a = objections.build("compile", "error TS2345 in file 12 at line 33")
    b = objections.build("compile", "error TS9999 in file 99 at line 1")
    assert a["signature"] == b["signature"]
    assert a["signature"].startswith("compile:")
    assert objections.build("security", "x")["signature"] != a["signature"]


def test_spent_when_the_head_already_carried_this_objection():
    obj = objections.build("resolve-review", "drops the base's deletion")
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": HEAD},
                   "fix_request": {"status": "refused", "action": "fix", "objection": obj,
                                   "against_head_sha": HEAD}})
    assert objections.spent(pr, obj["signature"])
    moved = Pr(None, {"pr": 1, "meta": {"head_sha": "b" * 40},
                      "fix_request": {"status": "refused", "action": "fix", "objection": obj,
                                      "against_head_sha": HEAD}})
    assert not objections.spent(moved, obj["signature"])
    queued = Pr(None, {"pr": 1, "meta": {"head_sha": HEAD},
                       "fix_request": {"status": "queued", "action": "fix", "objection": obj,
                                       "against_head_sha": HEAD}})
    assert not objections.spent(queued, obj["signature"])


def test_spent_when_a_round_already_answered_it():
    obj = objections.build("resolve-review", "r")
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": HEAD},
                   "fix_request": {"status": "awaiting-review", "action": "resolve",
                                   "against_head_sha": HEAD,
                                   "result": {"rounds": [{"objection": obj}]}}})
    assert objections.spent(pr, obj["signature"])


def test_budget_counts_todays_objection_endings_for_this_worker(tmp_path, monkeypatch):
    st = Store(tmp_path / "db")
    now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    today = [("w1", now.replace(hour=11)), ("w1", now.replace(hour=10)), ("w2", now.replace(hour=11))]
    for host, ts in today:
        st.append_run({"phase": "fix:single", "pr": 1, "started": None,
                       "finished": ts.isoformat(), "ts": ts.isoformat(),
                       "stats": {"host": host, "status": "pushed", "action": "fix",
                                 "objection": "compile:abc"}})
    st.append_run({"phase": "fix:single", "pr": 1, "started": None,
                   "finished": "2026-09-14T16:00:00+00:00", "ts": "2026-09-14T16:00:00+00:00",
                   "stats": {"host": "w1", "status": "pushed", "action": "fix",
                             "objection": "compile:abc"}})
    st.append_run({"phase": "fix:single", "pr": 2, "started": None,
                   "finished": now.isoformat(), "ts": now.isoformat(),
                   "stats": {"host": "w1", "status": "refused", "action": "fix"}})
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "3")
    assert objections.used_today(st, "w1", now) == 2
    assert objections.budget_left(st, "w1", now) == 1


def test_goal_text_names_the_kind():
    assert "compile check failed" in objections.goal_text(objections.build("compile", "boom"))
    assert "security review flagged" in objections.goal_text(objections.build("security", "s"))
