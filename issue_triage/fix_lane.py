"""Drive one reported issue through reproduce -> judge -> fix -> prove -> review
over single-commit clones of the machine's pinned base, and let only
host-observed sandbox exits and pure policy name the ending.

`run` is the integrator: it materializes a clone per stage, hands each locked-down
agent its clone, and reads the result back itself. The reproduction agent writes
two sets of tests: the reproduction, which must fail twice on the base, and the
preservation tests, which pin behavior a fix must keep and must pass twice on
it. The agents judge; the host decides — a reproduction outcome comes from
`issue_gates.reproduction_outcome` over the two red exits `prove` observed, and
a fix's fate from `issue_gates.fix_proof_bar` over the proof `prove` observed
(both sets, with the fix applied) and the two refuting reviews. Every fault is
a machine condition, never a verdict on the work: an agent outage, a sandbox
that could not run, a stage that crashed. The clones are removed on the way out.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from issue_triage import (
    fetch_issues,
    fix_issue,
    issue_gates,
    judge_repro,
    lane_check,
    lane_tree,
    reproduce_issue,
    review_issue_fix,
)
from issue_triage.issue_store import IssueStore
from pipeline import (
    check_records,
    diffpaths,
    gates,
    headless_agent,
    profile,
    prove,
    resolve_evidence,
    risktier,
    settings,
    storekit,
    threats,
    verify_driver,
)
from pipeline.store import Store
from pipeline.wire import VerifyAuthoredFile

ENDINGS_VERDICT = ("reproduced", "fixed", "not-reproduced", "unwritable", "wrong-symptom",
                   "not-a-defect", "no-fix", "fix-untrusted", "fix-unproven", "fix-rejected",
                   "declined", "cancelled")
ENDINGS_FAULT = ("agent-unavailable", "run-failed", "sandbox", "base-compile")

# The reproduction agent gets one retry when a host validator rejects its files
# or the first red leg passes on the base.
MAX_REPRO_ATTEMPTS = 2

# Sandbox records a stage's agent may leave, collected once when the stage ends.
CHECKS_LIMIT = lane_check.MAX_RUNS * MAX_REPRO_ATTEMPTS


def report_sha(title: str, body: str) -> str:
    """A stable 16-hex fingerprint of the reported title and body, so an edit to
    the report the reproduction was written against is visible as a changed sha."""
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LaneSpec:
    issue: int
    title: str
    body: str
    base: prove.PinnedBase
    action: Literal["reproduce", "fix"] = "fix"
    pre_patch: str | None = None


@dataclass
class LaneResult:
    ending: str
    fault: bool
    detail: str
    reproduction: dict | None = None
    result: dict | None = None
    agent_runs: int = 0


def _fault(ending: str) -> bool:
    return ending in ENDINGS_FAULT


def _is_judged_rejection(review: dict) -> bool:
    """True when a review is a reviewer's decision to reject; False when the
    reviewer itself never reached a verdict."""
    return review.get("verdict") != "safe" and not review.get("failed")


def _repro_detail(outcome: str, gave_up: str | None, invalid: str | None) -> str:
    if outcome == "unwritable":
        return gave_up or invalid or "no faithful reproduction was writable"
    return {
        "not-reproduced": "the reproduction did not fail on the pinned base",
        "wrong-symptom": "the failure does not show the reported symptom",
        "not-a-defect": "the reported behavior is not a defect in this code",
    }.get(outcome, outcome)


@dataclass(frozen=True)
class Authored:
    """A reproduction's accepted test sets: the files that must fail on the base
    and the preservation files that must pass on it, each set's derived command,
    and the one patch that adds both."""
    files: list[VerifyAuthoredFile]
    preserve: list[VerifyAuthoredFile]
    test_cmd: str
    preserve_cmd: str
    test_patch: Path


def _validate_reproduction(clone: Path, verdict: dict, spec: LaneSpec, label: str
                           ) -> tuple[str | None, Authored | None]:
    """Hold the agent's authored files to the host's rules, first failure winning
    as the invalid reason: (None, the accepted sets) or (reason, None)."""
    untracked, other = lane_tree.new_files(clone)
    # A reproduction may add a test file, and may add cases to one that exists;
    # anything else it touched — production code, or a test line it removed or
    # rewrote — invalidates the set.
    extended = lane_tree.additive_test_edits(clone, other)
    if set(other) - set(extended):
        return "edited-tracked-files", None
    reported = [str(f["path"]) for f in verdict.get("files", [])]
    kept = [str(f["path"]) for f in verdict.get("preserve", [])]
    if not kept:
        return "no-preservation-tests", None
    if set(reported) & set(kept):
        return "preservation-overlaps-reproduction", None
    if set(untracked) | set(extended) != set(reported) | set(kept):
        return "undisclosed-files", None
    files, why = lane_tree.read_files(clone, reported)
    preserve, kept_why = lane_tree.read_files(clone, kept)
    if why or kept_why:
        return why or kept_why, None
    cmd, skipped = verify_driver.validate_test_files(
        files, verdict.get("expected_red_signature"), base_clone=spec.base.clone,
        taken_paths=[], may_exist=frozenset(extended))
    if skipped:
        return skipped, None
    preserve_cmd, kept_skipped = verify_driver.validate_test_files(
        preserve, None, base_clone=spec.base.clone, taken_paths=reported,
        may_exist=frozenset(extended), red=False)
    if kept_skipped:
        return f"preservation-{kept_skipped}", None
    assert cmd is not None and preserve_cmd is not None  # a clean set has a command
    # Content-addressed, so a concurrent run of the same issue never swaps in
    # its own tests.
    test_patch = prove.compose(label, lane_tree.authored_test_diff(clone, reported + kept))
    if threats.scan_diff(test_patch.read_text())["verdict"] != "clear":
        return "threat-signature", None
    return None, Authored(files=files, preserve=preserve, test_cmd=cmd,
                          preserve_cmd=preserve_cmd, test_patch=test_patch)


def _passed_twice(legs: prove.Legs | dict) -> bool:
    return legs.get("exit") == gates.SENTINEL_PASS == legs.get("exit_confirm")


def _proof_evidence(proof: dict, authored: Authored) -> str:
    """What the host observed, in words, for the reviewers: each test set's
    runs with the change applied, and the related tests it ran."""
    def outcome(legs: dict | None) -> str:
        if not legs:
            return "not run"
        if _passed_twice(legs):
            return "passed twice"
        return f"did not pass (exit {legs.get('exit')}, confirm {legs.get('exit_confirm')})"

    lines = [
        f"- Reproduction tests ({', '.join(f['path'] for f in authored.files)}): failed "
        f"twice on the base; with the change applied, {outcome(proof.get('green'))}.",
        f"- Preservation tests ({', '.join(f['path'] for f in authored.preserve)}): passed "
        f"twice on the base; with the change applied, {outcome(proof.get('preserve'))}.",
    ]
    related = proof.get("related_tests")
    if related:
        run = related.get("run") or {}
        verdict = ("passed" if run.get("exit") == gates.SENTINEL_PASS
                   else "failed, and fails on the base too" if related.get("base_fails")
                   else f"failed (exit {run.get('exit')})")
        lines.append(f"- Related existing tests ({', '.join(related.get('files') or [])}): "
                     f"{verdict}.")
    else:
        lines.append("- Related existing tests: none were found for the changed paths.")
    return "\n".join(lines)


def run(spec: LaneSpec, *, workdir: Path,
        on_step: Callable[[str], None] = lambda step: None,
        still_valid: Callable[[], str | None] = lambda: None) -> LaneResult:
    """Reproduce, then (for `action == "fix"`) fix the issue, returning the
    ending the host observed. `workdir` holds the per-stage clones, removed on
    the way out; `on_step` is told each phase as it begins; `still_valid` names a
    reason to cancel (an issue closed or a report edited under the run) and is
    consulted before the fix stage and before the final result."""
    agent_runs = 0
    reproduction: dict | None = None
    result: dict | None = None
    label = f"issue-{spec.issue}"
    repro_dir = workdir / "repro"
    fix_dir = workdir / "fix"
    pre_patch_file: Path | None = None
    if spec.pre_patch is not None:
        workdir.mkdir(parents=True, exist_ok=True)
        pre_patch_file = workdir / "pre.patch"
        pre_patch_file.write_text(spec.pre_patch)

    def finish(ending: str, detail: str) -> LaneResult:
        return LaneResult(ending=ending, fault=_fault(ending), detail=detail,
                          reproduction=reproduction, result=result, agent_runs=agent_runs)

    def proof_patch(*parts: Path | str | None) -> Path:
        if spec.pre_patch is not None:
            return prove.flatten(spec.base.clone, spec.pre_patch, *parts, label=label)
        return prove.compose(label, *parts)

    try:
        # --- reproduction, with one retry on a rejected set or a passing red ---
        retry_note: str | None = None
        gave_up: str | None = None
        invalid: str | None = None
        verdict: dict = {}
        authored: Authored | None = None
        red: prove.Legs | dict[str, object] = {}
        kept_on_base: prove.Legs | None = None
        clone = repro_dir / "src"

        for _attempt in range(MAX_REPRO_ATTEMPTS):
            on_step("preparing the clone")
            clone = lane_tree.materialize(spec.base.clone, repro_dir / "src",
                                          pre_patch=spec.pre_patch)
            on_step("agent authoring the reproduction")
            agent_runs += 1
            verdict = reproduce_issue.author(
                str(clone), issue=spec.issue, title=spec.title, body=spec.body,
                env=lane_check.check_env(
                    issue=spec.issue, base=spec.base, worktree=clone,
                    records=lane_check.records_path(workdir, "repro"), test_patch=None,
                    pre_patch=pre_patch_file),
                retry_note=retry_note)
            if "give_up" in verdict:
                gave_up = str(verdict["give_up"])
                break
            invalid, authored = _validate_reproduction(clone, verdict, spec, label)
            if invalid:
                retry_note = invalid
                continue
            assert authored is not None  # a clean set is accepted
            on_step("proving red on the pinned base")
            red = prove.red_legs(spec.base, patch=proof_patch(authored.test_patch),
                                 test_cmd=authored.test_cmd, label=label)
            if red.get("exit") == gates.SENTINEL_PASS:
                retry_note = "the reproduction passed on the pinned base"
                continue
            if red.get("exit") != gates.SENTINEL_TEST_FAIL:
                break
            on_step("proving the preservation tests pass on the pinned base")
            kept_on_base = prove.green_legs(
                spec.base, patch=proof_patch(authored.test_patch),
                test_cmd=authored.preserve_cmd, label=label)
            if not _passed_twice(kept_on_base):
                if gates.SENTINEL_TEST_FAIL not in (kept_on_base.get("exit"),
                                                    kept_on_base.get("exit_confirm")):
                    return finish("sandbox", f"the preservation tests exited "
                                             f"{kept_on_base.get('exit')} on the base "
                                             "without a test verdict")
                invalid = retry_note = "the preservation tests fail on the pinned base"
                continue
            break

        judge_result: dict | None = None
        if (not gave_up and not invalid and red.get("exit") == gates.SENTINEL_TEST_FAIL
                and red.get("exit_confirm") == gates.SENTINEL_TEST_FAIL):
            on_step("judging the reproduction")
            agent_runs += 1
            assert authored is not None  # a red pair follows an accepted set
            judge_result = judge_repro.judge(
                str(clone), title=spec.title, body=spec.body, files=authored.files,
                claimed_symptom=str(verdict.get("claimed_symptom") or ""),
                expected_red_signature=str(verdict.get("expected_red_signature") or ""),
                red_tail=str(red.get("output_tail") or ""))

        outcome = issue_gates.reproduction_outcome(
            cast(dict, red), judge_result, gave_up=bool(gave_up), invalid=invalid)
        files = authored.files if authored else []
        preserve = authored.preserve if authored else []
        reproduction = {
            "outcome": outcome,
            "base_sha": spec.base.sha,
            "tier": risktier.tier_facet([f["path"] for f in files + preserve]),
            "report_sha": report_sha(spec.title, spec.body),
            "files": files,
            "test_cmd": authored.test_cmd if authored else None,
            "preserve": preserve,
            "preserve_cmd": authored.preserve_cmd if authored else None,
            "preserve_on_base": kept_on_base,
            "claimed_symptom": str(verdict.get("claimed_symptom") or ""),
            "expected_red_signature": str(verdict.get("expected_red_signature") or ""),
            "red": red,
            "judge": judge_result,
            "give_up": gave_up,
            "checks": check_records.collect(
                lane_check.records_path(workdir, "repro"), CHECKS_LIMIT),
        }

        if outcome is None:
            if judge_result is not None and judge_result.get("failed"):
                return finish("run-failed", str(judge_result.get("reason")
                                                or "the judge returned no usable rating"))
            return finish("sandbox", f"the reproduction exited {red.get('exit')} on the "
                                     "base without a test verdict")
        if outcome != "reproduced":
            return finish(outcome, _repro_detail(outcome, gave_up, invalid))
        if spec.action == "reproduce":
            return finish("reproduced", "reproduced on the pinned base")

        # --- fix ---
        assert authored is not None
        test_patch = authored.test_patch
        test_paths = [f["path"] for f in files]
        preserve_paths = [f["path"] for f in preserve]
        reason = still_valid()
        if reason:
            return finish("cancelled", reason)

        on_step("agent authoring the fix")
        fix_clone = lane_tree.materialize(spec.base.clone, fix_dir / "src", files + preserve,
                                          pre_patch=spec.pre_patch)
        agent_runs += 1
        fix_verdict = fix_issue.author(
            str(fix_clone), issue=spec.issue, title=spec.title, body=spec.body,
            test_paths=test_paths, preserve_paths=preserve_paths,
            red_tail=str(red.get("output_tail") or ""),
            withheld_globs=gates.fix_withheld_globs(),
            env=lane_check.check_env(
                issue=spec.issue, base=spec.base, worktree=fix_clone,
                records=lane_check.records_path(workdir, "fix"), test_patch=test_patch,
                pre_patch=pre_patch_file))
        fix_checks = check_records.collect(
            lane_check.records_path(workdir, "fix"), CHECKS_LIMIT)
        if "give_up" in fix_verdict:
            return finish("no-fix", str(fix_verdict["give_up"]))

        on_step("re-gating the fix")
        fix_patch = lane_tree.authored_patch(fix_clone)
        changed_paths = diffpaths.changed_paths(fix_patch)
        test_text = test_patch.read_text()
        if not test_text.endswith("\n"):
            test_text += "\n"
        result = {
            "patch": test_text + fix_patch,
            "changes": fix_verdict["changes"],
            "summary": fix_verdict["summary"],
            "root_cause": fix_verdict["root_cause"],
            "proof": {"red": red},
            "reviews": [],
            "threat": threats.scan_diff(fix_patch),
            "tier": risktier.tier_facet(changed_paths),
            "checks": fix_checks,
        }
        ok, why = issue_gates.fix_patch_regate(
            fix_patch, changes=fix_verdict["changes"], max_lines=settings.issue_fix_max_lines())
        if not ok:
            return finish("fix-untrusted", why)

        on_step("proving green")
        result["proof"]["green"] = prove.green_legs(
            spec.base, patch=proof_patch(test_patch, fix_patch),
            test_cmd=authored.test_cmd, label=label)
        on_step("proving the preservation tests still pass")
        result["proof"]["preserve"] = prove.green_legs(
            spec.base, patch=proof_patch(test_patch, fix_patch),
            test_cmd=authored.preserve_cmd, label=label)

        compile_cmd = profile.active().verify.compile_cmd
        if compile_cmd:
            on_step("compile preflight")
            compiled = prove.run_command(
                spec.base, proof_patch(test_patch, fix_patch), compile_cmd,
                phase="compile", label=label)
            result["proof"]["compile"] = compiled
            if compiled.get("error_kind") == "base-compile":
                return finish("base-compile", str(compiled.get("error")
                                                  or "the base fails the compile command"))

        repro_paths = set(test_paths) | set(preserve_paths)
        related = [t for t in resolve_evidence.related_tests(str(fix_clone), changed_paths)
                   if t not in repro_paths]
        related_cmd = verify_driver.derive_test_command(related)
        if related_cmd:
            on_step("related tests")
            entry: dict = {"files": related, "run": prove.run_command(
                spec.base, proof_patch(test_patch, fix_patch), related_cmd,
                phase="green", label=label)}
            if entry["run"].get("exit") == gates.SENTINEL_TEST_FAIL:
                base_run = prove.run_command(
                    spec.base, proof_patch(test_patch), related_cmd,
                    phase="green", label=label)
                if base_run.get("exit") == gates.SENTINEL_TEST_FAIL:
                    entry["base_fails"] = True
            result["proof"]["related_tests"] = entry

        reviews: list[dict] = []
        evidence = _proof_evidence(result["proof"], authored)
        on_step("reviewing: root-cause")
        agent_runs += 1
        reviews.append(review_issue_fix.review(
            str(fix_clone), fix_patch, lens="root-cause", title=spec.title, body=spec.body,
            root_cause=str(fix_verdict["root_cause"]), test_paths=test_paths + preserve_paths,
            evidence=evidence))
        if not _is_judged_rejection(reviews[0]):
            on_step("reviewing: scope-safety")
            agent_runs += 1
            reviews.append(review_issue_fix.review(
                str(fix_clone), fix_patch, lens="scope-safety", title=spec.title,
                body=spec.body, root_cause=str(fix_verdict["root_cause"]),
                test_paths=test_paths + preserve_paths, evidence=evidence))
        result["reviews"] = reviews

        stalled = [r for r in reviews if r.get("failed")]
        if stalled and not any(_is_judged_rejection(r) for r in reviews):
            return finish("run-failed", str(stalled[0].get("reason")
                                            or "a reviewing agent did not finish"))

        ending, gate_reason = issue_gates.fix_proof_bar(result)
        reason = still_valid()
        if reason:
            return finish("cancelled", reason)
        return finish(ending or "fixed", gate_reason)
    except headless_agent.AgentUnavailable as e:
        return finish("agent-unavailable", str(e))
    except headless_agent.AgentDeclined as e:
        return finish("declined", str(e))
    except (prove.NoBase, verify_driver.ProbeFailure) as e:
        return finish("sandbox", str(e))
    except (headless_agent.EditsBlockedError, RuntimeError, ValueError) as e:
        return finish("run-failed", str(e))
    finally:
        shutil.rmtree(repro_dir, ignore_errors=True)
        shutil.rmtree(fix_dir, ignore_errors=True)


def _reported(store: IssueStore, n: int) -> tuple[str, str] | None:
    """The reported title and body for issue `n`: the stored record when the
    store holds it, else a live fetch. None when neither knows the issue."""
    issue = store.load_issue(n)
    if issue is not None:
        return issue.title or "", issue.body or ""
    raw = fetch_issues.fetch_issue(n)
    if raw is not None:
        return raw["title"], raw.get("body") or ""
    return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m issue_triage.fix_lane",
        description="Run one reported issue through the fix lane on this machine's "
                    "held base, writing a result file and one ledger row.")
    ap.add_argument("--issue", type=int, required=True, help="the issue number to run")
    ap.add_argument("--reproduce-only", action="store_true",
                    help="stop after the reproduction proves red; author no fix")
    ap.add_argument("--base-sha",
                    help="prove against a base this machine already holds, named by "
                         "its SHA (default: the verify pin)")
    ap.add_argument("--tier", type=int, default=0,
                    help="the risk tier the held base was built at (with --base-sha)")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    n: int = args.issue
    action: Literal["reproduce", "fix"] = "reproduce" if args.reproduce_only else "fix"

    try:
        base = prove.held(args.base_sha, args.tier) if args.base_sha else prove.pinned(Store())
    except prove.NoBase as e:
        print(str(e), file=sys.stderr)
        return 2

    store = IssueStore()
    reported = _reported(store, n)
    if reported is None:
        print(f"issue #{n} is not in the store and could not be fetched", file=sys.stderr)
        return 2
    title, body = reported
    reported_sha = report_sha(title, body)

    def still_valid() -> str | None:
        live = fetch_issues.fetch_issue(n)
        if live is None:
            return None
        if live.get("state") != "open":
            return "issue-closed"
        if report_sha(live["title"], live.get("body") or "") != reported_sha:
            return "report-edited"
        return None

    spec = LaneSpec(issue=n, title=title, body=body, base=base, action=action)
    workdir = settings.verify_scratch() / "issue-fix" / f"issue-{n}"
    started = storekit.now()
    res = run(spec, workdir=workdir, on_step=lambda step: print(step, flush=True),
              still_valid=still_valid)
    finished = storekit.now()

    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(json.dumps({
        "issue": n,
        "report_sha": reported_sha,
        "base_sha": base.sha,
        "action": action,
        "ending": res.ending,
        "fault": res.fault,
        "detail": res.detail,
        "agent_runs": res.agent_runs,
        "started": started,
        "finished": finished,
        "reproduction": res.reproduction,
        "result": res.result,
    }, indent=2) + "\n")

    store.append_run({
        "phase": "issue-fix:run",
        "issue": n,
        "started": started,
        "finished": finished,
        "trigger": "cli",
        "stats": {
            "action": action,
            "ending": res.ending,
            "fault": res.fault,
            "detail": res.detail,
            "host": settings.worker_id(),
            "base_sha": base.sha,
            "report_sha": reported_sha,
            "agent_runs": res.agent_runs,
        },
    })

    print(f"{res.ending}: {res.detail}")
    print(f"result: {workdir / 'result.json'}")
    return 1 if res.fault else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
