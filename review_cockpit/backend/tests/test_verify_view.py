"""verify_view renders the stored VERIFY record for the cockpit: per-outcome
copy + tone, freshness against the merge-recency window, ANSI-stripped output
tails — and never alters the outcome or the store record itself."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pipeline import model, profile
from review_cockpit.backend import verify_view

HEAD = "abc123"
BASE = "b606869"


def _now(days_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _pr(verify: dict | None = None) -> model.Pr:
    rec: dict = {
        "pr": 1,
        "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                 "head_sha": HEAD, "checked_at": _now()},
    }
    if verify is not None:
        rec["verify"] = verify
    return model.Pr(None, rec)


def _verify(outcome: str | None = "verified-fix", *, days_ago: int = 0,
            head: str = HEAD, **over) -> dict:
    sec: dict = {"outcome": outcome, "tier": 1, "signals": {}, "findings": [],
                 "against_base_sha": BASE, "against_head_sha": head,
                 "checked_at": _now(days_ago)}
    sec.update(over)
    return sec


def test_no_section_returns_none():
    assert verify_view.verify_detail(_pr()) is None


def test_outcome_levels_cover_every_outcome():
    expected = {
        "verified-fix": "verified",
        "agent-verified": "verified",
        "escalate": "attention",
        "not-verified": "blocked",
        "needs-rebase": "blocked",
        "regressed": "blocked",
        "deps-touched": "blocked",
        "unverifiable-no-test": "info",
        "unverifiable-needs-live-agent": "info",
    }
    for outcome, level in expected.items():
        d = verify_view.verify_detail(_pr(_verify(outcome)))
        assert d is not None
        assert d["level"] == level, outcome
        assert d["outcome"] == outcome
        assert d["headline"]
        assert d["stale_reason"] is None


def test_null_outcome_is_pending():
    d = verify_view.verify_detail(_pr(_verify(None)))
    assert d is not None
    assert d["level"] == "pending" and d["outcome"] is None
    assert "blind verdict committed" in d["headline"]


def test_stale_verdict_says_it_no_longer_counts_for_merge():
    d = verify_view.verify_detail(_pr(_verify("verified-fix", head="older-head")))
    assert d is not None
    assert d["stale_reason"] is not None
    assert "no longer counts for merge" in d["detail"]
    # the outcome itself is rendered verbatim, never softened
    assert d["outcome"] == "verified-fix" and d["level"] == "verified"


def test_aged_out_verdict_is_stale():
    d = verify_view.verify_detail(_pr(_verify("verified-fix", days_ago=9)))
    assert d is not None
    assert d["stale_reason"] is not None


def test_signals_pass_through_with_tails_ansi_stripped():
    signals = {
        "blind_adequacy": {"faithful": True, "confidence": "high", "reasoning": "r"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                      "red_output_tail": "\x1b[1m\x1b[31mFAIL\x1b[39m\x1b[22m expected 2 to be 1",
                      "green_output_tail": "\x1b[32mPASS\x1b[39m"},
        "red_reason_match": {"matches": True, "confidence": "high"},
        "independent_repro": {"ran": True, "exit_code": 20,
                              "output_tail": "\x1b]0;title\x07plain"},
        "repro_reason_match": {"matches": False, "applicable": True},
    }
    findings = [{"title": "f", "detail": "d", "confidence": "high"}]
    pr = _pr(_verify("verified-fix", signals=signals, findings=findings))
    d = verify_view.verify_detail(pr)
    assert d is not None
    assert set(d["signals"]) == set(signals)
    assert d["signals"]["red_green"]["red_output_tail"] == "FAIL expected 2 to be 1"
    assert d["signals"]["red_green"]["green_output_tail"] == "PASS"
    assert d["signals"]["independent_repro"]["output_tail"] == "plain"
    # non-tail fields untouched
    assert d["signals"]["blind_adequacy"]["reasoning"] == "r"
    assert d["findings"] == findings
    # the store record itself is never mutated by the view
    assert "\x1b" in pr.verify_signals["red_green"]["red_output_tail"]


def test_base_and_head_shas_surface():
    d = verify_view.verify_detail(_pr(_verify("escalate")))
    assert d is not None
    assert d["against_base_sha"] == BASE
    assert d["against_head_sha"] == HEAD
    assert d["tier"] == 1


def test_partial_evidence_is_surfaced_and_demotes_the_level():
    signals = {"blind_adequacy": {"repro_command": "node repro.mjs"},
               "independent_repro": {"ran": True, "exit_code": 20},
               "repro_reason_match": {"matches": False, "applicable": True,
                                      "confidence": "high"}}
    d = verify_view.verify_detail(_pr(_verify(signals=signals)))
    assert d is not None
    assert d["signals_incomplete"] is not None
    assert "corroborate" in d["signals_incomplete"]
    assert d["level"] == "attention"


def test_complete_evidence_carries_no_incomplete_reason():
    d = verify_view.verify_detail(_pr(_verify()))
    assert d is not None
    assert d["signals_incomplete"] is None
    assert d["level"] == "verified"


def test_agent_verified_partial_lane_evidence_demotes_the_level(monkeypatch):
    p = profile.parse_profile(
        {"version": 1, "verify": {"compile_cmd": "c", "build_cmd": "b"}}, "t")
    monkeypatch.setattr(profile, "active", lambda: p)
    signals = {"blind_adequacy": {}}
    d = verify_view.verify_detail(_pr(_verify("agent-verified", signals=signals)))
    assert d is not None
    assert d["signals_incomplete"] is not None
    assert d["level"] == "attention"


def test_four_state_label_collapses_the_outcomes():
    expected = {
        "verified-fix": "Verified",
        "not-verified": "Not verified",
        "needs-rebase": "Not verified",
        "regressed": "Not verified",
        "escalate": "Needs your call",
        "deps-touched": "Needs your call",
        "unverifiable-no-test": "Couldn't run",
        "unverifiable-needs-live-agent": "Couldn't run",
    }
    for outcome, state in expected.items():
        d = verify_view.verify_detail(_pr(_verify(outcome)))
        assert d is not None and d["state"] == state, outcome
    # a null outcome (pending / errored hold) hasn't concluded
    d = verify_view.verify_detail(_pr(_verify(None)))
    assert d is not None and d["state"] == "Couldn't run"


def test_fault_attribution_per_outcome():
    # the operator's #1 question: PR's fault, harness's fault, or a judgment call?
    expected = {
        "verified-fix": None,
        "not-verified": "pr",
        "needs-rebase": "pr",
        "regressed": "pr",
        "deps-touched": "pr",
        "escalate": "judgment",
        "unverifiable-no-test": None,
        "unverifiable-needs-live-agent": None,
    }
    for outcome, fault in expected.items():
        d = verify_view.verify_detail(_pr(_verify(outcome)))
        assert d is not None
        assert d["fault"] == fault, outcome


def test_a_vacuous_filter_not_verified_is_a_harness_fault():
    # #7524 shape: not-verified but the finding says the test command skipped
    # every test — that's the harness, not the PR.
    findings = [{"signal": "vacuous-filter", "note": "-t matched no test"}]
    d = verify_view.verify_detail(_pr(_verify("not-verified", findings=findings)))
    assert d is not None
    assert d["fault"] == "system"


def test_a_vacuous_filter_is_a_harness_fault_from_the_exit_codes_alone():
    # a legacy record with no structured finding, but the exit codes + filtered
    # command still say the whole suite skipped — still the harness's fault
    signals = {"blind_adequacy": {"test_cmd": 'npx vitest run x -t "no match"'},
               "red_green": {"apply_exit": 0, "red_exit": 0, "green_exit": 0}}
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    assert d["fault"] == "system"


def test_request_fault_maps_error_kinds():
    from pipeline import model
    rec = model.Pr(None, {"pr": 1, "meta": {"title": "t", "author": "a",
                          "state": "open", "draft": False, "head_sha": HEAD}})
    for kind, fault in (("sandbox-error", "system"), ("no-base", "system"),
                        ("agent-failed", "system"), ("refused-safety", "pr")):
        rec.rec["verify_request"] = {"status": "error", "error_kind": kind,
                                     "error": "x"}
        v = verify_view.verify_request_view(rec)
        assert v is not None and v["fault"] == fault, kind


def test_empty_signals_carry_no_story_or_cause():
    d = verify_view.verify_detail(_pr(_verify("not-verified")))
    assert d is not None
    assert d["story"] == [] and d["cause"] is None


def test_vacuous_red_names_the_cause_and_fails_the_red_step():
    # The #7524 shape: the test never went red (everything skipped), green is
    # equally vacuous, the independent repro corroborates the defect anyway.
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "npx vitest run x",
                           "repro_command": "npx vitest run repro"},
        "red_green": {"apply_exit": 0, "red_exit": 0, "green_exit": 0},
        "red_reason_match": {"matches": False, "confidence": "high",
                             "reasoning": "no red at all — the file was skipped"},
        "regress": {"ran": False, "skipped_reason": "red-green-not-clean"},
        "independent_repro": {"ran": True, "exit_code": 20},
        "repro_reason_match": {"matches": True, "confidence": "high",
                               "reasoning": "matches the prediction"},
    }
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    assert d["cause"] is not None
    assert "never failed on unfixed main" in d["cause"]
    by_key = {s["key"]: s for s in d["story"]}
    assert set(by_key) == {"blind", "apply", "red", "green", "regress", "repro"}
    assert by_key["red"]["result"] == "fail"
    assert "did NOT fail" in by_key["red"]["note"]
    assert by_key["red"]["reasoning"] == "no red at all — the file was skipped"
    # a vacuous green never presents as a pass
    assert by_key["green"]["result"] == "info"
    assert by_key["regress"]["result"] == "skip"
    assert by_key["repro"]["result"] == "pass"


def test_vacuous_red_with_a_name_filter_names_the_filter():
    # The #7524 shape, precisely: the test command's -t filter matched no test
    # name, so vitest skipped every test and exited 0 on the unfixed code.
    signals = {
        "blind_adequacy": {"faithful": True,
                           "test_cmd": 'npx vitest run x.test.ts -t "release preserves status"'},
        "red_green": {"apply_exit": 0, "red_exit": 0, "green_exit": 0},
        "red_reason_match": {"matches": False, "confidence": "high",
                             "reasoning": "all tests skipped"},
    }
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    assert d["cause"] is not None
    assert "name filter (-t)" in d["cause"]
    assert "harness defect" in d["cause"]
    red = next(s for s in d["story"] if s["key"] == "red")
    assert red["result"] == "fail"
    assert "name filter (-t)" in red["note"]
    assert "did NOT fail" in red["note"]


def test_vacuous_repro_path_filter_warns_even_when_the_judge_rated_it_matching():
    # The #9041 shape: --config rebased the runner's root, the path filter kept
    # the repo-root prefix, so the repro found no files and exited nonzero. The
    # deterministic flag outranks the judge's rating — a fooled judge must not
    # present the exit as corroboration.
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "npx vitest run x",
                           "repro_command": ("npx vitest run --config "
                                             "server/vitest.config.ts "
                                             "server/src/__tests__/config-file.test.ts")},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0},
        "independent_repro": {"ran": True, "exit_code": 20},
        "repro_reason_match": {"matches": True, "confidence": "high",
                               "reasoning": "output looked like the prediction"},
    }
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    repro = next(s for s in d["story"] if s["key"] == "repro")
    assert repro["result"] == "warn"
    assert "server/src/__tests__/config-file.test.ts" in repro["note"]
    assert "harness defect" in repro["note"]


def test_a_skipped_repro_names_its_skip_reason():
    for reason, needle in (
            ("repro-targets-pr-test", "test the PR itself introduces"),
            ("host-path-in-command", "host path")):
        signals = {
            "blind_adequacy": {"faithful": True, "test_cmd": "t",
                               "repro_command": "npx vitest run x"},
            "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0},
            "independent_repro": {"ran": False, "exit_code": None,
                                  "skipped_reason": reason},
        }
        d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
        assert d is not None
        repro = next(s for s in d["story"] if s["key"] == "repro")
        assert repro["result"] == "warn", reason
        assert needle in repro["note"], reason


def test_a_rejected_repro_renders_its_reason_in_the_story():
    # The blind lane rejected the repro pre-run: repro_command is null, and
    # blind_adequacy.repro_rejected carries the reason. The story still shows a
    # repro step so the operator sees the rejection instead of nothing.
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t",
                           "repro_command": None,
                           "repro_rejected": "repro_command targets a test the "
                                             "PR itself introduces "
                                             "('src/x.test.ts')"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                      "red_exit_confirm": 20, "green_exit_confirm": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("verified-fix", signals=signals)))
    assert d is not None
    repro = next(s for s in d["story"] if s["key"] == "repro")
    assert repro["result"] == "warn"
    assert "rejected pre-run" in repro["note"]
    assert "src/x.test.ts" in repro["note"]


def test_confirm_step_pass_when_the_re_run_agrees():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                      "red_exit_confirm": 20, "green_exit_confirm": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("verified-fix", signals=signals)))
    assert d is not None
    confirm = next((s for s in d["story"] if s["key"] == "confirm"), None)
    assert confirm is not None and confirm["result"] == "pass"


def test_confirm_step_warns_on_a_flaky_red():
    # first red→green clean, confirm red passed → flaky (escalate)
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                      "red_exit_confirm": 0, "green_exit_confirm": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("escalate", signals=signals)))
    assert d is not None
    confirm = next(s for s in d["story"] if s["key"] == "confirm")
    assert confirm["result"] == "warn" and "flaky" in confirm["note"].lower()


def test_no_confirm_step_when_the_first_run_was_not_clean():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 0, "green_exit": 0,
                      "red_exit_confirm": None, "green_exit_confirm": None},
        "red_reason_match": {"matches": False, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    assert not any(s["key"] == "confirm" for s in d["story"])


def test_wrong_reason_red_names_the_cause():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0},
        "red_reason_match": {"matches": False, "confidence": "high",
                             "reasoning": "failed on an import error"},
    }
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    assert d["cause"] is not None and "not for the predicted reason" in d["cause"]
    red = next(s for s in d["story"] if s["key"] == "red")
    assert red["result"] == "fail" and "WRONG reason" in red["note"]


def test_still_failing_green_names_the_cause():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 20},
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("not-verified", signals=signals)))
    assert d is not None
    assert d["cause"] is not None and "still failed" in d["cause"]
    green = next(s for s in d["story"] if s["key"] == "green")
    assert green["result"] == "fail"


def _contained_red_green() -> dict:
    # The #3718 shape: both legs exit 20, the parsed sets show green failing
    # only on a test that also failed red, confirmed on the second run.
    return {"apply_exit": 0, "red_exit": 20, "green_exit": 20,
            "red_exit_confirm": 20, "green_exit_confirm": 20,
            "red_failing": ["a.test.ts > s > target", "a.test.ts > s > contam"],
            "green_failing": ["a.test.ts > s > contam"],
            "green_failing_confirm": ["a.test.ts > s > contam"],
            "failing_in_diff": []}


def test_a_contained_dirty_green_narrates_the_contamination():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": _contained_red_green(),
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("verified-fix", signals=signals)))
    assert d is not None
    green = next(s for s in d["story"] if s["key"] == "green")
    assert green["result"] == "warn"
    assert "also failed WITHOUT the fix" in green["note"]
    assert "a.test.ts > s > contam" in green["note"]
    confirm = next(s for s in d["story"] if s["key"] == "confirm")
    assert confirm["result"] == "pass"
    assert "contamination" in confirm["note"]


def test_a_disagreeing_contained_confirm_names_the_cause():
    rg = _contained_red_green()
    rg["green_failing_confirm"] = ["a.test.ts > s > brand-new-failure"]
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": rg,
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("escalate", signals=signals)))
    assert d is not None
    assert d["cause"] is not None and "confirm re-run disagreed" in d["cause"]


def test_needs_rebase_story_stops_at_apply():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 30},
    }
    d = verify_view.verify_detail(_pr(_verify("needs-rebase", signals=signals)))
    assert d is not None
    assert d["cause"] is not None and "rebase" in d["cause"]
    assert [s["key"] for s in d["story"]] == ["blind", "apply"]
    assert d["story"][-1]["result"] == "fail"


def test_escalate_cause_names_the_disagreement():
    signals = {
        "blind_adequacy": {"faithful": False, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("escalate", signals=signals)))
    assert d is not None
    assert d["cause"] is not None and "disagree" in d["cause"]
    blind = next(s for s in d["story"] if s["key"] == "blind")
    assert blind["result"] == "fail" and "NOT faithful" in blind["note"]


def test_verified_fix_story_all_green_and_no_cause():
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
        "regress": {"ran": True, "exit_first": 0},
    }
    d = verify_view.verify_detail(_pr(_verify("verified-fix", signals=signals)))
    assert d is not None
    assert d["cause"] is None
    assert all(s["result"] == "pass" for s in d["story"])


def test_unverifiable_story_is_a_single_info_step():
    signals = {"blind_adequacy": {"faithful": None, "test_cmd": None}}
    d = verify_view.verify_detail(_pr(_verify("unverifiable-no-test",
                                              signals=signals)))
    assert d is not None
    assert d["cause"] is None
    assert len(d["story"]) == 1 and d["story"][0]["result"] == "info"


class TestAgentVerifiedView:
    def test_agent_verified_copy_and_state(self):
        signals = {
            "blind_adequacy": {"has_test": False, "faithful": False, "test_cmd": None,
                               "requires_live_agent": False, "reasoning": "no test"},
            "red_green": {"apply_exit": 0, "red_exit": None, "green_exit": None},
            "authored_test": {"attempted": True, "can_author": True,
                              "test_cmd": "npx vitest run x.test.tsx",
                              "files": [{"path": "x.test.tsx", "contents": "t\n"}],
                              "expected_red_signature": "boom", "confidence": "high",
                              "reasoning": "r", "red_exit": 20, "green_exit": 0,
                              "red_exit_confirm": 20, "green_exit_confirm": 0,
                              "red_output_tail": "boom", "green_output_tail": "ok"},
            "red_reason_match": {"matches": True, "confidence": "high", "reasoning": "j"},
            "regress": {"ran": True, "exit_first": 0, "confirmed": False, "flake": False},
        }
        rec = _pr(_verify("agent-verified", signals=signals))
        v = verify_view.verify_detail(rec)
        assert v is not None
        assert v["level"] == "verified" and v["state"] == "Verified"
        assert "agent-authored" in v["headline"].lower() or "agent-authored" in v["detail"]
        assert v["fault"] is None

    def test_no_test_with_attempt_story_and_cause(self):
        signals = {
            "blind_adequacy": {"has_test": False, "faithful": False, "test_cmd": None,
                               "requires_live_agent": False, "reasoning": "no test"},
            "red_green": {"apply_exit": 0, "red_exit": None, "green_exit": None},
            "authored_test": {"attempted": True, "can_author": True,
                              "test_cmd": "npx vitest run x.test.tsx",
                              "files": [{"path": "x.test.tsx", "contents": "t\n"}],
                              "expected_red_signature": "boom", "confidence": "high",
                              "reasoning": "r", "red_exit": 20, "green_exit": None,
                              "red_exit_confirm": None, "green_exit_confirm": None,
                              "red_output_tail": "boom", "green_output_tail": None},
            "red_reason_match": {"matches": True, "confidence": "high", "reasoning": "j"},
            "regress": {},
        }
        rec = _pr(_verify("unverifiable-no-test", signals=signals))
        v = verify_view.verify_detail(rec)
        assert v is not None
        keys = [s["key"] for s in v["story"]]
        assert "author" in keys and "author-red" in keys
        assert v["cause"] is not None and "agent-authored" in v["cause"]

    def test_no_test_without_attempt_is_unchanged(self):
        signals = {"blind_adequacy": {"faithful": False, "test_cmd": None}}
        rec = _pr(_verify("unverifiable-no-test", signals=signals))
        v = verify_view.verify_detail(rec)
        assert v is not None
        assert [s["key"] for s in v["story"]] == ["blind"]
        assert v["cause"] is None


def test_lane_steps_appear_in_the_story():
    signals = {
        "blind_adequacy": {"test_cmd": "npx vitest run x", "faithful": True},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                      "red_exit_confirm": 20, "green_exit_confirm": 0},
        "lanes": {"compile": {"cmd": "pnpm -r typecheck", "exit": 20,
                              "ok": False, "duration_s": 100.0,
                              "error_excerpt": "error TS2739: x"},
                  "build": {"cmd": "pnpm build", "skipped": "compile failed"}},
        "regress": {"ran": False, "skipped_reason": "lane-compile-failed"},
    }
    d = verify_view.verify_detail(_pr(_verify("regressed", signals=signals)))
    assert d is not None
    keys = [s["key"] for s in d["story"]]
    assert "lane-compile" in keys and "lane-build" in keys
    compile_step = next(s for s in d["story"] if s["key"] == "lane-compile")
    assert compile_step["result"] == "fail"
    assert "error TS2739" in compile_step["note"]
    assert "compile lane" in (d["cause"] or "")


def test_escalate_lane_infra_error_is_a_system_fault_and_names_the_lane():
    # A lane that hit an infrastructure exit (not one of the two sentinels the
    # gate recognizes) is a harness artifact, not a judgment call — the fault
    # must read "system" and the cause must name the lane.
    signals = {
        "blind_adequacy": {"faithful": True, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                      "red_exit_confirm": 20, "green_exit_confirm": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
        "lanes": {"build": {"cmd": "pnpm build", "exit": 137, "ok": False}},
    }
    d = verify_view.verify_detail(_pr(_verify("escalate", signals=signals)))
    assert d is not None
    assert d["fault"] == "system"
    assert d["cause"] is not None and "build" in d["cause"]


def test_escalate_without_lanes_stays_a_judgment_fault():
    # The blind-unfaithful-vs-clean-red->green shape: nothing about a lane is
    # wrong here, so the fault stays the operator's judgment call.
    signals = {
        "blind_adequacy": {"faithful": False, "test_cmd": "t"},
        "red_green": {"apply_exit": 0, "red_exit": 20, "green_exit": 0},
        "red_reason_match": {"matches": True, "confidence": "high"},
    }
    d = verify_view.verify_detail(_pr(_verify("escalate", signals=signals)))
    assert d is not None
    assert d["fault"] == "judgment"
