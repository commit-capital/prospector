"""merge_pr against a real store, with only GitHub, the sandbox, the bot token
and the merge gate's policy faked.

The invariants: a live merge is pinned to the head it verified and fails
closed when that head cannot be confirmed; the gate, the threat rescan, the
live pre-flight and the compile preflight all run before any durable override
write or upstream merge; every outcome, refusals included, lands in the
activity log.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import compile_preflight, diff_cache, gates, live_prs, progress, threats
from pipeline import store as S
from pipeline.testsupport import threat_section
from prospector_app.backend import caps, data, executor, merge_progress, service
from prospector_app.backend import issues as issues_mod

REAL_RESCAN = executor._threat_rescan
REAL_LIVE_CI_BLOCK = executor._live_ci_block
HEAD = "a" * 40
NOW = "2026-10-01T00:00:00+00:00"
CLEAN_DIFF = "diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -1 +1,2 @@\n+const x = 1\n"
OPEN = {"state": "open", "merged": False, "head": {"sha": HEAD}, "mergeable_state": "clean"}
PASSED = {"cmd": "tsc", "head_sha": HEAD, "base_sha": "f" * 40, "exit": 0, "duration_s": 1.0}
COMPILE_FAILED = {**PASSED, "exit": 20, "error_excerpt": "error TS2739: missing"}
NO_DOCKER = {"cmd": "tsc", "head_sha": HEAD, "error": "OSError: docker not found",
             "duration_s": 0.1}


class Env(SimpleNamespace):
    def pr(self):
        return self.store.load_pr(5)

    def entries(self, kind: str = "merge") -> list[dict]:
        return [e for e in self.recorded if e["kind"] == kind]


@pytest.fixture
def env(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({
        "pr": 5,
        "meta": {"title": "t", "author": "mallory", "state": "open", "draft": False,
                 "head_sha": HEAD, "checked_at": NOW},
        "threat": threat_section(HEAD, NOW),
        "security": {"verdict": "YELLOW", "checked_at": NOW, "against_head_sha": HEAD,
                     "findings": []},
        "verify": {"outcome": "escalate", "checked_at": NOW, "against_head_sha": HEAD,
                   "against_base_sha": "b" * 40},
        "issues": {"linked": [{"issue": 8723, "how": "explicit"},
                              {"issue": 8723, "how": "explicit"},
                              {"issue": 900, "how": "subsystem"},
                              {"issue": 55, "how": "explicit"},
                              {"issue": 77, "how": "explicit"}]}})
    monkeypatch.setattr(data, "_store", st)
    data.refresh()
    diffs = tmp_path / "diffs"
    diffs.mkdir()
    (diffs / f"{HEAD}.diff").write_text(CLEAN_DIFF)
    monkeypatch.setattr(diff_cache, "DIFFS", diffs)
    monkeypatch.setattr(executor, "_threat_rescan", REAL_RESCAN)
    monkeypatch.setattr(executor, "_live_ci_block", REAL_LIVE_CI_BLOCK)

    e = Env(store=st, live=dict(OPEN), ci={"head": HEAD, "ci": "passing", "unreported": []},
            block=None, compile=PASSED, token="tok", recorded=[], merges=[],
            issue_states={8723: "open", 55: "closed"}, reflected=[])

    # The merge gate's policy is pipeline/gates.py's own; here it blocks on
    # `e.block` and opens to an operator's reason unless the block is "other".
    def eligibility(rec, changed_paths=None, override_reason=None, **k):
        if e.block is None or (e.block != "other" and override_reason):
            return True, "passed"
        return False, {"security": "security YELLOW", "verify": "verify escalate",
                       "unreviewed": "no security review", "other": "CI failing"}[e.block]
    monkeypatch.setattr(gates, "merge_eligibility", eligibility)
    monkeypatch.setattr(gates, "security_overridable", lambda rec, **k: e.block == "security")
    monkeypatch.setattr(gates, "verify_overridable", lambda rec, **k: e.block == "verify")
    monkeypatch.setattr(gates, "security_waivable", lambda rec, **k: e.block == "unreviewed")

    monkeypatch.setattr(executor.gh, "fetch_pr", lambda n, timeout=30: e.live)
    monkeypatch.setattr(live_prs, "fetch", lambda prs, **k: ({5: e.ci} if e.ci else {}, set()))
    monkeypatch.setattr(service, "live_changed_paths", lambda n: [])
    monkeypatch.setattr(compile_preflight, "run_for_merge",
                        lambda n, head: e.compile(n, head) if callable(e.compile) else e.compile)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: e.token)
    monkeypatch.setattr(caps, "capabilities", lambda: {"login": "operator"})
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **f: e.recorded.append({"kind": kind, **f}))
    monkeypatch.setattr(issues_mod, "issue_state", lambda n: e.issue_states.get(int(n)))
    monkeypatch.setattr(issues_mod, "reflect_issue_state",
                        lambda n, state, reason=None: e.reflected.append((int(n), state)))

    def bot_merge_run(argv, token):
        # What the store held at the moment the merge fired.
        pr = st.load_pr(5)
        e.merges.append({"argv": argv, "security_override": pr.security_override,
                         "verify_override": pr.verify_override})
        return SimpleNamespace(returncode=0, stderr="")
    monkeypatch.setattr(executor, "bot_merge_run", bot_merge_run)
    return e


def test_a_live_merge_is_pinned_to_the_verified_head(env):
    res = executor.merge_pr(5, dry_run=False)

    assert res["status"] == "merged"
    [merge] = env.merges
    argv = merge["argv"]
    assert argv[argv.index("--match-head-commit") + 1] == HEAD
    assert env.pr().state == "merged"
    assert [r["status"] for r in env.entries()] == ["started", "merged"]
    assert env.entries()[-1]["compile_preflight"]["ok"] is True
    assert merge_progress.get(5) is None


def _blocklist_author(env):
    reg = threats.empty_registry()
    threats.block_actor(reg, "mallory", "another PR carried a payload", added="2026-10-02")
    env.store.save_threats(reg)


def _unreadable_head(env, monkeypatch):
    (diff_cache.DIFFS / f"{HEAD}.diff").unlink()
    monkeypatch.setattr(diff_cache, "fetch_diffs", lambda manifest, **k: (0, len(manifest)))
    rec = env.pr().raw
    rec["threat"]["against_head_sha"] = "older"
    env.store.save_pr(rec)


REFUSALS = {
    "gate": (lambda env, mp: setattr(env, "block", "other"), "merge gate: CI failing"),
    "author blocklisted since the scan": (lambda env, mp: _blocklist_author(env),
                                          "threat rescan: malicious"),
    "head the rescan cannot read": (_unreadable_head,
                                    "threat rescan: threat scan stale or missing"),
    "github unreachable": (lambda env, mp: setattr(env, "live", None),
                           "pre-flight: couldn't confirm the PR's live state"),
    "closed upstream": (lambda env, mp: env.live.update(state="closed"),
                        "pre-flight: PR is closed upstream"),
    "merged upstream": (lambda env, mp: env.live.update(merged=True), "already merged"),
    "head moved": (lambda env, mp: env.live.update(head={"sha": "b" * 40}),
                   "pre-flight: head moved since analysis"),
    "now conflicts": (lambda env, mp: env.live.update(mergeable_state="dirty"),
                      "merge conflicts"),
    "ci failing": (lambda env, mp: env.ci.update(ci="failing"), "CI is failing"),
    "required check never ran": (lambda env, mp: env.ci.update(ci="unreported",
                                                               unreported=["ci / e2e"]),
                                 "ci / e2e"),
    "ci read at another head": (lambda env, mp: env.ci.update(head="other"), "retry"),
    "ci unreadable": (lambda env, mp: setattr(env, "ci", None), "retry"),
    "ci unknown": (lambda env, mp: env.ci.update(ci="unknown"), "retry"),
    "compile failure": (lambda env, mp: setattr(env, "compile", COMPILE_FAILED), "TS2739"),
    "sandbox unavailable": (lambda env, mp: setattr(env, "compile", NO_DOCKER),
                            "docker not found"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_every_refusal_fires_nothing_writes_no_override_and_is_logged(env, monkeypatch, case):
    # The operator's reason opens a security block, so each later check is
    # what refuses — and must refuse before the override is written.
    env.block = "security"
    arrange, named = REFUSALS[case]
    arrange(env, monkeypatch)

    res = executor.merge_pr(5, dry_run=False, reason="ship it")

    assert res["status"] == "blocked", res
    assert named in res["detail"]
    assert env.merges == []
    assert env.pr().security_override is None
    assert env.entries()[-1]["status"] == "blocked"
    assert env.entries()[-1]["detail"] == res["detail"]
    assert merge_progress.get(5) is None


def test_a_refusal_records_what_the_live_read_saw(env):
    env.live = dict(OPEN, state="closed")
    executor.merge_pr(5, dry_run=True)
    assert env.pr().state == "closed"
    env.live = dict(OPEN, head={"sha": "b" * 40})
    executor.merge_pr(5, dry_run=True)
    assert env.pr().live_head_sha == "b" * 40


def test_a_ci_block_replaces_the_stored_ci(env):
    env.ci.update(ci="unreported", unreported=["ci / e2e"])
    executor.merge_pr(5, dry_run=True)
    assert env.pr().ci == "unreported"


@pytest.mark.parametrize("block, reason, dry_run, status, detail, written", [
    ("security", None, True, "blocked", "security YELLOW", None),
    ("security", "mirrors upstream", True, "dry-run", "security-YELLOW override", None),
    ("security", "mirrors upstream", False, "merged", "merged", "security_override"),
    ("verify", "reproduced by hand", True, "dry-run", "verify-escalate override", None),
    ("verify", "reproduced by hand", False, "merged", "merged", "verify_override"),
    ("unreviewed", "docs typo", True, "dry-run", "without a security review", None),
    ("unreviewed", "docs typo", False, "merged", "merged", None),
    (None, "unneeded note", False, "merged", "merged", None),
])
def test_an_operators_reason(env, block, reason, dry_run, status, detail, written):
    env.block = block

    res = executor.merge_pr(5, dry_run=dry_run, reason=reason)

    assert res["status"] == status and detail in res["detail"]
    pr = env.pr()
    for section in ("security_override", "verify_override"):
        if section == written:
            # Logged to its own section, by the operator, before the merge fired.
            assert getattr(pr, section)["reason"] == reason
            assert getattr(pr, section)["by"] == "operator"
            assert env.merges[0][section]["reason"] == reason
        else:
            assert getattr(pr, section) is None
    if block == "unreviewed" and reason:
        assert res["security_waiver"] == reason
        assert env.entries()[-1]["security_waiver"] == reason


@pytest.mark.parametrize("compile_result, compile_reason, status", [
    (NO_DOCKER, "  no Docker here  ", "merged"),
    (NO_DOCKER, None, "blocked"),
    (COMPILE_FAILED, "ship it", "blocked"),
])
def test_only_a_preflight_that_could_not_run_is_waivable(env, compile_result,
                                                        compile_reason, status):
    env.compile = compile_result

    res = executor.merge_pr(5, dry_run=False, compile_reason=compile_reason)

    assert res["status"] == status
    if status == "merged":
        assert res["compile_waiver"] == "no Docker here"
        assert env.entries()[-1]["compile_waiver"] == "no Docker here"
    else:
        assert res["compile_waivable"] is (compile_result is NO_DOCKER)
        assert "compile_waiver" not in res


def test_a_dry_run_never_boots_the_sandbox_and_fires_nothing(env):
    env.compile = lambda n, head: pytest.fail("the preflight ran on a dry-run")
    res = executor.merge_pr(5, dry_run=True)
    assert res["status"] == "dry-run" and "compile_preflight" not in res
    assert env.merges == [] and env.pr().state == "open"


def test_without_a_bot_token_a_live_merge_is_forced_to_a_dry_run(env):
    env.token = None
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "dry-run" and res["forced"] is True
    assert env.merges == []


def test_an_unconfigured_compile_command_merges_without_a_preflight(env):
    env.compile = None
    res = executor.merge_pr(5, dry_run=False)
    assert res["status"] == "merged" and "compile_preflight" not in res


def test_a_live_merge_reports_each_step_and_refuses_a_second_click(env):
    seen: list = []

    def run_for_merge(n, head):
        assert [r["status"] for r in env.entries()] == ["started"]
        progress.step("building img")
        seen.append(merge_progress.get(5))
        seen.append(executor.merge_pr(5, dry_run=False))
        return PASSED
    env.compile = run_for_merge

    assert executor.merge_pr(5, dry_run=False)["status"] == "merged"

    progress_seen, second = seen
    assert progress_seen["step"] == "building img" and progress_seen["head_sha"] == HEAD
    assert second["status"] == "blocked" and "already running" in second["detail"]
    assert len(env.merges) == 1
    assert merge_progress.get(5) is None


def test_progress_clears_when_the_merge_raises(env):
    env.compile = lambda n, head: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        executor.merge_pr(5, dry_run=False)
    assert merge_progress.get(5) is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_merge_credits_the_issues_it_closes(env, dry_run):
    # Only an explicit (Fixes/Closes) link closes on merge; one already closed
    # was not this merge's doing; an issue never ingested still closes.
    executor.merge_pr(5, dry_run=dry_run)

    closes = env.entries("issue-close")
    assert [(c["issue"], c["fixed_by"], c["via"]) for c in closes] == [
        (8723, 5, "merge"), (77, 5, "merge")]
    assert {c["status"] for c in closes} == {"dry-run" if dry_run else "closed"}
    assert env.reflected == ([] if dry_run else [(8723, "closed"), (77, "closed")])
