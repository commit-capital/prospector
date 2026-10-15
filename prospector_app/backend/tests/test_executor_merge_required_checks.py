from types import SimpleNamespace

import pytest

from pipeline import gates, live_prs
from prospector_app.backend import data, executor, service

REAL_LIVE_CI_BLOCK = executor._live_ci_block


@pytest.fixture
def merge(monkeypatch):
    """Dry-run merge of PR 5 at head "h" with the live CI facts a test gives."""
    recorded: list[dict] = []
    edit = SimpleNamespace(record_live_state=lambda **kw: recorded.append(kw))
    store = SimpleNamespace(load_pr=lambda n: object(), edit_pr=lambda n: edit)

    def run(facts: dict | None):
        rec = SimpleNamespace(head_sha="h", linked_issues=[])
        monkeypatch.setattr(data, "prs", lambda: {5: rec})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
        monkeypatch.setattr(data, "store", lambda: store)
        monkeypatch.setattr(data, "refresh", lambda: None)
        monkeypatch.setattr(service, "live_changed_paths", lambda n: [])
        monkeypatch.setattr(gates, "merge_eligibility", lambda rec, **k: (True, "ok"))
        monkeypatch.setattr(executor, "_live_ci_block", REAL_LIVE_CI_BLOCK)
        monkeypatch.setattr(executor, "_pr_live", lambda n: {
            "state": "open", "merged": False, "head": "h", "mergeable_state": "blocked"})
        monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
        monkeypatch.setattr(live_prs, "fetch", lambda prs: ({5: facts} if facts else {}, set()))
        return executor.merge_pr(5, dry_run=True), recorded
    return run


def test_required_check_that_never_ran_blocks_and_is_recorded(merge):
    res, recorded = merge({"head": "h", "ci": "unreported", "unreported": ["ci / e2e"]})
    assert res["status"] == "blocked"
    assert "ci / e2e" in res["detail"]
    assert recorded == [{"ci": "unreported"}]


def test_live_failing_check_blocks(merge):
    res, _ = merge({"head": "h", "ci": "failing", "unreported": []})
    assert res["status"] == "blocked"


def test_passing_does_not_block(merge):
    res, recorded = merge({"head": "h", "ci": "passing", "unreported": []})
    assert res["status"] == "dry-run"
    assert recorded == []


@pytest.mark.parametrize("facts", [
    {"head": "other", "ci": "unreported", "unreported": ["ci / e2e"]},
    None,
])
def test_unconfirmed_head_blocks(merge, facts):
    res, recorded = merge(facts)
    assert res["status"] == "blocked"
    assert "retry" in res["detail"]
    assert recorded == []


@pytest.mark.parametrize("ci", ["unknown", None])
def test_unread_ci_blocks_and_replaces_stored_passing(merge, ci):
    res, recorded = merge({"head": "h", "ci": ci, "unreported": []})
    assert res["status"] == "blocked"
    assert "retry" in res["detail"]
    assert recorded == [{"ci": "unknown"}]
