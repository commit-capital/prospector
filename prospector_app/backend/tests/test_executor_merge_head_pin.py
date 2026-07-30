"""merge_pr pins the upstream merge to the exact head it verified
(`--match-head-commit`), so a force-push in the window between the pre-flight
head read and the merge can't land unverified code — GitHub refuses the merge
server-side when the live head no longer matches. And when the live PR state
can't be confirmed at all (GitHub unreachable), the merge fails CLOSED — unlike
the comment/close path, an irreversible merge must not proceed on a blind guess.
"""
from types import SimpleNamespace

from prospector_app.backend import data
from prospector_app.backend import executor
from pipeline import gates

_OPEN = {"state": "open", "merged": False, "head": "abc123", "mergeable_state": "clean"}


def _setup(monkeypatch, merges: list, *, live: dict | None):
    rec = SimpleNamespace(head_sha="abc123", linked_issues=[])
    monkeypatch.setattr(data, "prs", lambda: {5: rec})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    monkeypatch.setattr(executor, "_changed_paths", lambda n: [])
    monkeypatch.setattr(gates, "merge_eligibility", lambda rec, **k: (True, "passed all checks run"))
    monkeypatch.setattr(gates, "security_overridable", lambda rec, **k: False)
    monkeypatch.setattr(executor, "_pr_live", lambda n: live)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)
    monkeypatch.setattr(executor, "_credit_merge_closed_issues", lambda *a, **k: None)

    def fake_merge(cmd, token):
        merges.append(cmd)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(executor, "bot_merge_run", fake_merge)


def test_live_merge_pins_the_verified_head(monkeypatch):
    merges: list = []
    _setup(monkeypatch, merges, live=_OPEN)
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged"
    assert len(merges) == 1
    argv = merges[0]
    assert "--match-head-commit" in argv
    assert argv[argv.index("--match-head-commit") + 1] == "abc123"


def test_merge_blocks_when_live_state_unconfirmed(monkeypatch):
    merges: list = []
    _setup(monkeypatch, merges, live=None)  # GitHub unreachable → can't confirm head
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "blocked"
    assert "confirm" in res["detail"].lower()
    assert merges == []  # the merge was never attempted


def test_open_pr_with_matching_head_still_merges(monkeypatch):
    # the fail-closed guard must not block the normal path
    merges: list = []
    _setup(monkeypatch, merges, live=_OPEN)
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged"
