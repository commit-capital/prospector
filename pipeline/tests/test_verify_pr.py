"""The per-PR headless VERIFY orchestrator (pipeline/verify_pr.py): safety
floor, base-pin preflight, blind → sandbox → judge → outcome sequencing, and
the verify_request transitions each path records. Agents, Docker, and the
sandbox are stubbed — gates.verify_outcome is exercised for real through
verify_driver.commit_outcomes."""
from __future__ import annotations

import socket

import pytest

from pipeline import diff_cache
from pipeline import profile
from pipeline import store as S
from pipeline import verify_driver
from pipeline import verify_pr as vp
from pipeline.storekit import now as _now
from pipeline.testsupport import set_section

BLIND_OK = {"has_test": True, "test_cmd": "pnpm -s test x", "faithful": True,
            "confidence": "high", "claimed_symptom": "boom on save",
            "expected_red_signature": "AssertionError: boom",
            "repro_command": None, "expected_repro_signature": None,
            "from_linked_issue": False, "requires_live_agent": False,
            "reasoning": "test exercises the claimed defect"}

JUDGE_OK = {"red_reason_match": {"matches": True, "confidence": "high",
                                 "reasoning": "red output matches the prediction"},
            "repro_reason_match": {"applicable": False, "matches": None,
                                   "reasoning": "no repro ran"},
            "findings": []}

AUTHOR_OK = {"can_author": True,
            "files": [{"path": "src/repro.test.ts",
                       "contents": "test('repro', () => {})\n"}],
            "expected_red_signature": "AssertionError: boom",
            "confidence": "high", "reasoning": "authored test reproduces the defect"}


def _ev(rec, base, tier, *, red=20, green=0):
    """Host evidence in verify_driver.verify_pr's shape: a completed run with a
    complete regress leg."""
    return {"pr": rec.n, "head_sha": rec.head_sha, "base_sha": base, "tier": tier,
            "blind_adequacy": rec.verify_signals.get("blind_adequacy") or {},
            "red_green": {"apply_exit": 0, "red_exit": red, "green_exit": green,
                          "red_output_tail": "AssertionError: boom",
                          "green_output_tail": "ok",
                          "red_exit_confirm": red if (red == 20 and green == 0) else None,
                          "green_exit_confirm": green if (red == 20 and green == 0) else None},
            "independent_repro": {"ran": False, "exit_code": None,
                                  "from_linked_issue": False, "output_tail": ""},
            "regress": {"ran": True, "exit_first": 0, "exit_confirm": None,
                        "confirmed": False, "flake": False, "excluded_count": 0,
                        "new_failures": []}}


@pytest.fixture
def store(tmp_path):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 1, "meta": {"title": "fix boom", "state": "open",
                                  "head_sha": "a" * 40}})
    return st


@pytest.fixture
def pinned(store, tmp_path, monkeypatch):
    """A usable pinned base: registry + clone dir + image, with the diff cached
    and parseable, agents answering, and the sandbox stubbed to a clean run.
    The pin carries a suite, so the profile supplies a matching contract."""
    store.save_verify_base({"host": socket.gethostname(), "base_sha": "b" * 40,
                            "tier": 0, "pinned_at": _now(),
                            "baseline_failing": [], "baseline_captured_at": _now()})
    monkeypatch.setattr(
        verify_driver.profile, "active",
        lambda: profile.RepoProfile(verify=profile.VerifyPolicy(
            suite=profile.SuiteConfig(wrapper="scripts/w.mjs",
                                      server_project="@x/server"))))
    monkeypatch.setattr(verify_driver, "SCRATCH", tmp_path / "scratch")
    clone = tmp_path / "clone"
    clone.mkdir()
    monkeypatch.setattr(verify_driver, "base_clone_dir", lambda sha: clone)
    monkeypatch.setattr(vp, "_daemon_available", lambda: True)
    monkeypatch.setattr(vp, "_image_exists", lambda image: True)
    monkeypatch.setattr(diff_cache, "fetch_diff", lambda pr, head, *a, **k: True)
    monkeypatch.setattr(verify_driver, "changed_paths_for", lambda rec: ["src/x.test.ts"])
    # the harness self-test passes by default; its cache is per-image and
    # module-level, so clear it between tests
    vp._canary_cache.clear()
    monkeypatch.setattr(verify_driver, "run_canaries", lambda image, base, tier: [])

    answers = {"blind adequacy": dict(BLIND_OK), "post-run judge": dict(JUDGE_OK)}
    calls: list[str] = []

    def fake_agent(prompt, step):
        calls.append(step)
        ans = answers.get(step)
        return (ans, None) if ans is not None else (None, f"{step}: stubbed failure")

    monkeypatch.setattr(vp, "_call_agent_json", fake_agent)

    sandbox = {"called": 0}

    def fake_sandbox(rec, image, base, tier, baseline, *, suite_config=None):
        sandbox["called"] += 1
        return _ev(rec, base, tier)

    monkeypatch.setattr(verify_driver, "verify_pr", fake_sandbox)
    return {"answers": answers, "agent_calls": calls, "sandbox": sandbox,
            "clone": clone}


def _request(store, n=1):
    return store.load_pr(n).verify_request or {}


def test_unknown_pr(store):
    assert vp.run(store, 999) == 1


def test_from_queue_skips_a_pr_no_longer_queued(store, pinned):
    store.edit_pr(1).record_verify_request("cancelled", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    assert _request(store)["status"] == "cancelled"
    assert pinned["agent_calls"] == []


def test_refuses_closed_pr(store, pinned):
    rec = store.load_pr(1).raw
    rec["meta"]["state"] = "closed"
    store.save_pr(rec)
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["status"] == "error"
    assert req["error_kind"] == "refused-safety"


def test_refuses_malicious(store, pinned):
    set_section(store, 1, "threat", {"verdict": "malicious"})
    assert vp.run(store, 1) == 1
    assert _request(store)["error_kind"] == "refused-safety"
    assert pinned["agent_calls"] == []


def test_no_pinned_base(store, tmp_path, monkeypatch):
    monkeypatch.setattr(vp, "_image_exists", lambda image: True)
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["error_kind"] == "no-base"
    assert "prepare-base" in req["error"]


def test_missing_image_is_no_base(store, pinned, monkeypatch):
    monkeypatch.setattr(vp, "_image_exists", lambda image: False)
    assert vp.run(store, 1) == 1
    assert _request(store)["error_kind"] == "no-base"


def test_an_unreachable_daemon_is_not_a_missing_base(store, pinned, monkeypatch):
    """A stopped Docker daemon answers every image query the same way a missing
    image does. The preflight names the daemon so the operator restarts it,
    rather than sending them to prepare-base for a base that is already here."""
    monkeypatch.setattr(vp, "_daemon_available", lambda: False)
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert req["status"] == "waiting-for-base"
    assert "Docker daemon" in req["error"]
    assert "prepare-base" not in req["error"]


def test_an_unreachable_daemon_park_ignores_the_wait_budget(store, pinned, monkeypatch):
    """The wait budget bounds a base this machine must prepare — an operator
    action. No re-queue makes a stopped daemon answer, so a request parked on
    the outage keeps waiting however long it lasts and runs when it lifts."""
    monkeypatch.setattr(vp, "_daemon_available", lambda: False)
    store.edit_pr(1).record_verify_request(
        "waiting-for-base", queued_at="2026-07-15T00:00:00+00:00",
        error_kind="no-base", error="the daemon is down")
    assert vp.run(store, 1, from_queue=True) == 0
    assert _request(store)["status"] == "waiting-for-base"

    monkeypatch.setattr(vp, "_daemon_available", lambda: True)
    assert vp.run(store, 1, from_queue=True) == 0
    assert _request(store)["status"] == "done"


def test_a_direct_run_errors_when_the_daemon_is_down(store, pinned, monkeypatch):
    """A direct CLI run has no worker to retry it, so the outage is terminal
    for that run — recorded as the sandbox failure it is, not a missing base."""
    monkeypatch.setattr(vp, "_daemon_available", lambda: False)
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["status"] == "error"
    assert req["error_kind"] == "sandbox-error"


def test_no_base_parks_a_queued_request_as_waiting(store, monkeypatch):
    """A queued request that hits no-base is parked `waiting-for-base` (the
    worker retries it), not terminal-errored — no baseline ever ran."""
    monkeypatch.setattr(vp, "_image_exists", lambda image: True)
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert req["status"] == "waiting-for-base"
    assert req["error_kind"] == "no-base"
    assert "prepare-base" in req["error"]
    assert req["queued_at"]


def test_a_park_stamps_no_outcome_in_the_ledger(store, monkeypatch):
    """The ledger's `outcome` stat names what THIS run concluded. A park ran
    nothing, so it stamps none even though the PR carries an outcome an earlier
    run left — a stale word here reads as a verification that never happened."""
    monkeypatch.setattr(vp, "_image_exists", lambda image: True)
    set_section(store, 1, "verify", {"outcome": "agent-verified", "signals": {},
                                     "against_base_sha": "b" * 40})
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    stats = store.runs()[-1].raw["stats"]
    assert stats["status"] == "waiting-for-base"
    assert stats["outcome"] is None
    assert store.load_pr(1).verify_outcome == "agent-verified"


def test_no_base_wait_is_bounded(store, monkeypatch):
    """Past WAITING_FOR_BASE_MAX_HOURS since queueing, the no-base failure is
    terminal — the request errors with the current no-base message."""
    monkeypatch.setattr(vp, "_image_exists", lambda image: True)
    store.edit_pr(1).record_verify_request(
        "waiting-for-base", queued_at="2026-07-15T00:00:00+00:00",
        error_kind="no-base", error="no pinned base")
    assert vp.run(store, 1, from_queue=True) == 1
    req = _request(store)
    assert req["status"] == "error"
    assert req["error_kind"] == "no-base"
    assert "gave up" in req["error"]


def test_from_queue_picks_up_a_waiting_request(store, pinned):
    """Once the base lands, a parked request runs to completion from the
    worker's --from-queue pickup, keeping its original queued_at."""
    store.edit_pr(1).record_verify_request(
        "waiting-for-base", queued_at="2026-07-16T00:00:00+00:00",
        error_kind="no-base", error="no pinned base")
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert req["status"] == "done"
    assert req["queued_at"] == "2026-07-16T00:00:00+00:00"


def test_an_unhealthy_harness_refuses_before_running_the_pr(store, pinned, monkeypatch):
    # The canary self-test failed → no verdict is trustworthy, so the PR is
    # refused (sandbox-error) and no blind/judge agent ever runs.
    vp._canary_cache.clear()
    monkeypatch.setattr(verify_driver, "run_canaries",
                        lambda image, base, tier: ["a NON-fix patch still passed"])
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["status"] == "error" and req["error_kind"] == "sandbox-error"
    assert "canary" in req["error"]
    assert pinned["agent_calls"] == []   # never reached the blind agent


def test_auto_source_refuses_without_current_green_security(store, pinned):
    """An auto-queued request (the idle hunter's own pick) is re-checked at
    run time, not trusted from queue time: no current GREEN security verdict
    means the sandbox never runs, regardless of how long ago it was queued."""
    store.edit_pr(1).record_verify_request("queued", queued_at=_now(), source="auto")
    assert vp.run(store, 1, from_queue=True) == 1
    req = _request(store)
    assert req["error_kind"] == "refused-safety"
    assert pinned["agent_calls"] == []


def test_operator_source_not_refused_by_the_auto_green_floor(store, pinned):
    """An operator-queued request (source unset) keeps its existing, looser
    bar: it is not stopped by the auto-only GREEN-security floor, even with
    no security verdict at all."""
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert not (req.get("error_kind") == "refused-safety"
               and "GREEN security verdict" in (req.get("error") or ""))


def test_unreadable_diff_fails_closed(store, pinned, monkeypatch):
    monkeypatch.setattr(verify_driver, "changed_paths_for", lambda rec: [])
    assert vp.run(store, 1) == 1
    assert _request(store)["error_kind"] == "refused-safety"


def test_deps_touched_records_the_wave_refusal(store, pinned, monkeypatch):
    monkeypatch.setattr(verify_driver, "changed_paths_for",
                        lambda rec: ["package.json"])
    assert vp.run(store, 1) == 0
    rec = store.load_pr(1)
    assert rec.verify_outcome == "deps-touched"
    assert _request(store)["status"] == "done"
    assert pinned["agent_calls"] == []


def test_a_suite_pin_with_a_suiteless_profile_errors_loudly(store, pinned, monkeypatch):
    """The pin captured a baseline, so the profile must still carry the suite
    contract — losing it must fail the run loudly, never silently skip the
    regression gate."""
    monkeypatch.setattr(verify_driver.profile, "active", lambda: profile.GENERIC)
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["error_kind"] == "sandbox-error"
    assert "verify.suite" in req["error"]


def test_a_no_suite_pin_runs_without_a_contract(store, pinned, monkeypatch):
    monkeypatch.setattr(verify_driver.profile, "active", lambda: profile.GENERIC)
    store.save_verify_base({"host": socket.gethostname(), "base_sha": "b" * 40,
                            "tier": 0, "pinned_at": _now(),
                            "baseline_failing": [], "baseline_captured_at": _now(),
                            "suite": False})
    assert vp.run(store, 1) == 0
    assert _request(store)["status"] == "done"


def test_happy_path_verified_fix(store, pinned):
    store.edit_pr(1).record_verify_request("queued", queued_at="2026-07-16T00:00:00+00:00")
    assert vp.run(store, 1, from_queue=True) == 0
    rec = store.load_pr(1)
    assert rec.verify_outcome == "verified-fix"
    assert rec.verify_signals["red_reason_match"]["matches"] is True
    req = _request(store)
    assert req["status"] == "done"
    assert req["queued_at"] == "2026-07-16T00:00:00+00:00"
    assert req["started_at"] and req["finished_at"]
    assert pinned["agent_calls"] == ["blind adequacy", "post-run judge"]
    runs = store.runs()
    assert runs[-1].phase == "verify:single"
    assert runs[-1].raw["stats"]["outcome"] == "verified-fix"
    assert "trigger" not in runs[-1].raw

    # A re-queue with source="auto" (the idle hunter's own pick) stamps the
    # ledger entry with trigger="autohunt" — provenance the runs ledger and
    # its history endpoint later surface; the heartbeat carries the opt-in
    # flag and failure memory, not per-run provenance.
    set_section(store, 1, "security", {"verdict": "GREEN", "findings": []})
    store.edit_pr(1).record_verify_request("queued", queued_at=_now(), source="auto")
    assert vp.run(store, 1, from_queue=True) == 0
    runs = store.runs()
    assert runs[-1].phase == "verify:single"
    assert runs[-1].raw["trigger"] == "autohunt"


def test_lanes_are_persisted_to_signals(store, pinned, monkeypatch):
    """ev["lanes"] from verify_driver.verify_pr (the merge-gate compile/build
    lanes) is copied into the PR's persisted verify signals, next to the
    authored_test copy — the shape gates.verify_outcome reads via
    commit_outcomes."""
    def fake_sandbox(rec, image, base, tier, baseline, *, suite_config=None):
        return {**_ev(rec, base, tier),
                "lanes": {"compile": {"cmd": "c", "exit": 0, "ok": True,
                                      "duration_s": 1.0}}}

    monkeypatch.setattr(verify_driver, "verify_pr", fake_sandbox)
    assert vp.run(store, 1) == 0
    rec = store.load_pr(1)
    assert rec.verify_signals["lanes"]["compile"]["ok"] is True


def test_blind_agent_failure(store, pinned):
    pinned["answers"]["blind adequacy"] = None
    assert vp.run(store, 1) == 1
    assert _request(store)["error_kind"] == "agent-failed"
    assert pinned["sandbox"]["called"] == 0


class TestTransientAgentFailures:
    """A failed blind or judge agent call gets ONE in-run retry; a run that
    still has no usable answer parks its queued request for the worker to
    retry, carrying every attempt's failure detail as the log_tail."""

    def test_blind_failure_parks_queued_with_forensics(self, store, pinned, monkeypatch):
        calls: list[str] = []

        def fake(prompt, step):
            calls.append(step)
            return None, f"{step}: claude exited 1; last output: kaboom"

        monkeypatch.setattr(vp, "_call_agent_json", fake)
        store.edit_pr(1).record_verify_request("queued", queued_at=_now())
        assert vp.run(store, 1, from_queue=True) == 0
        req = _request(store)
        assert req["status"] == "queued"
        assert req["error_kind"] == "agent-failed"
        assert req["attempts"] == 1
        assert "kaboom" in req["log_tail"]
        assert calls == ["blind adequacy", "blind adequacy"]

    def test_blind_recovers_on_the_one_retry(self, store, pinned, monkeypatch):
        seq: list[tuple[dict | None, str | None]] = [
            (None, "blind adequacy: claude exited 1"), (dict(BLIND_OK), None)]
        calls: list[str] = []

        def fake(prompt, step):
            calls.append(step)
            if step == "blind adequacy":
                return seq.pop(0)
            return dict(JUDGE_OK), None

        monkeypatch.setattr(vp, "_call_agent_json", fake)
        assert vp.run(store, 1) == 0
        assert store.load_pr(1).verify_outcome == "verified-fix"
        assert calls.count("blind adequacy") == 2

    def test_unusable_blind_json_is_named_in_the_log_tail(self, store, pinned, monkeypatch):
        def fake(prompt, step):
            return {"weird": 1}, None

        monkeypatch.setattr(vp, "_call_agent_json", fake)
        store.edit_pr(1).record_verify_request("queued", queued_at=_now())
        assert vp.run(store, 1, from_queue=True) == 0
        req = _request(store)
        assert req["error_kind"] == "agent-failed"
        assert "unusable" in req["log_tail"]
        assert "weird" in req["log_tail"]

    def test_judge_failure_parks_queued_with_forensics(self, store, pinned, monkeypatch):
        calls: list[str] = []

        def fake(prompt, step):
            calls.append(step)
            if step == "blind adequacy":
                return dict(BLIND_OK), None
            return None, f"{step}: claude did not exit within 1200s"

        monkeypatch.setattr(vp, "_call_agent_json", fake)
        store.edit_pr(1).record_verify_request("queued", queued_at=_now())
        assert vp.run(store, 1, from_queue=True) == 0
        req = _request(store)
        assert req["status"] == "queued"
        assert req["error_kind"] == "agent-failed"
        assert "1200s" in req["log_tail"]
        assert calls.count("post-run judge") == 2


def test_no_test_resolves_without_judge_or_signal(store, pinned, monkeypatch):
    # No test file in the diff → the driver derives no command → unverifiable.
    # The AUTHOR pass fires for this no-test PR; its stub returns no answer
    # (agent-failed), so the lane still settles to unverifiable-no-test.
    monkeypatch.setattr(verify_driver, "changed_paths_for", lambda rec: ["src/impl.ts"])
    pinned["answers"]["blind adequacy"] = dict(BLIND_OK, has_test=False,
                                               test_cmd=None, faithful=False)

    def no_run(rec, image, base, tier, baseline, *, suite_config=None):
        return {**_ev(rec, base, tier),
                "red_green": {"apply_exit": None, "red_exit": None, "green_exit": None,
                              "red_output_tail": "", "green_output_tail": ""},
                "regress": {"ran": False, "skipped_reason": "red-green-not-clean"}}

    monkeypatch.setattr(verify_driver, "verify_pr", no_run)
    assert vp.run(store, 1) == 0
    rec = store.load_pr(1)
    assert rec.verify_outcome == "unverifiable-no-test"
    assert "red_reason_match" not in rec.verify_signals
    assert pinned["agent_calls"] == ["blind adequacy", "author test"]
    assert _request(store)["status"] == "done"


def test_requires_live_agent_spends_no_sandbox_time(store, pinned):
    pinned["answers"]["blind adequacy"] = dict(BLIND_OK, requires_live_agent=True)
    assert vp.run(store, 1) == 0
    assert store.load_pr(1).verify_outcome == "unverifiable-needs-live-agent"
    assert pinned["sandbox"]["called"] == 0
    assert _request(store)["status"] == "done"


def test_errored_run_holds(store, pinned, monkeypatch):
    def errored(rec, image, base, tier, baseline, *, suite_config=None):
        return {**_ev(rec, base, tier),
                "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 137,
                              "red_output_tail": "AssertionError: boom",
                              "green_output_tail": "oom"}}

    monkeypatch.setattr(verify_driver, "verify_pr", errored)
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["error_kind"] == "hold"
    # the section keeps its null outcome: no verdict is trusted from a failed run
    assert store.load_pr(1).verify_outcome is None


def test_probe_failure_is_sandbox_error(store, pinned, monkeypatch):
    def probe_fail(rec, image, base, tier, baseline, *, suite_config=None):
        raise verify_driver.ProbeFailure("isolation unproven")

    monkeypatch.setattr(verify_driver, "verify_pr", probe_fail)
    assert vp.run(store, 1) == 1
    assert _request(store)["error_kind"] == "sandbox-error"


def _probe_fail(rec, image, base, tier, baseline, *, suite_config=None):
    raise verify_driver.ProbeFailure("isolation unproven")


def test_a_transient_sandbox_failure_parks_the_queued_request(store, pinned, monkeypatch):
    """A queued run that dies on sandbox infrastructure re-queues itself with
    an attempt count (the worker re-picks it after a rest), not a terminal
    error an operator must notice and re-queue by hand."""
    monkeypatch.setattr(verify_driver, "verify_pr", _probe_fail)
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert req["status"] == "queued"
    assert req["error_kind"] == "sandbox-error"
    assert req["attempts"] == 1
    assert "isolation unproven" in req["error"]
    entry = store.runs(limit=1)[0].raw
    assert entry["stats"]["status"] == "queued"


def test_a_patch_fetch_failure_parks_as_fetch_error(store, pinned, monkeypatch):
    def fetch_fail(rec, image, base, tier, baseline, *, suite_config=None):
        raise verify_driver.FetchFailure("cannot fetch patch for PR #1: HTTP 500")

    monkeypatch.setattr(verify_driver, "verify_pr", fetch_fail)
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert req["status"] == "queued"
    assert req["error_kind"] == "fetch-error"
    assert req["attempts"] == 1
    assert "HTTP 500" in req["error"]


def test_an_unfetchable_diff_parks_as_fetch_error(store, pinned, monkeypatch):
    """A diff that cannot be fetched is a transient upstream failure, not a
    safety refusal: the queued request parks for retry."""
    monkeypatch.setattr(diff_cache, "fetch_diff", lambda pr, head, *a, **k: False)
    store.edit_pr(1).record_verify_request("queued", queued_at=_now())
    assert vp.run(store, 1, from_queue=True) == 0
    req = _request(store)
    assert req["status"] == "queued"
    assert req["error_kind"] == "fetch-error"
    assert req["attempts"] == 1


def test_transient_parking_gives_up_after_the_attempt_cap(store, pinned, monkeypatch):
    monkeypatch.setattr(verify_driver, "verify_pr", _probe_fail)
    store.edit_pr(1).record_verify_request(
        "queued", queued_at=_now(), error_kind="sandbox-error",
        error="isolation unproven", attempts=vp.TRANSIENT_MAX_ATTEMPTS - 1)
    assert vp.run(store, 1, from_queue=True) == 1
    req = _request(store)
    assert req["status"] == "error"
    assert req["error_kind"] == "sandbox-error"
    assert "gave up" in req["error"]


def test_a_direct_run_transient_failure_stays_terminal(store, pinned, monkeypatch):
    """A run outside the queue has no worker to re-pick it: the failure
    records a terminal error, exactly like the no-base preflight."""
    def fetch_fail(rec, image, base, tier, baseline, *, suite_config=None):
        raise verify_driver.FetchFailure("HTTP 500")

    monkeypatch.setattr(verify_driver, "verify_pr", fetch_fail)
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["status"] == "error"
    assert req["error_kind"] == "fetch-error"


def test_crash_records_exception_with_traceback(store, pinned, monkeypatch):
    def boom(rec, image, base, tier, baseline, *, suite_config=None):
        raise KeyboardInterrupt  # escapes the sandbox-step except Exception
    monkeypatch.setattr(verify_driver, "verify_pr", boom)
    with pytest.raises(KeyboardInterrupt):
        vp.run(store, 1)


def test_unexpected_error_lands_in_exception_kind(store, pinned, monkeypatch):
    monkeypatch.setattr(vp, "_blind_verdict",
                        lambda rec, clone: (_ for _ in ()).throw(TypeError("bug")))
    assert vp.run(store, 1) == 1
    req = _request(store)
    assert req["error_kind"] == "exception"
    assert "TypeError" in req["log_tail"]


# The PR's cached diff for the repro-rejection tests: it adds the test file
# src/x.test.ts (the same path the pinned fixture's changed_paths_for reports),
# so a repro_command naming that file or its test title is structurally vacuous.
_DIFF_ADDS_TEST = (
    "diff --git a/src/x.test.ts b/src/x.test.ts\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/x.test.ts\n"
    "@@ -0,0 +1,2 @@\n"
    '+it("boom on save", () => {});\n'
    "diff --git a/src/x.ts b/src/x.ts\n"
    "--- a/src/x.ts\n"
    "+++ b/src/x.ts\n"
    "@@ -1 +1,2 @@\n"
    "+export const y = 2;\n"
)

VACUOUS_REPRO = "npx vitest run src/x.test.ts --testTimeout=5000"
VALID_REPRO = "node --test scripts/repro.mjs"


class TestBlindReproRejection:
    """A blind verdict whose repro_command targets a test the PR itself
    introduces is rejected BEFORE commit: the blind agent is re-run once with a
    rejection addendum naming the offending token, and a retry that still
    fails (or errors) commits the verdict with the repro fields nulled and
    blind_adequacy.repro_rejected recording why. Blindness is preserved — the
    vetting happens before commit_blind, before any container boots."""

    @pytest.fixture(autouse=True)
    def cached_diff(self, monkeypatch):
        monkeypatch.setattr(verify_driver, "cached_diff_text",
                            lambda rec: _DIFF_ADDS_TEST)

    def _agents(self, monkeypatch, blind_answers: list[dict | None]) -> dict:
        """Install an agent stub whose successive 'blind adequacy' calls pop
        from blind_answers (then fail with None); the judge always answers.
        Returns {'blind_prompts': [...], 'calls': [...]}."""
        seq = list(blind_answers)
        seen: dict = {"blind_prompts": [], "calls": []}

        def fake(prompt, step):
            seen["calls"].append(step)
            if step == "blind adequacy":
                seen["blind_prompts"].append(prompt)
                ans = seq.pop(0) if seq else None
                return ((dict(ans), None) if ans is not None
                        else (None, "blind adequacy: stubbed failure"))
            return dict(JUDGE_OK), None

        monkeypatch.setattr(vp, "_call_agent_json", fake)
        return seen

    def _blind(self, store):
        return store.load_pr(1).verify_signals["blind_adequacy"]

    def test_a_valid_first_answer_commits_with_one_blind_call(
            self, store, pinned, monkeypatch):
        seen = self._agents(monkeypatch, [dict(
            BLIND_OK, repro_command=VALID_REPRO, expected_repro_signature="boom")])
        assert vp.run(store, 1) == 0
        blind = self._blind(store)
        assert blind["repro_command"] == VALID_REPRO
        assert not blind.get("repro_rejected")
        assert seen["calls"].count("blind adequacy") == 1

    def test_a_vacuous_repro_retries_once_and_commits_the_retry(
            self, store, pinned, monkeypatch):
        seen = self._agents(monkeypatch, [
            dict(BLIND_OK, repro_command=VACUOUS_REPRO,
                 expected_repro_signature="no such file"),
            dict(BLIND_OK, repro_command=VALID_REPRO,
                 expected_repro_signature="boom")])
        assert vp.run(store, 1) == 0
        blind = self._blind(store)
        assert blind["repro_command"] == VALID_REPRO
        assert blind["expected_repro_signature"] == "boom"
        assert not blind.get("repro_rejected")
        assert seen["calls"].count("blind adequacy") == 2
        # The retry is the FULL blind prompt plus a rejection addendum naming
        # the offending token and restating the no-patch invariant.
        retry = seen["blind_prompts"][1]
        assert "Blind adequacy review" in retry
        assert "src/x.test.ts" in retry
        assert "WITHOUT" in retry
        assert "repro_command: null" in retry

    def test_a_still_vacuous_retry_commits_nulled_with_repro_rejected(
            self, store, pinned, monkeypatch):
        seen = self._agents(monkeypatch, [
            dict(BLIND_OK, repro_command=VACUOUS_REPRO,
                 expected_repro_signature="x"),
            dict(BLIND_OK, repro_command='npx vitest run -t "boom on save"',
                 expected_repro_signature="y")])
        # Blindness invariant: by the time the sandbox boots, the committed
        # blind verdict is already the vetted (nulled) one.
        sandbox_blind: dict = {}

        def capture(rec, image, base, tier, baseline, *, suite_config=None):
            sandbox_blind.update(rec.verify_signals["blind_adequacy"])
            return _ev(rec, base, tier)

        monkeypatch.setattr(verify_driver, "verify_pr", capture)
        assert vp.run(store, 1) == 0
        blind = self._blind(store)
        assert blind["repro_command"] is None
        assert blind["expected_repro_signature"] is None
        assert "boom on save" in blind["repro_rejected"]
        assert sandbox_blind["repro_command"] is None
        assert sandbox_blind["repro_rejected"] == blind["repro_rejected"]
        assert seen["calls"].count("blind adequacy") == 2
        # The run still completes and the rejection surfaces as a finding.
        rec = store.load_pr(1)
        assert rec.verify_outcome == "verified-fix"
        finding = [f for f in rec.verify_findings
                   if f.get("signal") == "repro-rejected"]
        assert len(finding) == 1
        assert "boom on save" in finding[0]["note"]

    def test_an_agent_error_on_retry_commits_nulled_with_repro_rejected(
            self, store, pinned, monkeypatch):
        seen = self._agents(monkeypatch, [
            dict(BLIND_OK, repro_command=VACUOUS_REPRO,
                 expected_repro_signature="x")])
        assert vp.run(store, 1) == 0
        blind = self._blind(store)
        assert blind["repro_command"] is None
        assert blind["expected_repro_signature"] is None
        assert "src/x.test.ts" in blind["repro_rejected"]
        assert seen["calls"].count("blind adequacy") == 2
        assert store.load_pr(1).verify_outcome == "verified-fix"

    def test_a_retry_declining_with_null_is_accepted_without_rejection(
            self, store, pinned, monkeypatch):
        seen = self._agents(monkeypatch, [
            dict(BLIND_OK, repro_command=VACUOUS_REPRO,
                 expected_repro_signature="x"),
            dict(BLIND_OK, repro_command=None, expected_repro_signature=None,
                 reasoning="no base surface exercises the defect")])
        assert vp.run(store, 1) == 0
        blind = self._blind(store)
        assert blind["repro_command"] is None
        assert not blind.get("repro_rejected")
        assert seen["calls"].count("blind adequacy") == 2


class TestAuthorPass:
    """The AUTHOR pass: a PR whose blind verdict finds no test gets an
    agent-authored reproduction test, validated and committed to the store
    BEFORE any sandbox run — the same pre-run integrity property as the blind
    verdict itself."""

    def test_no_test_pr_commits_authored_before_sandbox(self, store, pinned, monkeypatch):
        # No test file among the changed paths → commit_blind derives no
        # command (has_test=False), so the AUTHOR pass fires.
        monkeypatch.setattr(verify_driver, "changed_paths_for", lambda rec: ["src/x.ts"])
        pinned["answers"]["blind adequacy"] = dict(BLIND_OK, has_test=False,
                                                    test_cmd=None, faithful=False)
        pinned["answers"]["author test"] = dict(AUTHOR_OK)
        captured: dict = {}

        def fake_sandbox(rec, image, base, tier, baseline, *, suite_config=None):
            captured["authored_test"] = rec.verify_signals.get("authored_test")
            return _ev(rec, base, tier)

        monkeypatch.setattr(verify_driver, "verify_pr", fake_sandbox)
        assert vp.run(store, 1) == 0
        authored = captured["authored_test"]
        assert authored is not None
        assert authored["attempted"] is True
        assert authored["test_cmd"] is not None and "src/repro.test.ts" in authored["test_cmd"]
        assert "author test" in pinned["agent_calls"]

    def test_author_agent_failure_records_skip_and_continues(self, store, pinned, monkeypatch):
        # No test file among the changed paths → commit_blind derives no
        # command (has_test=False), so the AUTHOR pass fires.
        monkeypatch.setattr(verify_driver, "changed_paths_for", lambda rec: ["src/impl.ts"])
        pinned["answers"]["blind adequacy"] = dict(BLIND_OK, has_test=False,
                                                    test_cmd=None, faithful=False)
        pinned["answers"]["author test"] = None

        def no_run(rec, image, base, tier, baseline, *, suite_config=None):
            return {**_ev(rec, base, tier),
                    "red_green": {"apply_exit": None, "red_exit": None, "green_exit": None,
                                  "red_output_tail": "", "green_output_tail": ""},
                    "regress": {"ran": False, "skipped_reason": "red-green-not-clean"}}

        monkeypatch.setattr(verify_driver, "verify_pr", no_run)
        assert vp.run(store, 1) == 0
        rec = store.load_pr(1)
        authored = rec.verify_signals.get("authored_test")
        assert authored == {"attempted": True, "can_author": False, "files": [],
                            "test_cmd": None, "skipped_reason": "agent-failed"}
        assert rec.verify_outcome == "unverifiable-no-test"
        assert _request(store)["status"] == "done"

    def test_test_lane_pr_never_runs_the_author_pass(self, store, pinned):
        # BLIND_OK already carries a derived test_cmd — the author agent stub
        # is never registered for this call, so a call would surface as None.
        assert vp.run(store, 1) == 0
        assert "author test" not in pinned["agent_calls"]

    def test_authored_lane_judge_confirms_reaches_agent_verified(
            self, store, pinned, monkeypatch):
        # No test file among the changed paths → the AUTHOR pass fires; the
        # sandbox stub reports a clean, confirmed red->green on the authored
        # lane alone (the PR's own red_green stays unrun), and the judge rates
        # the authored red's reason a match — the outcome settles agent-verified.
        monkeypatch.setattr(verify_driver, "changed_paths_for", lambda rec: ["src/x.ts"])
        pinned["answers"]["blind adequacy"] = dict(BLIND_OK, has_test=False,
                                                    test_cmd=None, faithful=False)
        pinned["answers"]["author test"] = dict(AUTHOR_OK)
        pinned["answers"]["post-run judge"] = {
            "red_reason_match": {"matches": True, "confidence": "high", "reasoning": "j"},
            "repro_reason_match": {"applicable": False, "matches": None,
                                   "confidence": "high", "reasoning": ""},
            "findings": [],
        }

        def fake_sandbox(rec, image, base, tier, baseline, *, suite_config=None):
            authored = dict(rec.verify_signals.get("authored_test") or {},
                            red_exit=20, green_exit=0,
                            red_exit_confirm=20, green_exit_confirm=0,
                            red_output_tail="boom", green_output_tail="ok",
                            expected_red_signature="boom")
            return {**_ev(rec, base, tier, red=None, green=None),
                    "authored_test": authored}

        monkeypatch.setattr(verify_driver, "verify_pr", fake_sandbox)
        assert vp.run(store, 1) == 0
        rec = store.load_pr(1)
        assert rec.verify_outcome == "agent-verified"
        assert "post-run judge" in pinned["agent_calls"]
