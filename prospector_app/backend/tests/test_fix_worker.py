"""The autofix worker's push discipline: what it parks, what it pushes, and what
it re-proves first.

The property under test throughout is that the idle hunter can run without
touching a contributor's branch. Every mechanical action parks with its evidence
unless TRIAGE_FIX_AUTOPUSH names it, and an approved push re-derives the change
against current base rather than pushing a result proven against a base that has
since moved.
"""
from __future__ import annotations

import json

import pytest

from pipeline import profile
from pipeline import store as S
from prospector_app.backend import data, fix_queue, fix_worker

HEAD = "a" * 40
NOW = "2026-06-10T00:00:00+00:00"
PR_DIFF = "diff --git a/pr.ts b/pr.ts\n+pr\n"


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
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH", "")
    monkeypatch.setattr(fix_worker, "_preflight", lambda n, patch: {"exit": 0})
    # The fix path reads the PR's diff from GitHub for the agent and the sandbox,
    # and the review provider's summary when the score is below the bar.
    pr_patch = tmp_path / "pr.patch"
    pr_patch.write_text(PR_DIFF)
    monkeypatch.setattr(fix_worker.verify_driver, "fetch_patch", lambda pr, head: pr_patch)
    monkeypatch.setattr(fix_worker.greptile, "fetch_greptile_summary_text",
                        lambda n: (None, None))
    data.refresh()
    return st


class _Probe:
    """Replays a scripted resubmit run, recording every subcommand invoked."""

    def __init__(self, rc: int = 0, stdout: str = "diff --git a/a.ts b/a.ts\n+x",
                 overrides: dict | None = None):
        self.rc, self.stdout, self.calls = rc, stdout, []
        self.overrides = overrides or {}
        self.applied: list[str] = []

    def __call__(self, n, *args, stdin: str | None = None):
        self.calls.append(args)
        if args[0] == "apply" and stdin is not None:
            self.applied.append(stdin)
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
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH", "update")
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


# --- a mechanical run reports where it is ---------------------------------------

class _StepReader(_Probe):
    """A probe that also records the stored fix_request step at each call, so a
    test can assert the step was written BEFORE the slow command ran."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.steps: dict[tuple, str | None] = {}

    def __call__(self, n, *args, stdin: str | None = None):
        self.steps[args] = (data.store().load_pr(1).fix_request or {}).get("step")
        return super().__call__(n, *args, stdin=stdin)


def test_an_update_reports_its_steps_while_it_runs(store, monkeypatch):
    # The merge probe and the compile preflight are the minutes-long halves of a
    # mechanical update; each announces itself so the queue row never sits on
    # "claimed" while the worker is busy.
    fix_queue.queue_pr(1, "update")
    probe = _StepReader()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    seen_at_preflight: list[str | None] = []
    monkeypatch.setattr(
        fix_worker, "_preflight",
        lambda n, patch: (seen_at_preflight.append(
            (data.store().load_pr(1).fix_request or {}).get("step")) or {"exit": 0}))

    fix_worker.run_one(1)

    assert probe.steps[("update", "--probe")] == "merging base in"
    assert seen_at_preflight == ["compile preflight"]
    assert store.load_pr(1).fix_request["status"] == "awaiting-review"


def test_a_rebase_reports_its_steps_while_it_runs(store, monkeypatch):
    fix_queue.queue_pr(1, "rebase")
    probe = _StepReader(overrides={"state": (0, '{"phase": "ready", "conflicts": []}')})
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.run_one(1)

    assert probe.steps[("prepare", "--rebase")] == "rebasing onto base"
    assert store.load_pr(1).fix_request["status"] == "awaiting-review"


def test_a_step_write_keeps_the_requests_action(store, monkeypatch):
    # The step writer replaces the whole fix_request section, so the action has
    # to be carried through it — a row must never flip to another action's name
    # mid-run.
    fix_queue.queue_pr(1, "update")
    monkeypatch.setattr(fix_worker, "_resubmit", _Probe())

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["action"] == "update"


# --- approving re-proves before it pushes ---------------------------------------

PATCH = "diff --git a/a.ts b/a.ts\n@@ -1 +1 @@\n-old\n+new\n"


def _parked(store, action: str, **result) -> None:
    store.load_pr(1).record_fix_request(
        "approved", action, queued_at=NOW, result={"message": "m", **result},
        head_sha=HEAD)
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


def test_the_reviewable_pile_is_listed_before_work_still_in_flight(store):
    # The proven-and-waiting set is the whole point of the tab: it is what an
    # operator acts on, so it must not be buried under running work.
    store.load_pr(1).record_fix_request("queued", "update", queued_at=NOW)
    store.save_pr({"pr": 2, "meta": {"title": "second", "state": "open",
                                     "head_sha": "b" * 40}})
    store.load_pr(2).record_fix_request(
        "awaiting-review", "rebase", queued_at=NOW, base_sha="c" * 40,
        result={"compile_preflight": {"exit": 0}})
    data.refresh()

    rows = fix_queue.queue_entries()

    assert [r["pr"] for r in rows] == [2, 1]
    assert rows[0]["status"] == "awaiting-review"
    assert rows[0]["action"] == "rebase"
    assert rows[0]["base_sha"] == "c" * 40
    assert rows[0]["resolvable"] is True


def test_a_parked_request_whose_preflight_failed_is_not_resolvable(store):
    store.load_pr(1).record_fix_request(
        "awaiting-review", "update", queued_at=NOW,
        result={"compile_preflight": {"exit": 1}})
    data.refresh()
    assert fix_queue.queue_entries()[0]["resolvable"] is False


def test_a_request_with_no_configured_preflight_is_still_resolvable(store):
    # A deployment with no verify.compile_cmd records None. The merge itself
    # resolving is the claim being made; the build check is corroboration.
    store.load_pr(1).record_fix_request(
        "awaiting-review", "update", queued_at=NOW,
        result={"compile_preflight": None})
    data.refresh()
    assert fix_queue.queue_entries()[0]["resolvable"] is True


def test_settled_requests_are_not_listed(store):
    store.load_pr(1).record_fix_request("pushed", "update", queued_at=NOW)
    data.refresh()
    assert fix_queue.queue_entries() == []


def test_the_queue_route_serves_the_reviewable_pile(store):
    from fastapi.testclient import TestClient

    from prospector_app.backend import app as appmod

    store.load_pr(1).record_fix_request(
        "awaiting-review", "update", queued_at=NOW, base_sha="c" * 40,
        result={"compile_preflight": {"exit": 0}})
    data.refresh()

    body = TestClient(appmod.app).get("/api/fix/queue").json()

    assert [r["pr"] for r in body["queue"]] == [1]
    assert body["queue"][0]["resolvable"] is True
    assert "runner" in body


def test_approving_a_fix_re_applies_the_reviewed_patch_verbatim(store, monkeypatch):
    # The bytes a person approved are the bytes that land: the push clones the
    # head and applies the stored patch, and never runs the agent again.
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("ci",))))
    _parked(store, "fix", patch=PATCH)
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.push_approved(1)

    assert store.load_pr(1).fix_request["status"] == "pushed"
    assert probe.applied == [PATCH]
    assert ("prepare",) in probe.calls


def test_any_machine_can_push_an_approved_fix(store, monkeypatch):
    # The patch is rebuilt from the store, so the machine that authored it is
    # not the only one that can push it.
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("ci",))))
    _parked(store, "fix", patch=PATCH)
    rec = store.load_pr(1)
    req = dict(rec.fix_request or {})
    rec.record_fix_request("approved", "fix", queued_at=NOW, result=req["result"],
                           host="some-other-machine", head_sha=HEAD)
    data.refresh()
    monkeypatch.setattr(fix_worker, "_resubmit", _Probe())

    assert fix_worker.next_approved() == 1
    fix_worker.push_approved(1)
    assert store.load_pr(1).fix_request["status"] == "pushed"


def test_a_resolve_is_left_to_the_machine_holding_its_merge(store, monkeypatch):
    # The merge commit being approved lives in that machine's worktree; no other
    # can rebuild it, so no other may claim the push.
    _parked(store, "resolve")
    rec = store.load_pr(1)
    rec.record_fix_request("approved", "resolve", queued_at=NOW,
                           result={"message": "m"}, host="some-other-machine",
                           head_sha=HEAD)
    data.refresh()

    assert fix_worker.next_approved() is None


def test_an_approved_fix_without_its_patch_refuses(store, monkeypatch):
    # Nothing to re-apply means nothing to push — never a silent empty commit.
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("ci",))))
    _parked(store, "fix")
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.push_approved(1)

    assert store.load_pr(1).fix_request["status"] == "refused"
    assert not _pushed(probe)


# --- a conflicted rebase escalates to an agent-authored merge resolution --------

class _ConflictedResubmit:
    """A resubmit whose rebase pauses on conflicts and whose merge prepare
    pauses on the same paths; continue/diff then succeed."""

    def __init__(self, tmp_path):
        self.calls = []
        self.wt = str(tmp_path / "wt")
        self.merged = False

    def __call__(self, n, *args):
        self.calls.append(args)
        rc, out = 0, ""
        if args[0] == "state":
            out = json.dumps({
                "phase": "ready" if self.merged else "conflicted",
                "mode": "merge" if self.merged else "rebase",
                "conflicts": [] if self.merged else ["one.txt"],
                "worktree": self.wt, "base_branch": "master"})
        elif args[0] == "diff":
            out = ("diff --git a/one.txt b/one.txt\n+resolved" if self.merged
                   else "diff --git a/one.txt b/one.txt\n+<<<<<<< conflict")
        elif args[0] == "continue":
            self.merged = True
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()


def test_conflicted_rebase_escalates_to_agent_and_parks_resolve(store, monkeypatch, tmp_path):
    fix_queue.queue_pr(1, "rebase")
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda wt, paths, **kw: {"resolutions": [
                            {"path": "one.txt", "rationale": "kept both"}]})

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "awaiting-review"
    assert req["action"] == "resolve"
    assert req["result"]["resolutions"][0]["rationale"] == "kept both"
    assert req["result"]["conflict_paths"] == ["one.txt"]
    assert req["result"]["merge_diff"].startswith("diff ")
    assert "resolved" in req["result"]["patch"]
    assert ("continue",) in fake.calls
    assert not _pushed(fake)
    # the parked worktree is kept: no abort after the merge was prepared
    after_merge = fake.calls[fake.calls.index(("prepare", "--merge")):]
    assert ("abort",) not in after_merge


def test_auto_queued_conflicted_rebase_keeps_the_refusal(store, monkeypatch, tmp_path):
    fix_queue.queue_pr(1, "rebase", source="auto")
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    called = []
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda *a, **kw: called.append(1))

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert not called
    assert ("prepare", "--merge") not in fake.calls


def test_agent_give_up_refuses_with_reason(store, monkeypatch, tmp_path):
    fix_queue.queue_pr(1, "rebase")
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda wt, paths, **kw: {"give_up": "the sides contradict"})

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert "the sides contradict" in req["refused_reason"]
    after_merge = fake.calls[fake.calls.index(("prepare", "--merge")):]
    assert ("abort",) in after_merge


def test_resolve_preflight_failure_refuses_and_aborts(store, monkeypatch, tmp_path):
    fix_queue.queue_pr(1, "rebase")
    fake = _ConflictedResubmit(tmp_path)
    monkeypatch.setattr(fix_worker, "_resubmit", fake)
    monkeypatch.setattr(fix_worker.resolve_conflicts, "resolve",
                        lambda wt, paths, **kw: {"resolutions": [
                            {"path": "one.txt", "rationale": "kept both"}]})
    monkeypatch.setattr(fix_worker, "_preflight",
                        lambda n, patch: {"exit": 1, "error_excerpt": "boom"})

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    after_merge = fake.calls[fake.calls.index(("prepare", "--merge")):]
    assert ("abort",) in after_merge


def test_approved_resolve_pushes_the_kept_tree_without_rederiving(store, monkeypatch):
    store.load_pr(1).record_fix_request(
        "approved", "resolve", queued_at=NOW,
        result={"patch": "diff", "conflict_paths": ["one.txt"]}, head_sha=HEAD)
    data.refresh()
    probe = _Probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)

    fix_worker.push_approved(1)

    assert probe.calls == [("push",)]
    assert store.load_pr(1).fix_request["status"] == "pushed"


# --- an agent-authored fix ------------------------------------------------------

def _queue_fix(guidance: str | None = "bound the retry loop") -> None:
    fix_queue.queue_pr(1, "fix", guidance=guidance)


def _fix_probe() -> _Probe:
    """A resubmit whose `prepare` leaves a readable worktree — what the fix path
    reads the agent's edits out of."""
    return _Probe(overrides={"state": (0, json.dumps({"phase": "prepared",
                                                      "mode": "edit",
                                                      "worktree": "/wt"}))})


def _authored(monkeypatch, verdict: dict | None = None, capture: dict | None = None):
    """Stub the authoring agent, recording the kwargs it was driven with."""
    def fake_author(worktree, **kw):
        if capture is not None:
            capture.update(kw, worktree=worktree)
        return verdict or {"summary": "Bound the retry loop",
                           "changes": [{"path": "a.ts", "rationale": "added a cap"}]}
    monkeypatch.setattr(fix_worker.author_fix, "author", fake_author)


def _reviewed(monkeypatch, verdict: dict | None = None, capture: dict | None = None):
    def fake_review(worktree, patch, **kw):
        if capture is not None:
            capture.update(kw, worktree=worktree, patch=patch)
        return verdict or {"verdict": "safe", "reason": "local and bounded",
                           "concerns": []}
    monkeypatch.setattr(fix_worker.review_fix, "review", fake_review)


def test_an_authored_fix_parks_with_its_rationale_and_pushes_nothing(store, monkeypatch):
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "awaiting-review"
    assert req["action"] == "fix"
    assert req["result"]["changes"][0]["rationale"] == "added a cap"
    assert req["result"]["review_verdict"]["verdict"] == "safe"
    assert req["result"]["message"] == "Bound the retry loop"
    assert not _pushed(probe)


def test_an_authored_fix_parks_its_patch_and_drops_its_worktree(store, monkeypatch):
    # The reviewed patch is the artifact, so the clone is released and the
    # approval is not tied to this machine.
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "awaiting-review"
    assert req["result"]["patch"].startswith("diff ")
    assert ("abort",) in probe.calls


def test_the_operators_guidance_becomes_the_agents_goal(store, monkeypatch):
    _queue_fix("drop the retry loop, keep the timeout")
    monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
    seen: dict = {}
    _authored(monkeypatch, capture=seen)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    assert "drop the retry loop, keep the timeout" in seen["goal"]


def test_an_unguided_fix_aims_at_the_outstanding_findings(store, monkeypatch):
    rec = store.load_pr(1).raw
    rec["greptile_review"] = {"severity": "defects", "against_head_sha": HEAD,
                              "checked_at": NOW,
                              "findings": [{"headline": "retry never exits",
                                            "class": "substantive", "why": "still there"}]}
    store.save_pr(rec)
    data.refresh()
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",))))
    fix_queue.queue_pr(1, "fix")
    monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
    seen: dict = {}
    _authored(monkeypatch, capture=seen)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    assert seen["findings"][0]["headline"] == "retry never exits"


def _sub_bar_review(store, findings: list[dict] | None = None) -> None:
    """A PR Greptile scored 3/5 at the current head, with the given digest."""
    rec = store.load_pr(1).raw
    rec["signals"].update({"greptile": 3, "greptile_reviewed_sha": HEAD})
    rec["greptile_review"] = {"severity": "defects" if findings else "clean",
                              "against_head_sha": HEAD, "checked_at": NOW,
                              "findings": findings or []}
    store.save_pr(rec)
    data.refresh()


def test_the_review_summary_is_evidence_when_the_digest_has_no_findings(store, monkeypatch):
    # Greptile's reasons for a sub-bar score live in its summary comment, which
    # names defects even when it left no inline comment for the digest to carry.
    _sub_bar_review(store)
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",))))
    monkeypatch.setattr(fix_worker.greptile, "fetch_greptile_summary_text",
                        lambda n: ("The armSilenceTimer guard misses terminalResultSeen.",
                                   HEAD[:8]))
    fix_queue.queue_pr(1, "fix")
    monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
    seen: dict = {}
    _authored(monkeypatch, capture=seen)
    reviewed: dict = {}
    _reviewed(monkeypatch, capture=reviewed)

    fix_worker.run_one(1)

    assert "misses terminalResultSeen" in seen["review_summary"]
    assert "review summary" in seen["goal"]
    assert "misses terminalResultSeen" in reviewed["review_summary"]


def test_a_summary_for_another_head_is_not_evidence(store, monkeypatch):
    _sub_bar_review(store)
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",))))
    monkeypatch.setattr(fix_worker.greptile, "fetch_greptile_summary_text",
                        lambda n: ("stale reasoning", "b" * 8))
    fix_queue.queue_pr(1, "fix")
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused" and "Nothing to aim" in req["refused_reason"]
    assert ("prepare",) not in probe.calls


def test_a_fix_with_nothing_to_aim_at_refuses_before_any_agent_runs(store, monkeypatch):
    _sub_bar_review(store)
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",))))
    fix_queue.queue_pr(1, "fix")
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch, verdict=None,
              capture=None)
    monkeypatch.setattr(fix_worker.author_fix, "author",
                        lambda *a, **k: pytest.fail("agent ran with no evidence"))

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused" and "Nothing to aim" in req["refused_reason"]
    assert probe.calls == []


def test_the_agent_gets_the_pr_diff_and_the_sandbox_sees_pr_plus_edits(store, monkeypatch,
                                                                       tmp_path):
    _queue_fix()
    monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
    seen: dict = {}
    _authored(monkeypatch, capture=seen)
    _reviewed(monkeypatch)
    preflighted: list[str] = []
    monkeypatch.setattr(fix_worker, "_preflight",
                        lambda n, patch: (preflighted.append(patch), {"exit": 0})[1])

    fix_worker.run_one(1)

    assert seen["diff_path"].endswith("pr.patch") and seen["head_sha"] == HEAD
    # The agent's edits were written against the PR's tree, so the sandbox
    # applies the PR's diff first and the edits after it.
    (patch,) = preflighted
    assert patch.startswith(PR_DIFF)
    assert patch.endswith("diff --git a/a.ts b/a.ts\n+x")


def test_a_pr_diff_that_cannot_be_fetched_fails_the_request_before_the_agent(store,
                                                                             monkeypatch):
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    monkeypatch.setattr(fix_worker.verify_driver, "fetch_patch",
                        lambda pr, head: (_ for _ in ()).throw(
                            fix_worker.verify_driver.FetchFailure("gh down")))
    monkeypatch.setattr(fix_worker.author_fix, "author",
                        lambda *a, **k: pytest.fail("agent ran without the PR diff"))

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "failed" and "gh down" in req["error"]
    assert probe.calls == []


def test_a_give_up_refuses_with_the_agents_reason(store, monkeypatch):
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch, {"give_up": "the finding needs a product call"})
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert "product call" in req["refused_reason"]
    assert not _pushed(probe)
    assert ("abort",) in probe.calls


def test_a_rejected_change_refuses_and_keeps_the_patch_visible(store, monkeypatch):
    # The operator should be able to see what the reviewer turned down.
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch, {"verdict": "unsafe", "reason": "breaks parse() callers",
                            "concerns": ["parse() now throws"]})

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert "breaks parse() callers" in req["refused_reason"]
    assert req["result"]["patch"].startswith("diff ")
    assert req["result"]["review_verdict"]["concerns"] == ["parse() now throws"]
    assert not _pushed(probe)


def test_an_undisclosed_edit_refuses(store, monkeypatch):
    # The patch touches a.ts; the agent only admitted to b.ts.
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch, {"summary": "s",
                            "changes": [{"path": "b.ts", "rationale": "r"}]})
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert not _pushed(probe)


def test_the_authored_patch_is_regated_on_the_paths_it_really_touched(store, monkeypatch):
    # The queue-time gate judged the PR's existing paths. What the agent wrote is
    # judged too, or an agent could author its way onto a withheld path.
    _queue_fix()
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(deny_globs=("*.ts",))))
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert "withholds from autofix" in req["refused_reason"]
    assert not _pushed(probe)


def test_the_reviewer_never_sees_a_patch_the_gate_already_refused(store, monkeypatch):
    # Ordering matters for cost and for reach: a patch on a withheld path is not
    # a judgment call, so it never reaches the reviewing agent.
    _queue_fix()
    monkeypatch.setattr(
        profile, "active",
        lambda: profile.RepoProfile(autofix=profile.AutofixPolicy(deny_globs=("*.ts",))))
    monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
    _authored(monkeypatch)
    seen: dict = {}
    _reviewed(monkeypatch, capture=seen)

    fix_worker.run_one(1)

    assert store.load_pr(1).fix_request["status"] == "refused"
    assert seen == {}


def test_guidance_survives_the_claim_and_the_approval(store, monkeypatch):
    # Guidance is what authorizes a fix where the profile names no fixable
    # gates. Every transition rewrites the whole section, so if it is dropped
    # anywhere the operator's own approved change refuses at the push.
    _queue_fix("drop the retry loop")
    monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
    _authored(monkeypatch)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)
    assert store.load_pr(1).fix_request["guidance"] == "drop the retry loop"

    fix_queue.approve_pr(1)
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    fix_worker.push_approved(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "pushed", req.get("refused_reason")


# --- terminal records keep their head stamp -------------------------------------

def test_terminal_records_carry_the_head_they_ran_against(store, monkeypatch):
    # The one-attempt-per-head guard reads the stamp off refused/failed/pushed
    # records; a terminal record that dropped it would re-arm the PR instantly.
    fix_queue.queue_pr(1, "update")
    probe = _Probe(rc=8)  # base conflicts → refused
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    fix_worker.run_one(1)
    req = store.load_pr(1).fix_request
    assert req["status"] == "refused"
    assert req["against_head_sha"] == HEAD


def test_refuse_records_the_claimed_head_not_the_current_one(store):
    # The stamp is the head the run was pinned against, which is what the
    # one-attempt guard compares — a head that moved mid-run must not be
    # recorded as attempted.
    fix_worker._refuse(1, {"action": "fix", "against_head_sha": "b" * 40}, "nope")
    req = store.load_pr(1).fix_request
    assert req["against_head_sha"] == "b" * 40


# --- hunting agent-authored fixes -----------------------------------------------

def _fixable_pr(n: int = 2) -> dict:
    """A CI-passing, mergeable PR scored below the review bar at its current
    head — what the fix hunt targets."""
    return {"pr": n,
            "meta": {"title": "below bar", "state": "open", "head_sha": HEAD},
            "signals": {"greptile": 4, "greptile_reviewed_sha": HEAD,
                        "ci": "passing", "mergeable": True,
                        "checked_at": NOW, "against_head_sha": HEAD}}


@pytest.fixture
def fix_profile(monkeypatch):
    p = profile.RepoProfile(autofix=profile.AutofixPolicy(fixable_gates=("review",)))
    monkeypatch.setattr(profile, "active", lambda: p)


def test_hunter_ignores_fix_without_the_opt_in(store, fix_profile, monkeypatch):
    monkeypatch.delenv("TRIAGE_FIX_HUNT_FIX", raising=False)
    store.save_pr(_fixable_pr())
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) is None


def test_hunter_queues_fix_with_the_opt_in(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    store.save_pr(_fixable_pr())
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) == "fix"


def test_one_fix_attempt_per_head(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    rec = _fixable_pr()
    rec["fix_request"] = {"status": "refused", "action": "fix",
                          "against_head_sha": HEAD}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) is None
    # A moved head re-arms the PR.
    rec["fix_request"] = {"status": "refused", "action": "fix",
                          "against_head_sha": "b" * 40}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[2]) == "fix"


def test_fix_hunt_respects_the_in_flight_cap(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    monkeypatch.setenv("TRIAGE_FIX_HUNT_LIMIT", "1")
    running = _fixable_pr(3)
    running["fix_request"] = {"status": "running", "action": "fix", "source": "auto"}
    store.save_pr(running)
    store.save_pr(_fixable_pr(2))
    # Drop PR 1 below the mechanical hunt's review bar so only fixes compete.
    one = store.load_pr(1).raw
    one["signals"]["greptile"] = 4
    store.save_pr(one)
    data.refresh()
    assert fix_worker.next_auto() is None


def test_hunt_order_mechanical_then_nits_then_tiers(store, fix_profile, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_HUNT_FIX", "1")
    # PR numbers deliberately run against the priority order, so a first-match
    # scan by number would pick every one of these wrong.
    three = _fixable_pr(2)
    three["signals"]["greptile"] = 3
    four = _fixable_pr(3)
    nits = _fixable_pr(4)
    nits["greptile_review"] = {"severity": "nits", "checked_at": NOW,
                               "against_head_sha": HEAD}
    for rec in (three, four, nits):
        store.save_pr(rec)
    data.refresh()
    # PR 1 (unmergeable, bar met) is mechanical and wins outright.
    assert fix_worker.next_auto() == ("rebase", 1)
    one = store.load_pr(1).raw
    one["fix_request"] = {"status": "running", "action": "rebase"}
    store.save_pr(one)
    data.refresh()
    assert fix_worker.next_auto() == ("fix", 4)  # nits beat score tiers
    nits2 = store.load_pr(4).raw
    nits2["fix_request"] = {"status": "running", "action": "fix"}
    store.save_pr(nits2)
    data.refresh()
    assert fix_worker.next_auto() == ("fix", 3)  # 4/5 beats 3/5


def test_authored_fix_pushes_when_autopush_names_fix(store, monkeypatch):
    monkeypatch.setenv("TRIAGE_FIX_AUTOPUSH", "fix")
    monkeypatch.setattr(fix_worker, "_retrigger_review", lambda n: None)
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "pushed"
    assert _pushed(probe)


# --- a pushed fix asks for a fresh review ---------------------------------------

def test_pushed_fix_retriggers_the_review(store, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(fix_worker, "_retrigger_review", calls.append)
    fix_worker._finish_pushed(1, {"action": "fix"}, "pushed ok")
    assert calls == [1]


def test_pushed_update_does_not_retrigger(store, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(fix_worker, "_retrigger_review", calls.append)
    fix_worker._finish_pushed(1, {"action": "update"}, "pushed ok")
    assert calls == []


def test_retrigger_failure_leaves_the_pushed_record(store, monkeypatch):
    def boom(n: int) -> None:
        raise RuntimeError("no network")
    monkeypatch.setattr(fix_worker, "_retrigger_review", boom)
    fix_worker._finish_pushed(1, {"action": "fix"}, "pushed ok")
    assert store.load_pr(1).fix_request["status"] == "pushed"


def test_a_failed_ending_is_hunted_again_once_it_cools(store, monkeypatch):
    # A failure is the machine's, not a verdict on the code: the PR rests only
    # for the cooldown, then a recovered worker picks it back up unprompted.
    from datetime import datetime, timedelta, timezone
    rec = store.load_pr(1).raw
    just_now = datetime.now(timezone.utc).isoformat()
    rec["fix_request"] = {"status": "failed", "action": "rebase", "source": "auto",
                          "against_head_sha": HEAD, "finished_at": just_now,
                          "error": "the autofix worker restarted mid-run"}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[1]) is None
    long_ago = (datetime.now(timezone.utc) - timedelta(
        seconds=fix_worker.FAILED_RETRY_COOLDOWN_SECONDS + 60)).isoformat()
    rec["fix_request"]["finished_at"] = long_ago
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[1]) == "rebase"


def test_an_agent_that_does_not_land_is_a_failure_not_a_verdict(store, monkeypatch):
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    monkeypatch.setattr(fix_worker.author_fix, "author",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("claude did not exit within 1800s")))
    _reviewed(monkeypatch)

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "failed" and "1800s" in req["error"]
    assert ("abort",) in probe.calls and not _pushed(probe)


def test_a_reviewer_without_a_verdict_fails_the_request_and_keeps_the_patch(store,
                                                                            monkeypatch):
    _queue_fix()
    probe = _fix_probe()
    monkeypatch.setattr(fix_worker, "_resubmit", probe)
    _authored(monkeypatch)
    _reviewed(monkeypatch, {"verdict": "unsafe", "concerns": [], "failed": True,
                            "reason": "the reviewing agent did not finish: timeout"})

    fix_worker.run_one(1)

    req = store.load_pr(1).fix_request
    assert req["status"] == "failed" and "did not finish" in req["error"]
    assert req["result"]["patch"].startswith("diff --git")
    assert not _pushed(probe)


def test_a_broken_sandbox_fails_while_a_failed_compile_refuses(store, monkeypatch):
    # Same gate outcome — nothing is pushed — but a sandbox that could not run
    # is the machine's problem and the hunter may retry it; a compile that ran
    # and failed is a verdict on the change.
    for pf, status in (({"cmd": "pnpm -r typecheck", "error": "docker: daemon down"},
                        "failed"),
                       ({"cmd": "pnpm -r typecheck", "exit": 20, "error_excerpt": "TS2345"},
                        "refused")):
        _queue_fix()
        monkeypatch.setattr(fix_worker, "_resubmit", _fix_probe())
        _authored(monkeypatch)
        _reviewed(monkeypatch)
        monkeypatch.setattr(fix_worker, "_preflight", lambda n, patch, pf=pf: pf)

        fix_worker.run_one(1)

        req = store.load_pr(1).fix_request
        assert req["status"] == status, pf
        assert req["result"]["compile_preflight"] == pf


def test_a_refused_rebase_is_not_hunted_again_at_the_same_head(store, monkeypatch):
    # Without this the hunter ping-pongs on the lowest-numbered conflicted PRs,
    # re-attempting the same refused rebase every sweep.
    rec = store.load_pr(1).raw
    rec["fix_request"] = {"status": "refused", "action": "rebase",
                          "against_head_sha": HEAD, "source": "auto"}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[1]) is None
    # A moved head re-arms the PR.
    rec["fix_request"] = {"status": "refused", "action": "rebase",
                          "against_head_sha": "b" * 40, "source": "auto"}
    store.save_pr(rec)
    data.refresh()
    assert fix_worker.auto_fixable(data.prs()[1]) == "rebase"
