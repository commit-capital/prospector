"""The autofix worker's push discipline: what it parks, what it pushes, and what
it re-proves first.

The property under test throughout is that the idle hunter can run without
touching a contributor's branch. Every mechanical action parks with its evidence
unless TRIAGE_FIX_AUTOPUSH names it, and an approved push re-derives the change
against current base rather than pushing a result proven against a base that has
since moved.
"""
from __future__ import annotations

import pytest

from pipeline import profile, settings
from pipeline import store as S
from prospector_app.backend import data, fix_queue, fix_worker

HEAD = "a" * 40
NOW = "2026-06-10T00:00:00+00:00"


@pytest.fixture
def store(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 1,
                "meta": {"title": "fix boom", "state": "open", "head_sha": HEAD},
                "signals": {"greptile": 5, "ci": "failing", "mergeable": False,
                            "checked_at": NOW, "against_head_sha": HEAD},
                "drift": {"state": "conflicts", "checked_at": NOW,
                          "against_head_sha": HEAD}})
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(settings, "FIX_AUTOPUSH", frozenset())
    monkeypatch.setattr(fix_worker, "_preflight", lambda n, patch: {"exit": 0})
    data.refresh()
    return st


class _Probe:
    """Replays a scripted resubmit run, recording every subcommand invoked."""

    def __init__(self, rc: int = 0, stdout: str = "diff --git a/a.ts b/a.ts\n+x",
                 overrides: dict | None = None):
        self.rc, self.stdout, self.calls = rc, stdout, []
        self.overrides = overrides or {}

    def __call__(self, n, *args):
        self.calls.append(args)
        rc, out = self.overrides.get(args[0], (self.rc, self.stdout))
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": "boom"})()


def _pushed(probe: _Probe) -> bool:
    """Whether anything the worker ran could have reached the contributor's
    branch: an explicit push, or a bare `update`, which merges AND pushes."""
    return any(a[0] == "push" or a == ("update",) for a in probe.calls)


# --- the hunt bar ---------------------------------------------------------------

def test_hunter_requires_the_review_bar(store):
    rec = store.load_pr(1).raw
    rec["signals"]["greptile"] = 4
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.next_auto() is None


def test_hunter_takes_an_unmergeable_pr_that_meets_the_bar(store):
    # CI is failing and the PR does not merge — the two things pr_clean refuses
    # on, and the two things an update exists to clear.
    assert fix_worker.next_auto() == ("rebase", 1)


# --- update parks instead of pushing --------------------------------------------

def test_update_parks_for_review_and_pushes_nothing(store, monkeypatch):
    fix_queue.queue_pr(1, "update")
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "awaiting-review"
    assert not _pushed(probe), "an unattended update must not reach the branch"
    assert ("update", "--probe") in probe.calls


def test_update_pushes_when_autopush_names_it(store, monkeypatch):
    monkeypatch.setattr(settings, "FIX_AUTOPUSH", frozenset({"update"}))
    fix_queue.queue_pr(1, "update")
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["status"] == "pushed"
    assert _pushed(probe)


def test_a_conflicted_probe_refuses_rather_than_parking(store, monkeypatch):
    # Exit 8 is "the base conflicts". There is nothing for an operator to approve,
    # so it must not land in the reviewable pile.
    fix_queue.queue_pr(1, "update")
    probe = _Probe(rc=8)
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["status"] == "refused"
    assert not _pushed(probe)


def test_a_parked_request_records_the_base_it_was_proven_against(store, monkeypatch):
    fix_queue.queue_pr(1, "update")
    monkeypatch.setattr(fix_worker, "_resubmit", _Probe())
    monkeypatch.setattr(fix_worker, "_base_sha", lambda: "b" * 40)

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["base_sha"] == "b" * 40


def test_a_parked_mechanical_request_keeps_no_worktree(store, monkeypatch):
    # The approve path re-derives the change, so holding the tree would only
    # accumulate clones on the sandbox machine for a backlog of dozens.
    fix_queue.queue_pr(1, "rebase")
    probe = _Probe(overrides={"state": (0, '{"phase": "ready", "conflicts": []}')})
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["status"] == "awaiting-review"
    assert any(a[0] == "abort" for a in probe.calls), "the prepared tree is discarded"


# --- approving re-proves before it pushes ---------------------------------------

def _parked(store, action: str) -> None:
    store.load_pr(1).record_fix_request(
        "approved", action, queued_at=NOW, result={"message": "m"}, head_sha=HEAD)
    data.refresh()


def test_approving_an_update_re_merges_against_current_base(store, monkeypatch):
    # A week-old "resolvable" verdict says nothing about today's main. The push
    # runs the merge again rather than trusting the stored result.
    _parked(store, "update")
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.push_approved(1)

    assert store.load_pr(1).fix_request["status"] == "pushed"
    assert ("update",) in probe.calls, "the merge is re-run, not replayed from cache"


def test_approving_refuses_when_the_base_now_conflicts(store, monkeypatch):
    _parked(store, "update")
    probe = _Probe(rc=8)
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.push_approved(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert "conflicts" in (req.get("refused_reason") or "").lower()


def test_approving_a_fix_pushes_the_reviewed_patch_verbatim(store, monkeypatch):
    # An agent-authored change is not reproducible: re-deriving it at approve
    # time would push something the operator never saw.
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("ci",))))
    _parked(store, "fix")
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.push_approved(1)

    assert store.load_pr(1).fix_request["status"] == "pushed"
    assert not any(a[0] in ("prepare", "update") for a in probe.calls)
