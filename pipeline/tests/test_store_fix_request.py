"""fix_request carries an objection and the objection source."""
from pipeline import profile, settings
from pipeline.store import Store


def _store(tmp_path):
    st = Store(tmp_path / "db")
    st.save_pr({"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "a" * 40}})
    return st


def test_an_objection_round_trips_with_its_source(tmp_path):
    st = _store(tmp_path)
    obj = {"kind": "resolve-review", "signature": "resolve-review:behavior",
           "text": "the merge drops the base's deletion", "from": {"lens": "behavior"}}
    st.edit_pr(1).record_fix_request("queued", "fix", source="objection", objection=obj,
                                     head_sha="a" * 40)
    req = st.load_pr(1).fix_request
    assert req["source"] == "objection" and req["objection"] == obj


def test_the_objection_gate_is_a_known_fixable_gate():
    assert "objection" in profile.AUTOFIX_GATES


def test_budget_and_flags_read_the_environment(monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_OBJECTION_BUDGET", raising=False)
    assert settings.fix_objection_budget() == 20
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "5")
    assert settings.fix_objection_budget() == 5
    monkeypatch.setenv("TRIAGE_FIX_OBJECTION_BUDGET", "zero")
    assert settings.fix_objection_budget() == 20
    monkeypatch.setenv("TRIAGE_FIX_HUNT_SECURITY", "1")
    assert settings.fix_hunt_security() is True
    monkeypatch.delenv("TRIAGE_FIX_AUTOPUSH_MIN_TIER", raising=False)
    assert settings.fix_autopush_min_tier() == 2
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH_MAX_LINES", "120")
    assert settings.fix_autopush_max_lines() == 120
