"""Operator-facing view of a PR's VERIFY record (the dynamic-verification phase).

Renders the stored `verify` section for the app: per-outcome headline copy,
a display tone, freshness (gates.VERIFY_MAX_AGE_DAYS — the merge-recency
window), and the signals passed through with ANSI escape sequences stripped
from the captured output tails — including `authored_test`, the agent-authored
test lane that runs when the PR ships none of its own. Read-only and
policy-free: the outcome and its freshness come verbatim from the store and
freshness.py — gates.py is the one policy module, and nothing here recomputes,
softens, or overrides it.

Two derived display fields explain the verbatim outcome in plain English:
`cause` names the specific check that broke ("the test never failed on unfixed
main"), and `story` walks the run's checks in order, each with a pass/fail and
a one-line note. Both read the same trusted inputs the gate read — host exit
codes, the pre-committed blind verdict, and the judge's stored ratings, never
the untrusted output tails — and neither feeds back into policy.

The `*_output_tail` fields are the PR's own captured test output (capped at
8 KiB by the driver): untrusted, attacker-influenced text. This module passes
them through as inert strings for the UI to display under an "untrusted" label;
nothing may derive a verdict from them.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypedDict

from pipeline import freshness
from pipeline import gates

if TYPE_CHECKING:
    from pipeline.model import Pr

# CSI / OSC / two-byte ESC sequences — vitest colors its output and the sandbox
# captures the raw stream, so the tails arrive full of terminal control codes.
_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

# Fields inside a signal dict that carry captured container output.
_TAIL_FIELDS = ("red_output_tail", "green_output_tail", "output_tail")


class VerifyRequestView(TypedDict):
    status: str
    source: str | None
    step: str | None
    queued_at: str | None
    started_at: str | None
    finished_at: str | None
    error_kind: str | None
    error: str | None
    log_tail: str | None
    checked_at: str | None
    fault: str | None


# The operator's #1 trust question: when verification doesn't end in a green,
# is it the PR's fault or the harness's? `fault` answers it as a first-class
# field on every surface — "pr" (the PR is why: no repro, needs rebase,
# regressed, deps-touched), "system" (the harness/infra broke: an errored run,
# a hold, an unhealthy sandbox — NOT the PR's fault), "judgment" (the signals
# disagree; a human must decide), or None (verified, or nothing wrong yet).

# The error_kind of a failed verify_request → its fault. Every kind but the
# safety refusal is the harness/infra; refused-safety is a property of the PR
# (closed, malicious, or deps-touching) that stopped the run.
_REQUEST_FAULT: dict[str, str] = {
    "no-base": "system", "fetch-error": "system", "sandbox-error": "system",
    "agent-failed": "system", "interrupted": "system", "exception": "system",
    "hold": "system",
    "refused-safety": "pr",
}

# A committed outcome → its fault. verified-fix, agent-verified, and the
# unverifiable outcomes carry none (nothing went wrong — the PR just has no
# test to run, or needs a live agent). A null outcome (pending / held) carries
# none here; a hold's system-fault shows on the request strip, not the record.
_OUTCOME_FAULT: dict[str, str] = {
    "not-verified": "pr", "needs-rebase": "pr", "regressed": "pr",
    "deps-touched": "pr", "escalate": "judgment",
}


def _request_fault(error_kind: str | None) -> str | None:
    return _REQUEST_FAULT.get(error_kind) if error_kind else None


# The eight verify outcomes collapse to FOUR operator-facing states for the lead
# label — the raw outcome stays available as a chip/tooltip for anyone who wants
# it. This is what makes the panel glanceable: "what happened" in one of four
# words, not a vocabulary of eight.
_STATE_LABEL: dict[str, str] = {
    "verified-fix": "Verified",
    "agent-verified": "Verified",
    "not-verified": "Not verified", "needs-rebase": "Not verified",
    "regressed": "Not verified",
    "escalate": "Needs your call", "deps-touched": "Needs your call",
    "unverifiable-no-test": "Couldn't run",
    "unverifiable-needs-live-agent": "Couldn't run",
}


def _state_label(outcome: str | None) -> str:
    """The four-state operator label for an outcome — 'Verified' / 'Not verified'
    / 'Needs your call' / 'Couldn't run'. A null outcome (blind committed, not
    yet judged, or an errored hold) hasn't run to a conclusion."""
    if not outcome:
        return "Couldn't run"
    return _STATE_LABEL.get(outcome, "Couldn't run")


def _outcome_fault(outcome: str | None, signals: dict, findings: list[dict]) -> str | None:
    """The fault a committed outcome attributes. A not-verified whose red never
    ran because the test command's name filter matched nothing is a harness
    artifact (the whole suite skipped), not the PR's fault — so it reads as
    system, whether that shows as a structured finding or only in the exit
    codes (a record from before the finding existed). An escalate whose lane
    evidence names an infrastructure error is the same kind of harness
    artifact, not a judgment call."""
    if outcome == "not-verified":
        vacuous = any(f.get("signal") == "vacuous-filter" for f in findings) or (
            gates.vacuous_name_filter(signals.get("blind_adequacy") or {},
                                      signals.get("red_green") or {}) is not None)
        if vacuous:
            return "system"
    if outcome == "escalate" and gates._lane_escalate_cause(signals) is not None:
        return "system"
    return _OUTCOME_FAULT.get(outcome) if outcome else None


class VerifyStep(TypedDict):
    """One check in the run's plain-English storyline. `result` is a display
    tone: "pass" | "fail" | "warn" | "skip" | "info". `reasoning` carries the
    judge's explanation when this step is the one it explains."""
    key: str
    label: str
    result: str
    note: str
    reasoning: str | None


class VerifyDetail(TypedDict):
    outcome: str | None
    tier: int | None
    checked_at: str | None
    against_base_sha: str | None
    against_head_sha: str | None
    stale_reason: str | None
    signals_incomplete: str | None
    level: str
    state: str
    headline: str
    detail: str
    fault: str | None
    cause: str | None
    story: list[VerifyStep]
    signals: dict[str, dict]
    findings: list[dict]


def _strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def _clean_signals(signals: dict) -> dict[str, dict]:
    """The signals with ANSI escapes stripped from every captured output tail.
    Copies the dicts it touches — the store record is never mutated."""
    out: dict[str, dict] = {}
    for name, sig in signals.items():
        if not isinstance(sig, dict):
            continue
        cleaned = dict(sig)
        for field in _TAIL_FIELDS:
            v = cleaned.get(field)
            if isinstance(v, str):
                cleaned[field] = _strip_ansi(v)
        out[name] = cleaned
    return out


# Per-outcome operator copy: (level, headline, detail). `level` is a display
# tone only — "verified" / "attention" / "blocked" / "info" / "pending".
_OUTCOME_COPY: dict[str, tuple[str, str, str]] = {
    "verified-fix": (
        "verified",
        "Verified — the fix demonstrably resolves the claimed bug",
        "The PR's test failed on pinned main for the predicted reason and passed "
        "with the fix applied, in the isolated sandbox."),
    "agent-verified": (
        "verified",
        "Agent-verified — an agent-authored test corroborates the fix",
        "The PR ships no test, so the harness authored one. It failed on pinned "
        "main for the predicted reason and passed with the fix applied — twice, "
        "in fresh containers, in the isolated sandbox. Corroborating evidence "
        "for your call: it does not count toward the auto-merge bar, which "
        "requires an author-shipped test."),
    "escalate": (
        "attention",
        "Escalated — the signals don't line up",
        "One of four things: the blind reviewer judged the test unfaithful yet it "
        "went cleanly red→green; the red-reason match is itself unsure; the "
        "red→green did not reproduce on the confirm re-run (a flaky, "
        "nondeterministic result); or a merge-gate lane hit an infrastructure "
        "error and reached no verdict. The phase never resolves this — the "
        "operator is the resolution. Read the blind reasoning, the red output, "
        "and any lane detail, and decide."),
    "not-verified": (
        "blocked",
        "Not verified — the sandbox run did not confirm the fix",
        "The test did not go cleanly red→green, or it failed on main for a reason "
        "that does not match the claimed symptom. A red for the wrong reason is "
        "not a reproduction; see the judge's reasoning below."),
    "needs-rebase": (
        "blocked",
        "Needs rebase — the patch does not apply to pinned main",
        "The PR's diff no longer applies cleanly (host-observed apply failure). "
        "The author must rebase before verification can run."),
    "deps-touched": (
        "blocked",
        "Refused — the PR touches dependency manifests",
        "It changes package.json / pnpm-lock.yaml / pnpm-workspace.yaml, and the "
        "sandbox never installs PR-controlled dependencies. Routed to needs-human."),
    "unverifiable-no-test": (
        "info",
        "Unverifiable — no test and no reproduction to run",
        "The PR ships no test and no linked-issue repro, so there is nothing to go "
        "red→green. It fails the auto-merge bar but does not block a human merge."),
    "unverifiable-needs-live-agent": (
        "info",
        "Unverifiable here — reproducing needs a live agent run",
        "The blind pass judged this bug reproducible only with a live model agent "
        "(Tier 2), which this sandbox does not run. It fails the auto-merge bar "
        "but does not block a human merge."),
    "regressed": (
        "blocked",
        "Regressed — the merged tree gets worse with this PR applied",
        "The fix itself works, but with the PR applied a full-suite test or a "
        "merge-gate lane (compile/build) that passes on pinned main fails. The "
        "specifics are in the story and findings below."),
}

_PENDING_COPY = (
    "pending",
    "Verification in progress — blind verdict committed, no conclusion yet",
    "The blind adequacy verdict below was committed before any sandbox run — the "
    "phase's integrity property. No outcome exists yet (the run is pending, or it "
    "errored and will re-queue). This fails the auto-merge bar and blocks nothing "
    "else.")


def _step(key: str, label: str, result: str, note: str,
          reasoning: str | None = None) -> VerifyStep:
    return {"key": key, "label": label, "result": result, "note": note,
            "reasoning": reasoning}


def _red_step(blind: dict, rg: dict, judge: dict) -> VerifyStep:
    """The red-run check in plain English: the PR's test on unfixed main must
    fail (exit 20), and the judge must confirm it failed for the predicted
    reason. This is the check that proves the bug exists."""
    label = ("Run the PR's test WITHOUT the fix — it must fail, for the "
             "predicted reason (this is what proves the bug is real)")
    exit_ = rg.get("red_exit")
    reasoning = judge.get("reasoning")
    if exit_ == gates.SENTINEL_PASS:
        flag = gates.vacuous_name_filter(blind, rg)
        if flag is not None:
            return _step("red", label, "fail",
                         "The test did NOT fail — and the test command carries a "
                         f"name filter ({flag}). A filter that matches no test "
                         "title skips every test and exits 0, so the likeliest "
                         "cause is that nothing ran at all: a defect in the test "
                         "command, not evidence about the PR. The judge's read of "
                         "the output is below.", reasoning)
        return _step("red", label, "fail",
                     "The test did NOT fail — it exited as passing on the unfixed "
                     "code, so it reproduced nothing. Typical causes: every test in "
                     "the file was skipped, the test command's name filter matched "
                     "no tests, or the test simply does not catch the bug. The "
                     "judge's read of the output is below.", reasoning)
    if exit_ != gates.SENTINEL_TEST_FAIL:
        if exit_ is None:
            return _step("red", label, "skip", "Never ran.")
        return _step("red", label, "fail",
                     f"Infrastructure error (exit {exit_}) — not a verdict; "
                     "the run re-queues.")
    matches = judge.get("matches")
    if matches is False:
        return _step("red", label, "fail",
                     "The test failed, but for the WRONG reason — a red for the "
                     "wrong reason is not a reproduction.", reasoning)
    if matches is True:
        if judge.get("confidence") == "low":
            return _step("red", label, "warn",
                         "The test failed and the judge rated the reason matching, "
                         "but at low confidence — this escalates to a human.",
                         reasoning)
        return _step("red", label, "pass",
                     "The test failed as required, and the judge confirmed the "
                     "failure matches the pre-committed prediction.", reasoning)
    return _step("red", label, "warn",
                 "The test failed, but the judge never rated whether the reason "
                 "matches — the run holds and re-runs.", reasoning)


def _green_step(rg: dict) -> VerifyStep:
    label = "Run the test again WITH the fix applied — it must pass"
    exit_ = rg.get("green_exit")
    red_ok = rg.get("red_exit") == gates.SENTINEL_TEST_FAIL
    if exit_ == gates.SENTINEL_PASS:
        if red_ok:
            return _step("green", label, "pass", "The test passed with the fix.")
        return _step("green", label, "info",
                     "Exited as passing — but with no real failure in the step "
                     "above, this proves nothing.")
    if exit_ == gates.SENTINEL_TEST_FAIL:
        contaminated = gates.contained_green_failures(rg)
        if contaminated:
            return _step("green", label, "warn",
                         "Exited failing, but every failure is a test that also "
                         "failed WITHOUT the fix — contamination in the test "
                         "file, not evidence against the PR. The target test "
                         "itself flipped red→green. Waved through: "
                         + "; ".join(contaminated))
        return _step("green", label, "fail",
                     "The test STILL failed with the fix applied.")
    if exit_ is None:
        return _step("green", label, "skip", "Never ran.")
    return _step("green", label, "fail",
                 f"Infrastructure error (exit {exit_}) — not a verdict; the run "
                 "re-queues.")


def _confirm_step(rg: dict) -> VerifyStep | None:
    """The confirm re-run check, or None when no confirm ran (the first red→green
    was not clean). A verified-fix must reproduce on the second run too; a
    disagreement is a flaky (nondeterministic) reproduction that escalates."""
    red2 = rg.get("red_exit_confirm")
    green2 = rg.get("green_exit_confirm")
    if red2 is None and green2 is None:
        return None
    label = "Confirm re-run — the red→green must reproduce a second time (rules out a flake)"
    if red2 == gates.SENTINEL_TEST_FAIL and green2 == gates.SENTINEL_PASS:
        return _step("confirm", label, "pass",
                     "The second run reproduced the same red→green — not a flake.")
    if (red2 == gates.SENTINEL_TEST_FAIL and green2 == gates.SENTINEL_TEST_FAIL
            and gates.green_confirm_accepted(rg)):
        return _step("confirm", label, "pass",
                     "The second run reproduced the same red→green, green again "
                     "failing only on the contamination that also fails red.")
    if red2 == gates.SENTINEL_PASS:
        return _step("confirm", label, "warn",
                     "On the confirm run the test did NOT fail without the fix — the "
                     "reproduction is flaky (nondeterministic), so it can't stand as "
                     "a verified fix. Escalated for a human.")
    if green2 == gates.SENTINEL_TEST_FAIL:
        return _step("confirm", label, "warn",
                     "On the confirm run the test failed even WITH the fix — the "
                     "result is flaky (nondeterministic). Escalated for a human.")
    return _step("confirm", label, "skip",
                 f"Did not complete cleanly (red {red2}, green {green2}) — the run "
                 "holds and re-runs.")


def _regress_step(regress: dict) -> VerifyStep:
    label = "Full-suite regression check — the rest of the tests must not break"
    if not regress.get("ran"):
        reason = regress.get("skipped_reason")
        note = ("Skipped — the red→green above was not clean, so there was "
                "nothing to regression-check."
                if reason == "red-green-not-clean"
                else f"Skipped ({reason})." if reason else "Never ran.")
        return _step("regress", label, "skip", note)
    if regress.get("confirmed"):
        return _step("regress", label, "fail",
                     "With this PR applied, tests that pass on pinned main fail — "
                     "confirmed on a second run. The failing tests are in the "
                     "findings below.")
    if regress.get("exit_first") == gates.SENTINEL_TEST_FAIL:
        return _step("regress", label, "warn",
                     "A failure appeared once but did not confirm on the second "
                     "run — treated as flaky, not a regression.")
    return _step("regress", label, "pass", "No regressions.")


_LANE_LABELS: dict[str, str] = {
    "compile": "Compile lane — the whole repo must typecheck with the PR applied",
    "build": "Build lane — the whole repo must build with the PR applied",
}


def _lane_step(name: str, entry: dict) -> VerifyStep:
    """One merge-gate lane's check: the profile's whole-repo command over the
    patched tree, host-observed. A failed lane reads as `regressed`; a
    non-sentinel exit escalates."""
    label = _LANE_LABELS.get(
        name, f"{name} lane — the profile's {name} command must pass with the PR applied")
    key = f"lane-{name}"
    if "exit" not in entry:
        return _step(key, label, "skip",
                     f"Skipped — {entry.get('skipped', 'not reached')}.")
    exit_ = entry.get("exit")
    if exit_ == gates.SENTINEL_PASS:
        dur = entry.get("duration_s")
        return _step(key, label, "pass",
                     f"`{entry.get('cmd')}` passed"
                     + (f" ({dur:g}s)." if isinstance(dur, (int, float)) else "."))
    if exit_ == gates.SENTINEL_TEST_FAIL:
        note = (f"`{entry.get('cmd')}` failed against the pinned base with this "
                f"PR applied")
        excerpt = entry.get("error_excerpt")
        return _step(key, label, "fail",
                     f"{note}: {excerpt}" if excerpt else f"{note}.")
    return _step(key, label, "fail",
                 f"Infrastructure error (exit {exit_}) — not a verdict; "
                 "escalated for a human.")


def _lane_steps(signals: dict) -> list[VerifyStep]:
    return [_lane_step(name, entry)
            for name, entry in (signals.get("lanes") or {}).items()
            if isinstance(entry, dict)]


def _repro_step(blind: dict, repro: dict, rating: dict) -> VerifyStep | None:
    """The independent-repro check, or None when the blind pass authored no
    repro. Corroborating evidence only — the outcome never turns on it. A
    repro the driver rejected pre-run (blind.repro_rejected — the command
    targeted a test the PR itself introduces, and a rejection retry did not
    produce a valid one) renders as its own warn step."""
    label = ("Independent reproduction by the agent (corroborating only — "
             "never changes the outcome)")
    if not blind.get("repro_command"):
        rejected = blind.get("repro_rejected")
        if not rejected:
            return None
        return _step("repro", label, "warn",
                     f"Independent repro rejected pre-run: {rejected}",
                     blind.get("reasoning"))
    reasoning = rating.get("reasoning")
    if not repro.get("ran"):
        skip = repro.get("skipped_reason")
        if skip == "repro-targets-pr-test":
            return _step("repro", label, "warn",
                         "The repro was skipped without running: its command "
                         "targets a test the PR itself introduces, and the "
                         "repro phase runs the pinned base with no patch "
                         "applied — such a target cannot exist there, so its "
                         "exit would carry no signal.", reasoning)
        if skip == "host-path-in-command":
            return _step("repro", label, "warn",
                         "The repro was skipped without running: its command "
                         "names a host path that does not exist inside the "
                         "container, so its exit would carry no signal.",
                         reasoning)
        return _step("repro", label, "warn",
                     "A repro was authored but never ran — the evidence is "
                     "partial.", reasoning)
    flag = gates.misrooted_repro_config(blind, repro)
    if flag is not None:
        # Deterministic, so it outranks the judge's rating: a fooled judge must
        # not present the exit as corroboration.
        return _step("repro", label, "warn",
                     "The repro exited as failing, but its command points "
                     f"--config at a subdirectory config ({flag}) it never "
                     "makes the runner's root — the runner keeps its root at "
                     "the working directory, so the paths that config declares "
                     "relative to itself resolve against the repo root and the "
                     "suite dies at load having run zero tests. A harness "
                     "defect in the command, not corroboration of the bug.",
                     reasoning)
    nflag = gates.vacuous_repro_name_filter(blind, repro)
    if nflag is not None:
        # Same precedence as the misrooted config above: deterministic, so it
        # outranks the judge's reading of an exit that evaluated no assertion.
        return _step("repro", label, "warn",
                     "The repro exited 0, but its command carries a name filter "
                     f"({nflag!r}) — a filter that matches no test title skips "
                     "every test and exits 0, so the likeliest cause is that "
                     "nothing ran at all rather than that the defect failed to "
                     "reproduce. A harness defect in the command, not evidence "
                     "about the PR.", reasoning)
    matches = rating.get("matches")
    if matches is True:
        return _step("repro", label, "pass",
                     "The agent's own repro DID hit the bug on unfixed main, "
                     "exactly as predicted — the defect itself is real.", reasoning)
    if matches is False:
        return _step("repro", label, "warn",
                     "The repro ran but failed for a reason that does not match "
                     "the prediction — it corroborates nothing.", reasoning)
    return _step("repro", label, "warn",
                 "The repro ran but was never rated by the judge.", reasoning)


def _authored_steps(authored: dict, signals: dict) -> list[VerifyStep]:
    """The agent-authored lane's checks in execution order — corroborating
    evidence only; no step here is a policy gate."""
    steps: list[VerifyStep] = []
    author_label = ("Author a test for the claimed defect (agent-authored — "
                    "corroborating only, never an auto-merge signal)")
    skip = authored.get("skipped_reason")
    if skip:
        steps.append(_step("author", author_label, "warn",
                           f"The authoring pass produced no runnable test ({skip}).",
                           authored.get("reasoning")))
        return steps
    files = ", ".join(f.get("path", "?") for f in authored.get("files") or [])
    steps.append(_step("author", author_label, "pass",
                       f"The agent authored {files}; the driver derived the run "
                       "command from the path(s) — committed before any run.",
                       authored.get("reasoning")))

    rg = signals.get("red_green") or {}
    apply_label = "Apply the PR's diff to a pinned copy of main"
    apply_exit = rg.get("apply_exit")
    if apply_exit == gates.SENTINEL_PASS:
        steps.append(_step("apply", apply_label, "pass", "Applied cleanly."))
    elif apply_exit == gates.SENTINEL_PATCH_CONFLICT:
        steps.append(_step("apply", apply_label, "fail",
                           "The diff does not apply — the author must rebase."))
        return steps
    else:
        steps.append(_step("apply", apply_label, "skip", "Never ran."))
        return steps

    judge = signals.get("red_reason_match") or {}
    red_label = ("Run the authored test WITHOUT the fix — it must fail, for the "
                 "predicted reason")
    red = authored.get("red_exit")
    if red is None:
        steps.append(_step("author-red", red_label, "skip", "Never ran."))
        return steps
    if red == gates.SENTINEL_PASS:
        steps.append(_step("author-red", red_label, "fail",
                           "The authored test did NOT fail on the unfixed base — "
                           "it reproduces nothing, so it corroborates nothing.",
                           judge.get("reasoning")))
        return steps
    if red != gates.SENTINEL_TEST_FAIL:
        steps.append(_step("author-red", red_label, "fail",
                           f"Infrastructure error (exit {red}) — not a verdict. "
                           "Re-queue to retry the lane."))
        return steps
    matches = judge.get("matches")
    if matches is False:
        steps.append(_step("author-red", red_label, "fail",
                           "The authored test failed, but for the WRONG reason — "
                           "not a reproduction.", judge.get("reasoning")))
    elif matches is True and judge.get("confidence") == "low":
        steps.append(_step("author-red", red_label, "warn",
                           "Failed as required, but the judge's reason rating is "
                           "low-confidence — recorded, not corroborating.",
                           judge.get("reasoning")))
    elif matches is True:
        steps.append(_step("author-red", red_label, "pass",
                           "Failed as required, and the judge confirmed the failure "
                           "matches the pre-committed prediction.",
                           judge.get("reasoning")))
    else:
        steps.append(_step("author-red", red_label, "warn",
                           "Failed, but the judge never rated the reason.",
                           judge.get("reasoning")))

    green_label = "Run the authored test again WITH the fix applied — it must pass"
    green = authored.get("green_exit")
    if green == gates.SENTINEL_PASS:
        steps.append(_step("author-green", green_label, "pass",
                           "The authored test passed with the fix."))
    elif green == gates.SENTINEL_TEST_FAIL:
        steps.append(_step("author-green", green_label, "fail",
                           "The authored test STILL failed with the fix applied."))
        return steps
    elif green is None:
        steps.append(_step("author-green", green_label, "skip", "Never ran."))
        return steps
    else:
        steps.append(_step("author-green", green_label, "fail",
                           f"Infrastructure error (exit {green}) — not a verdict."))
        return steps

    red2 = authored.get("red_exit_confirm")
    green2 = authored.get("green_exit_confirm")
    if red2 is not None or green2 is not None:
        confirm_label = ("Confirm re-run — the red→green must reproduce a second "
                         "time (rules out a flake)")
        if red2 == gates.SENTINEL_TEST_FAIL and green2 == gates.SENTINEL_PASS:
            steps.append(_step("author-confirm", confirm_label, "pass",
                               "The second run reproduced the same red→green."))
        else:
            steps.append(_step("author-confirm", confirm_label, "warn",
                               f"The confirm re-run disagreed (red {red2}, green "
                               f"{green2}) — a flaky reproduction corroborates "
                               "nothing."))
            return steps

    steps.extend(_lane_steps(signals))
    regress = signals.get("regress") or {}
    if regress:
        steps.append(_regress_step(regress))
    return steps


def _build_story(signals: dict) -> list[VerifyStep]:
    """The run's checks in execution order, each with a plain-English verdict.
    Mirrors the checks gates.verify_outcome gates on — for explanation only;
    the outcome itself is always the stored one. Empty when the record carries
    no signals (nothing to narrate)."""
    if not signals:
        return []
    blind = signals.get("blind_adequacy") or {}
    rg = signals.get("red_green") or {}
    judge = signals.get("red_reason_match") or {}
    regress = signals.get("regress") or {}
    repro = signals.get("independent_repro") or {}
    rating = signals.get("repro_reason_match") or {}

    blind_label = ("Pre-run review: is the PR's test a faithful reproduction "
                   "of the claimed bug?")
    if blind.get("requires_live_agent"):
        return [_step("blind", blind_label, "info",
                      "The blind pass judged this bug reproducible only with a "
                      "live model agent, which the sandbox does not run — nothing "
                      "was executed.")]
    if not blind.get("test_cmd"):
        steps = [_step("blind", blind_label, "info",
                       "The PR ships no test and no linked-issue repro — nothing "
                       "author-shipped to run.")]
        authored = signals.get("authored_test") or {}
        if authored.get("attempted"):
            steps.extend(_authored_steps(authored, signals))
        return steps

    faithful = blind.get("faithful")
    steps = [_step("blind", blind_label,
                   "pass" if faithful else "warn" if faithful is None else "fail",
                   "Judged faithful — committed before any container ran."
                   if faithful else "No verdict recorded." if faithful is None
                   else "Judged NOT faithful — even a clean red→green escalates "
                        "to a human on this verdict.")]

    apply_label = "Apply the PR's diff to a pinned copy of main"
    apply_exit = rg.get("apply_exit")
    if apply_exit == gates.SENTINEL_PASS:
        steps.append(_step("apply", apply_label, "pass", "Applied cleanly."))
    elif apply_exit == gates.SENTINEL_PATCH_CONFLICT:
        steps.append(_step("apply", apply_label, "fail",
                           "The diff does not apply — the author must rebase."))
        return steps
    elif apply_exit is None:
        steps.append(_step("apply", apply_label, "skip", "Never ran."))
        return steps
    else:
        steps.append(_step("apply", apply_label, "fail",
                           f"Infrastructure error (exit {apply_exit}) — not a "
                           "verdict; the run re-queues."))
        return steps

    steps.append(_red_step(blind, rg, judge))
    steps.append(_green_step(rg))
    confirm_step = _confirm_step(rg)
    if confirm_step:
        steps.append(confirm_step)
    steps.extend(_lane_steps(signals))
    if regress:
        steps.append(_regress_step(regress))
    repro_step = _repro_step(blind, repro, rating)
    if repro_step:
        steps.append(repro_step)
    return steps


def _cause(outcome: str | None, signals: dict) -> str | None:
    """One sentence naming the specific check that broke, for the blocking and
    attention outcomes — None when nothing broke (or the headline already says
    everything, as for the unverifiable outcomes)."""
    if not signals:
        return None
    rg = signals.get("red_green") or {}
    if outcome == "needs-rebase":
        return ("The PR's diff no longer applies to pinned main — nothing could "
                "run until the author rebases.")
    if outcome == "not-verified":
        if rg.get("red_exit") == gates.SENTINEL_PASS:
            blind = signals.get("blind_adequacy") or {}
            flag = gates.vacuous_name_filter(blind, rg)
            if flag is not None:
                return ("The PR's test never failed on unfixed main (exit 0), and "
                        f"the test command carries a name filter ({flag}) — a "
                        "filter that matches no test title skips every test and "
                        "exits 0, so the likeliest cause is that no test ran at "
                        "all. A harness defect in the command, not evidence about "
                        "the PR. See the first ✗ below for the judge's explanation.")
            return ("The PR's test never failed on unfixed main (exit 0), so the "
                    "bug was never reproduced and the fix was never demonstrated. "
                    "See the first ✗ below for the judge's explanation.")
        if rg.get("green_exit") == gates.SENTINEL_TEST_FAIL:
            return "The PR's test still failed after the fix was applied."
        return ("The test failed on unfixed main, but not for the predicted "
                "reason — the failure does not demonstrate the claimed bug. See "
                "the first ✗ below for the judge's explanation.")
    if outcome == "escalate":
        blind = signals.get("blind_adequacy") or {}
        if blind.get("faithful") is False:
            return ("The signals disagree: the blind reviewer judged the test "
                    "unfaithful, yet it went cleanly red→green. A human must "
                    "decide which is right.")
        lane = gates._lane_escalate_cause(signals)
        if lane is not None:
            entry = (signals.get("lanes") or {}).get(lane)
            exit_ = entry.get("exit") if isinstance(entry, dict) else None
            return (f"The {lane} lane could not run to a verdict (infrastructure "
                    f"exit {exit_}) — not evidence about the PR; re-queue once "
                    "the harness is healthy, or decide from the other signals.")
        red2, green2 = rg.get("red_exit_confirm"), rg.get("green_exit_confirm")
        if (red2 is not None or green2 is not None) and not (
                red2 == gates.SENTINEL_TEST_FAIL
                and gates.green_confirm_accepted(rg)):
            return ("The confirm re-run disagreed with the first red→green — a "
                    "nondeterministic reproduction, or a failure the first "
                    "green run did not carry. A human must judge; both runs "
                    "are in the steps below.")
        return ("The run went red→green, but the judge's failure-reason rating "
                "is low-confidence — a human should read the red output and "
                "decide.")
    if outcome == "regressed":
        lanes = signals.get("lanes") or {}
        failed = next((n for n, e in lanes.items()
                       if isinstance(e, dict) and e.get("exit") == gates.SENTINEL_TEST_FAIL),
                      None)
        if failed is not None:
            return (f"The fix works, but the {failed} lane fails with the PR "
                    f"applied — the merged tree does not pass "
                    f"`{lanes[failed].get('cmd')}`.")
        return ("The fix works, but with the PR applied other tests that pass "
                "on pinned main fail — confirmed on a second run.")
    if outcome == "unverifiable-no-test":
        authored = signals.get("authored_test") or {}
        if authored.get("attempted"):
            return ("The PR ships no test; an agent-authored test was attempted "
                    "as corroboration and did not end agent-verified — the story "
                    "below shows where it stopped.")
    return None


def verify_request_view(rec: Pr) -> VerifyRequestView | None:
    """The PR's verification-queue state rendered for the app detail view,
    or None when the PR was never queued. The log_tail is the orchestrator's
    captured output on an errored run — ANSI-stripped like every other tail."""
    req = rec.verify_request
    if not req:
        return None
    tail = req.get("log_tail")
    return {
        "status": str(req.get("status", "")),
        "source": req.get("source"),
        "step": req.get("step"),
        "queued_at": req.get("queued_at"),
        "started_at": req.get("started_at"),
        "finished_at": req.get("finished_at"),
        "error_kind": req.get("error_kind"),
        "error": req.get("error"),
        "log_tail": _strip_ansi(tail) if isinstance(tail, str) else None,
        "checked_at": req.get("checked_at"),
        "fault": _request_fault(req.get("error_kind")),
    }


def verify_detail(rec: Pr) -> VerifyDetail | None:
    """The PR's verify section rendered for the app detail view, or None
    when the phase never reached this PR (no section at all)."""
    sec = rec.section("verify")
    if not sec:
        return None
    outcome = rec.verify_outcome
    level, headline, detail = (_OUTCOME_COPY.get(outcome) if outcome else None) or _PENDING_COPY
    stale = freshness.currency_failure(rec, "verify",
                                       max_age_days=gates.VERIFY_MAX_AGE_DAYS)
    if stale and outcome is not None:
        detail = (f"{detail} This verdict is {stale} — it no longer counts for "
                  "merge; re-queue it to re-verify.")
    incomplete = gates.verify_signals_incomplete(rec)
    if incomplete is not None and outcome in ("verified-fix", "agent-verified"):
        # Partial evidence never wears the full-confidence banner: the level
        # demotes and the gap leads the detail copy, ahead of the fold.
        level = "attention"
        headline = "Verified with incomplete evidence — one signal did not corroborate"
        detail = (f"The gated signals passed, but {incomplete}. The auto-merge "
                  f"recommendation is withheld until a re-verify completes the "
                  f"evidence. {detail}")
    signals = rec.verify_signals
    return {
        "outcome": outcome,
        "tier": sec.get("tier"),
        "checked_at": sec.get("checked_at"),
        "against_base_sha": rec.verify_base_sha,
        "against_head_sha": sec.get("against_head_sha"),
        "stale_reason": stale,
        "signals_incomplete": incomplete,
        "level": level,
        "state": _state_label(outcome),
        "headline": headline,
        "detail": detail,
        "fault": _outcome_fault(outcome, signals, rec.verify_findings),
        "cause": _cause(outcome, signals),
        "story": _build_story(signals),
        "signals": _clean_signals(signals),
        "findings": rec.verify_findings,
    }
