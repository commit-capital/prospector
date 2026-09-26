"""The cross-tested issue-fix lane: several independent agents each reproduce and
fix a reported defect, every fix is run against every other agent's failing
test, and only a fix independent reproductions agree on is judged further.

The candidates (`settings.issue_fix_models()`, one agent per model, run at once)
work in their own clones with the one-agent lane's prompt
(`solo_lane.author`). Each finished patch is re-gated
(`issue_gates.fix_patch_regate`, no rewrite of an existing test). A candidate's
tests are a reproduction only when they fail twice on the unfixed tree. Then
every candidate's fix runs against every reproduction — its own and the
others' — and a fix is agreed when it passes each of them and at least
AGREEMENT of them. Two readings of the report that pin different behavior
cannot both be passed, so a disagreement ends the run `fix-disputed`: the
report leaves the correct behavior open. The smallest agreed fix then goes
through the host's checks (`solo_lane.host_checks`: compile, related tests,
the full suite) together with the tests of every reproduction it passed
(`shipped_reproductions`). Agreement proves the fix does what the report asks,
never that it does nothing more, so a fix that clears the checks then faces the
scope-safety reviewer (`review_issue_fix`), and ends `fixed` only on its
explicit `safe`.

`run` returns the staged lane's `LaneResult`, so the replay scores it like the
other lanes; `agent_runs` counts every candidate and the reviewer.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from issue_triage import fix_lane, issue_gates, lane_check, lane_tree, review_issue_fix, solo_lane
from pipeline import (
    check_records,
    diffpaths,
    gates,
    headless_agent,
    prove,
    risktier,
    settings,
    threats,
    verify_driver,
)

# Reproductions an agreed fix must pass, its own among them.
AGREEMENT = 2


@dataclass
class Candidate:
    index: int
    model: str
    ending: str | None = None
    detail: str = ""
    verdict: dict = field(default_factory=dict)
    test_patch: str = ""
    fix_patch: str = ""
    test_paths: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    reproduces: bool = False
    red: prove.Legs | None = None
    passes: dict[int, bool] = field(default_factory=dict)

    def summary(self) -> dict:
        return {"index": self.index, "model": self.model, "ending": self.ending,
                "detail": self.detail[:300], "tests": self.test_paths,
                "fix_lines": issue_gates.changed_line_count(self.fix_patch),
                "reproduces": self.reproduces,
                "passes": {str(k): v for k, v in self.passes.items()}}


def _author(spec: fix_lane.LaneSpec, workdir: Path, cand: Candidate,
            pre_patch_file: Path | None) -> Candidate:
    """One candidate's agent, in its own clone, and the re-gate of its patch.
    The clone is removed once the patch is read. An agent outage or a declined
    prompt propagates."""
    clone_dir = workdir / f"cand-{cand.index}"
    records = lane_check.records_path(workdir, f"cand-{cand.index}")
    try:
        clone = lane_tree.materialize(spec.base.clone, clone_dir / "src",
                                      pre_patch=spec.pre_patch)
        env = lane_check.check_env(issue=spec.issue, base=spec.base, worktree=clone,
                                   records=records, test_patch=None, pre_patch=pre_patch_file)
        env["PROSPECTOR_ISSUE_CHECK_MAX_RUNS"] = str(solo_lane.MAX_RUNS)
        verdict = solo_lane.author(str(clone), title=spec.title, body=spec.body, env=env,
                                   model=cand.model)
        cand.checks = check_records.collect(records, solo_lane.MAX_RUNS)
        if "give_up" in verdict:
            cand.ending = "not-a-defect" if verdict["kind"] == "not-a-defect" else "no-fix"
            cand.detail = verdict["give_up"]
            return cand
        cand.verdict = verdict
        patch = lane_tree.authored_patch(clone)
        cand.fix_patch = diffpaths.filter_diff(patch, lambda p: not diffpaths.is_test_path(p))
        cand.test_patch = diffpaths.filter_diff(patch, diffpaths.is_test_path)
        cand.test_paths = diffpaths.changed_paths(cand.test_patch)
        _, other = lane_tree.new_files(clone)
        rewritten = sorted(set(p for p in other if diffpaths.is_test_path(p))
                           - set(lane_tree.additive_test_edits(clone, other)))
        if rewritten:
            cand.ending = "fix-untrusted"
            cand.detail = "the change rewrites existing tests: " + ", ".join(rewritten)
            return cand
        ok, why = issue_gates.fix_patch_regate(cand.fix_patch, changes=verdict["changes"],
                                               max_lines=settings.issue_fix_max_lines())
        if not ok:
            cand.ending, cand.detail = "fix-untrusted", why
        return cand
    except (headless_agent.EditsBlockedError, RuntimeError, ValueError) as e:
        if isinstance(e, (headless_agent.AgentUnavailable, headless_agent.AgentDeclined)):
            raise
        cand.ending, cand.detail = "run-failed", str(e)
        return cand
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)


def _twice(legs: prove.Legs, want: int) -> bool:
    return legs.get("exit") == want and legs.get("exit_confirm") == want


def agreed_candidates(live: list[Candidate]) -> list[Candidate]:
    """The candidates whose fix passes every reproduction it was run against,
    and at least AGREEMENT of them."""
    return [c for c in live
            if len(c.passes) >= AGREEMENT and all(c.passes.values())]


def shipped_reproductions(pick: Candidate, repros: list[Candidate]) -> list[Candidate]:
    """The reproductions whose tests ship with the picked fix: the pick's own
    first when it reproduces, then every other in index order, leaving out one
    whose test files another shipped reproduction already writes."""
    ordered = sorted(repros, key=lambda r: (r is not pick, r.index))
    shipped: list[Candidate] = []
    taken: set[str] = set()
    for r in ordered:
        if taken.isdisjoint(r.test_paths):
            shipped.append(r)
            taken.update(r.test_paths)
    return shipped


def _evidence(proof: dict, shipped: list[Candidate]) -> str:
    """What the host observed, in words, for the scope-safety reviewer."""
    lines = [f"- {len(shipped)} independent reproduction(s) "
             f"({', '.join(p for r in shipped for p in r.test_paths)}) each failed twice on "
             "the unfixed tree and passed twice with this change applied."]
    compiled = proof.get("compile")
    if compiled:
        lines.append("- The compile command passes with the change applied."
                     if compiled.get("exit") == gates.SENTINEL_PASS
                     else "- The compile command fails on the unfixed tree too.")
    related = proof.get("related_tests")
    if related:
        run = related.get("run") or {}
        verdict = ("passed" if run.get("exit") == gates.SENTINEL_PASS
                   else "failed, and fails on the unfixed tree too" if related.get("base_fails")
                   else f"failed (exit {run.get('exit')})")
        lines.append(f"- Related existing tests ({', '.join(related.get('files') or [])}): "
                     f"{verdict}.")
    else:
        lines.append("- Related existing tests: none were found for the changed paths.")
    suite = proof.get("suite")
    if suite and not suite.get("skipped"):
        lines.append("- The full test suite shows no failure the unfixed tree does not.")
    lines.append("- Every reproduction checks the behavior the report asks for; none checks "
                 "what must stay unchanged, so no test here would catch a change that does "
                 "more than the report asks.")
    return "\n".join(lines)


def run(spec: fix_lane.LaneSpec, *, workdir: Path,
        on_step: Callable[[str], None] = lambda step: None) -> fix_lane.LaneResult:
    """Several agents reproduce and fix `spec`'s issue; cross-testing their
    reproductions picks the fix they agree on, and the host's checks name the
    ending. Every clone is removed on the way out."""
    label = f"cross-{spec.issue}"
    models = settings.issue_fix_models()
    reproduction: dict | None = None
    result: dict | None = None
    agent_runs = 0

    def finish(ending: str, detail: str) -> fix_lane.LaneResult:
        return fix_lane.LaneResult(ending=ending, fault=ending in fix_lane.ENDINGS_FAULT,
                                   detail=detail, reproduction=reproduction, result=result,
                                   agent_runs=agent_runs)

    workdir.mkdir(parents=True, exist_ok=True)
    pre_patch_file: Path | None = None
    if spec.pre_patch is not None:
        pre_patch_file = workdir / "pre.patch"
        pre_patch_file.write_text(spec.pre_patch)

    def proof_patch(*parts: str) -> Path:
        if spec.pre_patch is not None:
            return prove.flatten(spec.base.clone, spec.pre_patch, *parts, label=label)
        return prove.compose(label, *parts)

    try:
        on_step(f"{len(models)} agents reproducing and fixing")
        agent_runs = len(models)
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            cands = list(pool.map(
                lambda im: _author(spec, workdir, Candidate(index=im[0], model=im[1]),
                                   pre_patch_file), enumerate(models)))
        result = {"candidates": [c.summary() for c in cands], "proof": {}, "reviews": [],
                  "patch": "",
                  "candidate_patches": [{"index": c.index, "test_patch": c.test_patch,
                                         "fix_patch": c.fix_patch} for c in cands]}
        live = [c for c in cands if c.ending is None]
        if not live:
            endings = [c.ending for c in cands]
            if all(e == "not-a-defect" for e in endings):
                return finish("not-a-defect", cands[0].detail)
            if all(e == "run-failed" for e in endings):
                return finish("run-failed", cands[0].detail)
            return finish("no-fix", "; ".join(f"{c.model}: {c.ending}" for c in cands)[:400])

        on_step("proving each reproduction on the unfixed tree")
        for c in live:
            cmd = verify_driver.derive_test_command(c.test_paths)
            if cmd:
                red = prove.red_legs(spec.base, patch=proof_patch(c.test_patch),
                                     test_cmd=cmd, label=label)
                c.red = red
                c.reproduces = _twice(red, gates.SENTINEL_TEST_FAIL)
        repros = [c for c in live if c.reproduces]

        on_step("cross-testing every fix against every reproduction")
        for c in live:
            for r in repros:
                cmd = verify_driver.derive_test_command(r.test_paths)
                assert cmd is not None  # a reproduction has test paths
                legs = prove.green_legs(spec.base, patch=proof_patch(r.test_patch, c.fix_patch),
                                        test_cmd=cmd, label=label)
                c.passes[r.index] = _twice(legs, gates.SENTINEL_PASS)
        agreed = agreed_candidates(live)
        result["candidates"] = [c.summary() for c in cands]
        result["agreement"] = {"reproductions": [r.index for r in repros],
                               "agreed": [c.index for c in agreed]}
        if not agreed:
            if len(repros) < AGREEMENT:
                return finish("fix-unproven", f"{len(repros)} independent reproduction(s); "
                                              f"an agreed fix needs {AGREEMENT}")
            return finish("fix-disputed", "no fix passes every reproduction: the candidates "
                                          "read the report's correct behavior differently")

        pick = min(agreed, key=lambda c: (issue_gates.changed_line_count(c.fix_patch), c.index))
        shipped = shipped_reproductions(pick, repros)
        test_patch = "".join(r.test_patch for r in shipped)
        test_paths = [p for r in shipped for p in r.test_paths]
        paths = diffpaths.changed_paths(test_patch + pick.fix_patch)
        reproduction = {"outcome": "reproduced", "base_sha": spec.base.sha,
                        "red": shipped[0].red, "files": [{"path": p} for p in test_paths]}
        result["agreement"]["shipped"] = [r.index for r in shipped]
        result.update({"patch": test_patch + pick.fix_patch, "pick": pick.index,
                       "changes": pick.verdict["changes"], "summary": pick.verdict["summary"],
                       "root_cause": pick.verdict["root_cause"],
                       "threat": threats.scan_diff(pick.fix_patch),
                       "tier": risktier.tier_facet(paths), "checks": pick.checks})
        tree = lane_tree.materialize(spec.base.clone, workdir / "pick" / "src",
                                     pre_patch=spec.pre_patch)
        blocked = solo_lane.host_checks(
            spec, test_patch=test_patch, fix_patch=pick.fix_patch,
            test_paths=test_paths, tree=tree, proof_patch=proof_patch, label=label,
            proof=result["proof"], on_step=on_step, own_green=False)
        if blocked:
            return finish(*blocked)

        on_step("reviewing: scope-safety")
        lane_tree.apply_patch(tree, test_patch + pick.fix_patch)
        agent_runs += 1
        review = review_issue_fix.review(
            str(tree), pick.fix_patch, lens="scope-safety", title=spec.title, body=spec.body,
            root_cause=str(pick.verdict["root_cause"]), test_paths=test_paths,
            evidence=_evidence(result["proof"], shipped))
        result["reviews"] = [review]
        if review.get("failed"):
            return finish("run-failed", str(review.get("reason")
                                            or "the reviewing agent did not finish"))
        if review.get("verdict") != "safe":
            return finish("fix-rejected", f"scope-safety: {review.get('reason') or 'not safe'}")
        return finish("fixed", f"{len(agreed)} of {len(live)} fixes pass all {len(repros)} "
                               "reproductions; the smallest clears the host's checks and the "
                               "scope-safety review")
    except headless_agent.AgentUnavailable as e:
        return finish("agent-unavailable", str(e))
    except headless_agent.AgentDeclined as e:
        return finish("declined", str(e))
    except (prove.NoBase, prove.SuiteFault, verify_driver.ProbeFailure) as e:
        return finish("sandbox", str(e))
    except (headless_agent.EditsBlockedError, RuntimeError, ValueError) as e:
        return finish("run-failed", str(e))
    finally:
        shutil.rmtree(workdir / "pick", ignore_errors=True)
