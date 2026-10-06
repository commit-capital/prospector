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
AGREEMENT of them. A reading of the report is a group of candidates whose fixes
pass every reproduction in the group, their own included (`readings`). Two
readings pin different behavior no single fix passes, so a run with two ends
`fix-disputed`: the report leaves the correct behavior open. A cross-test run
that fails only existing tests names them (`pinned_failures`), and fixes that
would be agreed but for such tests end the run `fix-pinned`: the lane never
rewrites a repository's tests, so whether they still hold is a maintainer's
call. Any other run without an agreed fix ends `fix-unproven`. The smallest
agreed fix then goes through the host's checks (`solo_lane.host_checks`:
compile, related tests, the full suite) together with the tests of every
reproduction it passed (`shipped_reproductions`). Agreement proves the fix does
what the report asks, never that it does nothing more, so a fix that clears the
checks then faces the scope-safety reviewer (`review_issue_fix`), and ends
`fixed` only on its explicit `safe`. The reviewer's inventory of unasked changes to inputs that
worked vetoes a `safe` only on tier-0 paths; elsewhere it is recorded beside the
verdict for the maintainer who reviews the proposal.

`run` returns the staged lane's `LaneResult`, so the replay scores it like the
other lanes; `agent_runs` counts every candidate and the reviewer.
"""
from __future__ import annotations

import contextvars
import itertools
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from issue_triage import (
    fix_lane,
    issue_gates,
    lane_check,
    lane_tree,
    review_issue_fix,
    solo_lane,
    trust_boundary,
)
from pipeline import (
    authoring,
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
    # The unfixed tree's copy of each existing test file its tests extend.
    base_tests: dict[str, str] = field(default_factory=dict)
    # By reproduction, the existing tests its fix fails there when they are all it fails.
    pinned: dict[int, list[str]] = field(default_factory=dict)

    def summary(self) -> dict:
        return {"index": self.index, "model": self.model, "ending": self.ending,
                "detail": self.detail[:300], "tests": self.test_paths,
                "fix_lines": issue_gates.changed_line_count(self.fix_patch),
                "reproduces": self.reproduces,
                "passes": {str(k): v for k, v in self.passes.items()},
                "pinned": {str(k): v for k, v in self.pinned.items()}}


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
                                   model=cand.model, guidance=spec.guidance, notes=spec.notes,
                                   contributor_docs=authoring.docs_from_tree(spec.base.clone))
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
        cand.base_tests = lane_tree.committed_texts(clone, cand.test_paths)
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


def pinned_failures(green: prove.Legs, red: prove.Legs | None, test_patch: str,
                    base_tests: dict[str, str]) -> list[str]:
    """The tests a fix's run of a reproduction fails, when every one is an
    existing test: not failing on the unfixed tree, titled on no line the
    reproduction adds, and titled in the unfixed copy of a file it extends.
    [] when the run fails anything else, or its report or the reproduction's
    red one does not parse. The reports are the tests' own output, read only to
    name why a fix is not agreed."""
    if green.get("exit") != gates.SENTINEL_TEST_FAIL or red is None:
        return []
    failing = verify_driver.parse_failed_tests(green["output_tail"])
    red_failing = verify_driver.parse_failed_tests(red["output_tail"])
    if not failing or red_failing is None or set(failing) & set(red_failing):
        return []
    added = "\n".join(line[1:] for line in test_patch.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    for name in failing:
        title = name.rsplit(" > ", 1)[-1].strip()
        if not title or title in added or not any(title in t for t in base_tests.values()):
            return []
    return failing


def agreed_candidates(live: list[Candidate], *,
                      apart_from_pinned: bool = False) -> list[Candidate]:
    """The candidates whose fix passes every reproduction it was run against,
    and at least AGREEMENT of them. With `apart_from_pinned`, a run that fails
    only existing tests (`Candidate.pinned`) counts as a pass."""
    return [c for c in live if len(c.passes) >= AGREEMENT
            and all(ok or (apart_from_pinned and bool(c.pinned.get(r)))
                    for r, ok in c.passes.items())]


def readings(live: list[Candidate]) -> list[list[Candidate]]:
    """The live candidates grouped by the behavior they pin: each group's fixes
    pass every reproduction in the group, their own included, and it holds at
    least one reproduction. The largest groups come first; a candidate belongs
    to the first group that holds it."""
    groups: list[list[Candidate]] = []
    for k in range(len(live), 0, -1):
        for combo in itertools.combinations(live, k):
            if not any(c.reproduces for c in combo):
                continue
            if any(set(c.index for c in combo) <= set(c.index for c in g) for g in groups):
                continue
            if all(c.passes.get(r.index, False) for c in combo for r in combo if r.reproduces):
                groups.append(list(combo))
    out: list[list[Candidate]] = []
    taken: set[int] = set()
    for g in groups:
        rest = [c for c in g if c.index not in taken]
        if any(c.reproduces for c in rest):
            out.append(rest)
            taken.update(c.index for c in rest)
    return out


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


def _judge_pick(spec: fix_lane.LaneSpec, workdir: Path, pick: Candidate,
                repros: list[Candidate], result: dict, *,
                proof_patch: Callable[..., Path], label: str,
                on_step: Callable[[str], None]) -> tuple[str | None, str, dict, int]:
    """Ship `pick` with the tests of `repros` and put it through the host's
    checks and the scope-safety review, recording both on `result`, then record
    the trust boundaries a pick that clears them crosses. Returns the
    ending it stops at (None when it clears every check) with the reason, the
    reproduction record, and the agent runs spent. The review tree is removed on
    the way out."""
    shipped = shipped_reproductions(pick, repros)
    test_patch = "".join(r.test_patch for r in shipped)
    test_paths = [p for r in shipped for p in r.test_paths]
    paths = diffpaths.changed_paths(test_patch + pick.fix_patch)
    reproduction = {"outcome": "reproduced", "base_sha": spec.base.sha,
                    "red": shipped[0].red, "files": [{"path": p} for p in test_paths]}
    result.setdefault("agreement", {})["shipped"] = [r.index for r in shipped]
    result.update({"patch": test_patch + pick.fix_patch, "pick": pick.index,
                   "changes": pick.verdict["changes"], "summary": pick.verdict["summary"],
                   "root_cause": pick.verdict["root_cause"],
                   "threat": threats.scan_diff(pick.fix_patch),
                   "tier": risktier.tier_facet(paths), "checks": pick.checks})
    try:
        tree = lane_tree.materialize(spec.base.clone, workdir / "pick" / "src",
                                     pre_patch=spec.pre_patch)
        blocked = solo_lane.host_checks(
            spec, test_patch=test_patch, fix_patch=pick.fix_patch,
            test_paths=test_paths, tree=tree, proof_patch=proof_patch, label=label,
            proof=result["proof"], on_step=on_step, own_green=False)
        if blocked:
            return blocked[0], blocked[1], reproduction, 0

        on_step("reviewing: scope-safety")
        lane_tree.apply_patch(tree, test_patch + pick.fix_patch)
        review = review_issue_fix.review(
            str(tree), pick.fix_patch, lens="scope-safety", title=spec.title, body=spec.body,
            root_cause=str(pick.verdict["root_cause"]), test_paths=test_paths,
            evidence=_evidence(result["proof"], shipped),
            inventory_veto=result["tier"].get("tier") == 0)
        result["reviews"] = [review]
        if review.get("failed"):
            return "run-failed", str(review.get("reason")
                                     or "the reviewing agent did not finish"), reproduction, 1
        if review.get("verdict") != "safe":
            return ("fix-rejected", f"scope-safety: {review.get('reason') or 'not safe'}",
                    reproduction, 1)
        on_step("reviewing: trust boundaries")
        result["boundary"] = trust_boundary.judge(str(tree), spec.base.clone, pick.fix_patch,
                                                  title=spec.title, body=spec.body)
        return None, "", reproduction, 2
    finally:
        shutil.rmtree(workdir / "pick", ignore_errors=True)


def _proof_patcher(spec: fix_lane.LaneSpec, label: str) -> Callable[..., Path]:
    def proof_patch(*parts: str) -> Path:
        if spec.pre_patch is not None:
            return prove.flatten(spec.base.clone, spec.pre_patch, *parts, label=label)
        return prove.compose(label, *parts)
    return proof_patch


def _faulted(e: Exception) -> tuple[str, str]:
    """The ending a lane exception names."""
    if isinstance(e, headless_agent.AgentUnavailable):
        return "agent-unavailable", str(e)
    if isinstance(e, headless_agent.AgentDeclined):
        return "declined", str(e)
    if isinstance(e, (prove.NoBase, prove.SuiteFault, verify_driver.ProbeFailure)):
        return "sandbox", str(e)
    return "run-failed", str(e)


_LANE_ERRORS = (prove.NoBase, prove.SuiteFault, verify_driver.ProbeFailure,
                headless_agent.EditsBlockedError, RuntimeError, ValueError)


def _unagreed(live: list[Candidate], repros: list[Candidate], result: dict) -> tuple[str, str]:
    """The ending and reason of a run whose cross-test agreed no fix,
    recording on `result` a dispute's readings or the existing tests that hold
    the fixes back."""
    groups = readings(live)
    if len(groups) >= 2:
        result["readings"] = [[c.index for c in g] for g in groups]
        return "fix-disputed", ("no fix passes every reproduction: the candidates read the "
                                "report's correct behavior differently")
    held = agreed_candidates(live, apart_from_pinned=True)
    if held:
        tests = sorted({t for c in held for names in c.pinned.values() for t in names})
        result["agreement"]["pinned"] = {"agreed": [c.index for c in held], "tests": tests}
        return "fix-pinned", (f"{len(held)} fix(es) pass all {len(repros)} reproductions but "
                              f"fail existing tests the lane may not rewrite: {'; '.join(tests)}")
    if len(repros) < AGREEMENT:
        why = f"{len(repros)} independent reproduction(s); an agreed fix needs {AGREEMENT}"
    elif not groups:
        why = "no candidate's fix passes its own reproduction"
    else:
        why = (f"only one reading of the report has a fix that passes its reproductions, "
               f"and no fix passes all {len(repros)}")
    seen = sorted({t for c in live for t in c.pinned.get(c.index, [])})
    if seen:
        why += f"; existing tests the fixes fail: {'; '.join(seen)}"
    return "fix-unproven", why


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

    proof_patch = _proof_patcher(spec, label)

    try:
        on_step(f"{len(models)} agents reproducing and fixing")
        agent_runs = len(models)
        # Each candidate runs in a copy of this context, so the lane its spend is
        # metered under reaches the pool's threads.
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            cands = list(pool.map(
                lambda im: contextvars.copy_context().run(
                    _author, spec, workdir, Candidate(index=im[0], model=im[1]), pre_patch_file),
                enumerate(models)))
        result = {"candidates": [c.summary() for c in cands], "proof": {}, "reviews": [],
                  "patch": "",
                  "candidate_patches": [{"index": c.index, "test_patch": c.test_patch,
                                         "fix_patch": c.fix_patch, "test_paths": c.test_paths,
                                         "verdict": c.verdict, "checks": c.checks}
                                        for c in cands]}
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
                pinned = [] if c.passes[r.index] else pinned_failures(
                    legs, r.red, r.test_patch, r.base_tests)
                if pinned:
                    c.pinned[r.index] = pinned
        agreed = agreed_candidates(live)
        result["candidates"] = [c.summary() for c in cands]
        result["agreement"] = {"reproductions": [r.index for r in repros],
                               "agreed": [c.index for c in agreed]}
        if not agreed:
            return finish(*_unagreed(live, repros, result))

        pick = min(agreed, key=lambda c: (issue_gates.changed_line_count(c.fix_patch), c.index))
        ending, detail, reproduction, spent = _judge_pick(
            spec, workdir, pick, repros, result, proof_patch=proof_patch, label=label,
            on_step=on_step)
        agent_runs += spent
        if ending:
            return finish(ending, detail)
        return finish("fixed", f"{len(agreed)} of {len(live)} fixes pass all {len(repros)} "
                               "reproductions; the smallest clears the host's checks and the "
                               "scope-safety review")
    except _LANE_ERRORS as e:
        return finish(*_faulted(e))


def _restore(result: dict) -> list[Candidate]:
    """The candidates a finished run recorded, rebuilt from its result."""
    patches = {c["index"]: c for c in result.get("candidate_patches") or []}
    out: list[Candidate] = []
    for s in result.get("candidates") or []:
        p = patches.get(s["index"]) or {}
        out.append(Candidate(
            index=s["index"], model=s.get("model") or "", ending=s.get("ending"),
            detail=s.get("detail") or "", verdict=p.get("verdict") or {},
            test_patch=p.get("test_patch") or "", fix_patch=p.get("fix_patch") or "",
            test_paths=list(p.get("test_paths") or []), checks=list(p.get("checks") or []),
            reproduces=bool(s.get("reproduces")),
            passes={int(k): bool(v) for k, v in (s.get("passes") or {}).items()},
            pinned={int(k): list(v) for k, v in (s.get("pinned") or {}).items()}))
    return out


def judge_reading(spec: fix_lane.LaneSpec, *, workdir: Path, result: dict, reading: list[int],
                  on_step: Callable[[str], None] = lambda step: None) -> fix_lane.LaneResult:
    """Resume a `fix-disputed` run on one reading, chosen by an answer to the
    question it raised: the smallest fix among `reading`'s candidates that
    passes every reproduction in it goes through the host's checks and the
    scope-safety review. `result` is the disputed run's own; the candidates'
    cross-test results are read from it, never re-run."""
    label = f"cross-{spec.issue}"
    result = {**result, "proof": {}, "reviews": [], "patch": "", "reading": reading}
    agent_runs = 0
    reproduction: dict | None = None

    def finish(ending: str, detail: str) -> fix_lane.LaneResult:
        return fix_lane.LaneResult(ending=ending, fault=ending in fix_lane.ENDINGS_FAULT,
                                   detail=detail, reproduction=reproduction, result=result,
                                   agent_runs=agent_runs)

    group = [c for c in _restore(result) if c.index in reading and c.ending is None]
    repros = [c for c in group if c.reproduces]
    fits = [c for c in group if c.verdict and all(c.passes.get(r.index) for r in repros)]
    if not repros or not fits:
        return finish("fix-unproven", "the chosen reading has no fix that passes its "
                                      "reproductions")
    workdir.mkdir(parents=True, exist_ok=True)
    pick = min(fits, key=lambda c: (issue_gates.changed_line_count(c.fix_patch), c.index))
    try:
        ending, detail, reproduction, agent_runs = _judge_pick(
            spec, workdir, pick, repros, result, proof_patch=_proof_patcher(spec, label),
            label=label, on_step=on_step)
    except _LANE_ERRORS as e:
        return finish(*_faulted(e))
    if ending:
        return finish(ending, detail)
    return finish("fixed", f"the answer chose reading {reading}; its smallest fix passes its "
                           f"{len(repros)} reproduction(s) and clears the host's checks and the "
                           "scope-safety review")
