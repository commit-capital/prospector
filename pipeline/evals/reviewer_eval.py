"""The reviewer eval gate (docs/superpowers/specs/2026-10-23-reviewer-eval-ratchet-design.md):
replay Prospector's own reviewers on their labeled runs, offline, and score them.

A case is a labeled `review_verdicts` row with its recorded inputs. `replay`
rebuilds what the reviewer read — a tree at the run's base from the eval's own
clone of TRIAGE_REPO (`EvalRepo`), the PR diff by head SHA from the store, the
worktree patch a fix review saw — and calls the reviewer with no subject, so
nothing is recorded. A case is `partial` when the row cannot be rebuilt as the
reviewer saw it: a run recorded before its worktree patch was, a backfilled
run, or security, whose lenses read live upstream.
"""
from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from pipeline import (diff_cache, headless_agent, reviewers, settings, settings_registry,
                      storekit, verdict_labels, verdict_log)
from pipeline.store import Store
from pipeline.storekit import VerdictRecord


class Unreplayable(RuntimeError):
    """The case's base or diff is not available to rebuild."""


@dataclass(frozen=True)
class Case:
    row: VerdictRecord
    inputs: dict[str, str]
    fidelity: Literal["full", "partial"]
    why_partial: str | None


def repo_url() -> str:
    return f"https://github.com/{settings.repo()}.git"


def _partial(row: VerdictRecord, inputs: dict[str, str]) -> str | None:
    if row.prompt_sha == "backfill":
        return "backfilled from a fix attempt that ran before capture"
    if row.reviewer == "security":
        return "the lenses read live upstream"
    if row.reviewer in ("fix-review", "trust-boundary") and not inputs.get("worktree_patch"):
        return "recorded before the worktree patch was"
    return None


def load_cases(store: Store, reviewer: str) -> list[Case]:
    """`reviewer`'s labeled PR and issue runs whose inputs are still stored."""
    out = []
    for row in store.review_verdicts(reviewer=reviewer, labeled=True):
        blob = store.review_verdict_inputs(row.id) if row.subject_kind in ("pr", "issue") else None
        if blob is None:
            continue
        inputs = json.loads(gzip.decompress(blob))
        why = _partial(row, inputs)
        out.append(Case(row=row, inputs=inputs, fidelity="partial" if why else "full",
                        why_partial=why))
    return out


class EvalRepo:
    """The eval's own clone of TRIAGE_REPO at `root`, fetched on demand, from
    which `tree` checks out a detached worktree at any base SHA."""

    def __init__(self, root: Path):
        self.root = root

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(self.root), *args], check=check,
                              capture_output=True, text=True)

    def _has(self, sha: str) -> bool:
        return self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    @contextlib.contextmanager
    def tree(self, sha: str) -> Iterator[Path]:
        if not (self.root / ".git").exists():
            self.root.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "clone", "-q", "--no-checkout", repo_url(), str(self.root)],
                           check=True, capture_output=True, text=True)
        self._git("worktree", "prune")
        if not self._has(sha):
            self._git("fetch", "-q", "origin", check=False)
            if not self._has(sha):
                raise Unreplayable(f"{sha} is not in {repo_url()}")
        with tempfile.TemporaryDirectory(prefix="reviewer-eval-") as tmp:
            dest = Path(tmp) / "tree"
            self._git("worktree", "add", "-q", "--detach", str(dest), sha)
            try:
                yield dest
            finally:
                self._git("worktree", "remove", "--force", str(dest), check=False)


def _report(inputs: dict[str, str]) -> tuple[str, str]:
    if "title" in inputs:
        return inputs["title"], inputs.get("body", "")
    report = json.loads(inputs.get("report") or "{}")
    return str(report.get("title") or ""), str(report.get("body") or "")


@contextlib.contextmanager
def _worktree(repo: EvalRepo, case: Case) -> Iterator[tuple[Path, Path]]:
    """The base tree and, beside it, the materialized tree the reviewer read."""
    from issue_triage import lane_tree
    if not case.row.base_sha:
        raise Unreplayable("the run recorded no base")
    with repo.tree(case.row.base_sha) as base, tempfile.TemporaryDirectory() as tmp:
        tree = lane_tree.materialize(base, Path(tmp) / "src")
        try:
            lane_tree.apply_patch(tree, case.inputs.get("worktree_patch") or case.inputs["patch"])
        except ValueError as e:
            raise Unreplayable(str(e)) from e
        yield base, tree


def _diff(store: Store, case: Case) -> str:
    diff = store.load_diff(case.row.head_sha or "")
    if diff is None:
        raise Unreplayable(f"no stored diff for head {case.row.head_sha}")
    return diff


def replay(case: Case, repo: EvalRepo, store: Store) -> str:
    """The reviewer's verdict on `case` under the current code; never recorded."""
    reviewer, inputs, row = case.row.reviewer, case.inputs, case.row
    if reviewer == "intake":
        from issue_triage import intake_audit
        blocked = inputs.get("blocked") == "1" or bool(row.data.get("blocked"))
        return intake_audit.verdict_of(intake_audit.judge(
            inputs["title"], inputs["body"], inputs.get("notes") or None, blocked=blocked))
    if reviewer == "fixability":
        from issue_triage import fixability
        if not row.base_sha:
            raise Unreplayable("the run recorded no base")
        with repo.tree(row.base_sha) as tree:
            try:
                return str(fixability.review_rendered(tree, title=inputs["title"],
                                                      body=inputs["body"],
                                                      replies_block=inputs.get("replies", ""))["verdict"])
            except (ValueError, RuntimeError) as e:
                if isinstance(e, (headless_agent.AgentUnavailable, headless_agent.AgentDeclined,
                                  headless_agent.AgentTransient)):
                    raise
                return "failed"
    if reviewer == "fix-review":
        from issue_triage import review_issue_fix
        title, body = _report(inputs)
        with _worktree(repo, case) as (_, tree):
            return review_issue_fix.verdict_of(review_issue_fix.full_review(
                str(tree), inputs["patch"], title=title, body=body,
                approved=inputs.get("approved") or None,
                root_cause=inputs.get("root_cause", ""),
                test_paths=[p for p in inputs.get("test_paths", "").split("\n") if p],
                evidence=inputs.get("evidence", ""),
                inventory_veto=inputs.get("inventory_veto", "1") == "1",
                history=inputs.get("history") or None,
                alternatives=inputs.get("alternatives") or ()))
    if reviewer == "trust-boundary":
        from issue_triage import trust_boundary
        title, body = _report(inputs)
        with _worktree(repo, case) as (base, tree):
            return trust_boundary.verdict_of(trust_boundary.judge(
                str(tree), base, inputs["patch"], title=title, body=body,
                approved=inputs.get("approved") or None))
    if reviewer == "design":
        from pipeline import design_review
        diff = _diff(store, case)
        if not row.base_sha:
            raise Unreplayable("the run recorded no base")
        with repo.tree(row.base_sha) as tree, tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"{row.head_sha}.diff"
            path.write_text(diff)
            try:
                entry = design_review.rendered_entry(
                    tree, path, pr=row.subject, head_sha=row.head_sha or "",
                    base_sha=row.base_sha, coverage_record=None, inputs=inputs)
            except design_review.DesignReviewFailed:
                return "failed"
        passed = reviewers._design_bar(entry, row.head_sha).status == reviewers.PASS
        return "pass" if passed else "fail"
    if reviewer == "security":
        from pipeline import security_review
        path = diff_cache.DIFFS / f"{row.head_sha}.diff"
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_diff(store, case))
        reviewed = security_review.review_pr(row.subject, inputs.get("title", ""),
                                             row.head_sha or "",
                                             bot_evidence=json.loads(inputs.get("bot_evidence") or "[]"))
        if reviewed is None:
            raise Unreplayable("the security review could not read the diff")
        return reviewed[0].verdict
    raise ValueError(f"no replay for {reviewer!r}")


EVAL_CASE = "reviewer-eval:case"


@dataclass(frozen=True)
class Score:
    cases: int = 0
    must_catch: int = 0
    should_pass: int = 0
    right: int = 0
    under: int = 0
    over: int = 0
    failed: int = 0
    partial: int = 0
    unreplayable: int = 0


def ledger_samples(store: Store) -> dict[tuple[int, str, str], list[str]]:
    """Every eval sample in the ledger, by (run id, prompt_sha, model)."""
    out: dict[tuple[int, str, str], list[str]] = {}
    for run in store.runs(phase=EVAL_CASE):
        stats = (run.raw.get("stats") or {}) if isinstance(run, storekit.PhaseRun) else {}
        if stats:
            key = (int(stats["verdict_id"]), str(stats["prompt_sha"]), str(stats["model"]))
            out.setdefault(key, []).append(str(stats["verdict"]))
    return out


def samples(case: Case, prompt_sha: str, model: str,
            ledger: dict[tuple[int, str, str], list[str]]) -> list[str]:
    """`case`'s verdicts under one configuration: the production run's own when
    it ran that configuration, then every eval sample of it."""
    own = [case.row.verdict] if (case.row.prompt_sha, case.row.model) == (prompt_sha, model) else []
    return own + ledger.get((case.row.id, prompt_sha, model), [])


def add_sample(store: Store, case: Case, prompt_sha: str, model: str, verdict: str) -> None:
    now = storekit.now()
    store.append_run({"phase": EVAL_CASE, "started": now, "finished": now,
                      "stats": {"verdict_id": case.row.id, "reviewer": case.row.reviewer,
                                "prompt_sha": prompt_sha, "model": model, "verdict": verdict,
                                "fidelity": case.fidelity}})


def majority(reviewer: str, verdicts: list[str]) -> str | None:
    """The most common judging verdict, a tie going to the most severe; the
    failure value only when nothing judged."""
    order = verdict_log.VERDICTS[reviewer]
    judged = [v for v in verdicts if v != order[-1]]
    if not judged:
        return verdicts[0] if verdicts else None
    return max(set(judged), key=lambda v: (judged.count(v), order.index(v)))


def score(results: dict[int, str], cases: list[Case]) -> Score:
    counts: dict[str, int] = {"cases": len(cases)}

    def add(key: str) -> None:
        counts[key] = counts.get(key, 0) + 1

    for case in cases:
        label, reviewer = case.row.label or {}, case.row.reviewer
        add("must_catch" if verdict_labels.blocks(reviewer, label["expected"]) else "should_pass")
        if case.fidelity == "partial":
            add("partial")
        verdict = results.get(case.row.id)
        if verdict is None:
            add("unreplayable")
        elif verdict == verdict_log.VERDICTS[reviewer][-1]:
            add("failed")
        else:
            add(verdict_labels.call(label, verdict, reviewer))
    return Score(**counts)


PINS = Path(__file__).resolve().parent / "data" / "reviewer_pins.json"


def configs() -> dict[str, dict[str, str]]:
    """Each reviewer's prompt and default model as the code sets them, ignoring
    the environment: the configuration a pin is checked against."""
    from issue_triage import fixability, intake_audit, review_issue_fix, trust_boundary
    from pipeline import design_review, security_review
    agent = str(settings_registry.BY_NAME["TRIAGE_AGENT_MODEL"].default)
    return {
        "design": {"prompt_sha": design_review.PROMPT_SHA, "model": agent},
        "security": {"prompt_sha": security_review.PROMPT_SHA, "model": agent},
        # The review models default to the agent model.
        "fix-review": {"prompt_sha": review_issue_fix.PROMPT_SHA, "model": agent},
        "trust-boundary": {"prompt_sha": trust_boundary.PROMPT_SHA, "model": agent},
        "intake": {"prompt_sha": intake_audit.PROMPT_SHA, "model": agent},
        "fixability": {"prompt_sha": fixability.PROMPT_SHA, "model": fixability.MODEL},
    }


def init_pins(path: Path = PINS) -> None:
    """Pin every reviewer without a pin to the code's configuration, provisionally."""
    pins = json.loads(path.read_text()) if path.exists() else {}
    for reviewer, config in configs().items():
        pins.setdefault(reviewer, {**config, "provisional": True, "score": None, "run": None})
    path.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")


EVAL_RUN = "reviewer-eval:run"
REPO_ROOT = Path(__file__).resolve().parents[2]


class PinUnreplayable(RuntimeError):
    """No code on main ran the pinned configuration, so its side cannot replay."""


@dataclass(frozen=True)
class GateResult:
    reviewer: str
    pinned: Score
    candidate: Score
    passed: bool
    provisional: bool
    reasons: list[str]


@contextlib.contextmanager
def _agent_model(model: str) -> Iterator[None]:
    """Run every agent on `model`, with usage bookkept in a throwaway store; the
    review models follow the agent model."""
    from pipeline.evals import providers
    saved = {k: os.environ.get(k) for k in ("TRIAGE_AGENT_MODEL", "TRIAGE_ISSUE_FIX_REVIEW_MODELS")}
    os.environ["TRIAGE_AGENT_MODEL"] = model
    os.environ.pop("TRIAGE_ISSUE_FIX_REVIEW_MODELS", None)
    try:
        with providers.provider("claude", codex_bin="", model=None):
            yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def eval_repo() -> EvalRepo:
    return EvalRepo(settings.verify_scratch() / "reviewer-eval" / "repo")


def replay_current(case: Case, store: Store, model: str) -> str:
    with _agent_model(model):
        return replay(case, eval_repo(), store)


UNREPLAYABLE_EXIT = 3


def _main_pins(rev: str) -> dict:
    shown = subprocess.run(["git", "-C", str(REPO_ROOT), "show",
                            f"{rev}:{PINS.relative_to(REPO_ROOT)}"],
                           capture_output=True, text=True, check=False)
    return json.loads(shown.stdout) if shown.returncode == 0 and shown.stdout else {}


def pinned_commit(reviewer: str, pin: dict) -> str | None:
    """The newest main commit whose pins file pins `reviewer` to `pin`'s prompt
    and model: its code ran them, since CI keeps every main commit's code equal
    to its pins."""
    subprocess.run(["git", "-C", str(REPO_ROOT), "fetch", "-q", "origin", "main"],
                   capture_output=True, check=False)
    want = (pin["prompt_sha"], pin["model"])
    commits = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-list", "origin/main", "--",
                              str(PINS.relative_to(REPO_ROOT))],
                             capture_output=True, text=True, check=False).stdout.split()
    # Newest first: the pin held from the commit that set it until the next one
    # that changed the file, so the newest code it ran is just before that.
    for i, commit in enumerate(commits):
        entry = _main_pins(commit).get(reviewer) or {}
        if (entry.get("prompt_sha"), entry.get("model")) == want:
            if i == 0:
                return _rev("origin/main")
            return _rev(f"{commits[i - 1]}^")
    return None


def _rev(name: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", name],
                          capture_output=True, text=True, check=True).stdout.strip()


@contextlib.contextmanager
def pinned_runner(reviewer: str, pin: dict, store: Store,
                  commit: str | None = None) -> Iterator[Callable[[Case], str]]:
    """Replays under the code main ran for `pin`, from one checkout of it, each
    case in a child process reading this process's store."""
    commit = commit or pinned_commit(reviewer, pin)
    if commit is None:
        raise PinUnreplayable(f"no main commit pins {reviewer} to {pin['prompt_sha']}/"
                              f"{pin['model']}; remove that pin and run --init")
    env = {**os.environ, "TRIAGE_STORE_URL": store.engine.url.render_as_string(hide_password=False)}
    with pinned_checkout(commit) as wt:
        def run(case: Case) -> str:
            done = subprocess.run(
                ["uv", "run", "--project", str(wt), "python", "-m", "pipeline.evals.reviewer_eval",
                 "replay", "--verdict-id", str(case.row.id), "--model", pin["model"]],
                cwd=wt, env=env, capture_output=True, text=True, check=False)
            if done.returncode == UNREPLAYABLE_EXIT:
                raise Unreplayable(done.stderr.strip()[-400:])
            lines = done.stdout.strip().splitlines()
            if done.returncode != 0 or not lines:
                raise RuntimeError(f"pinned replay of run {case.row.id} failed: "
                                   f"{done.stderr[-400:]}")
            return lines[-1].strip()
        yield run


@contextlib.contextmanager
def pinned_checkout(commit: str) -> Iterator[Path]:
    """A detached worktree of this repository at `commit`, removed on exit."""
    git = ["git", "-C", str(REPO_ROOT), "worktree"]
    subprocess.run([*git, "prune"], capture_output=True, check=False)
    with tempfile.TemporaryDirectory(prefix="eval-pin-") as tmp:
        wt = Path(tmp) / "code"
        subprocess.run([*git, "add", "-q", "--detach", str(wt), commit], check=True,
                       capture_output=True)
        try:
            yield wt
        finally:
            subprocess.run([*git, "remove", "--force", str(wt)], capture_output=True, check=False)


def gate(store: Store, reviewer: str, *, k: int = 3, pins_path: Path = PINS,
         replay_candidate: Callable[[Case], str] | None = None,
         replay_pinned: Callable[[Case], str] | None = None) -> GateResult:
    """Score `reviewer`'s labeled cases under its pin and under the code's
    configuration, sampling each side until it has a verdict and, where the two
    disagree, `k`. The candidate passes when, on no case the pin called right,
    it under-calls or fails to judge, and it over-calls no more often. With
    fewer than MIN_CASES replayed must-catch or should-pass cases the gate is
    provisional and passes, reporting what it would have refused."""
    pin = json.loads(pins_path.read_text())[reviewer]
    cand = configs()[reviewer]
    failure = verdict_log.VERDICTS[reviewer][-1]
    run_cand = replay_candidate or (lambda c: replay_current(c, store, cand["model"]))
    cases = load_cases(store, reviewer)
    ledger = ledger_samples(store)
    results: dict[str, dict[int, str]] = {"pinned": {}, "candidate": {}}
    with contextlib.ExitStack() as stack:
        pinned_run: list[Callable[[Case], str]] = [replay_pinned] if replay_pinned else []

        def run_pin(case: Case) -> str:
            if not pinned_run:  # the pinned checkout is made once, when first needed
                pinned_run.append(stack.enter_context(pinned_runner(reviewer, pin, store)))
            return pinned_run[0](case)

        def top_up(case: Case, side: str, want: int) -> str | None:
            config, run = (pin, run_pin) if side == "pinned" else (cand, run_cand)
            got = samples(case, config["prompt_sha"], config["model"], ledger)
            while len(got) < want:
                verdict = run(case)
                add_sample(store, case, config["prompt_sha"], config["model"], verdict)
                ledger.setdefault((case.row.id, config["prompt_sha"], config["model"]),
                                  []).append(verdict)
                got.append(verdict)
            return majority(reviewer, got)

        for case in cases:
            try:
                p, c = top_up(case, "pinned", 1), top_up(case, "candidate", 1)
                if p != c:
                    p, c = top_up(case, "pinned", k), top_up(case, "candidate", k)
            except Unreplayable:
                continue
            if p is not None and c is not None:
                results["pinned"][case.row.id], results["candidate"][case.row.id] = p, c
    pinned_score, cand_score = score(results["pinned"], cases), score(results["candidate"], cases)
    reasons = []
    for case in cases:
        label = case.row.label or {}
        p, c = results["pinned"].get(case.row.id), results["candidate"].get(case.row.id)
        if not (p and c) or p == failure or verdict_labels.call(label, p, reviewer) != "right":
            continue
        if c == failure or verdict_labels.call(label, c, reviewer) == "under":
            reasons.append(f"run {case.row.id} ({case.row.subject_kind} #{case.row.subject}): "
                           f"the pin said {p}, right; the candidate "
                           + ("failed to judge" if c == failure else f"says {c}"))
    if cand_score.over > pinned_score.over:
        reasons.append(f"{cand_score.over} over-calls against the pin's {pinned_score.over}")
    replayed = [c for c in cases if c.row.id in results["candidate"]]
    catch = sum(1 for c in replayed
                if verdict_labels.blocks(reviewer, (c.row.label or {})["expected"]))
    provisional = (catch < verdict_labels.MIN_CASES
                   or len(replayed) - catch < verdict_labels.MIN_CASES)
    return GateResult(reviewer=reviewer, pinned=pinned_score, candidate=cand_score,
                      passed=provisional or not reasons, provisional=provisional, reasons=reasons)


def write_pin(reviewer: str, result: GateResult, pins_path: Path = PINS) -> None:
    pins = json.loads(pins_path.read_text())
    pins[reviewer] = {**configs()[reviewer], "provisional": result.provisional,
                      "score": asdict(result.candidate), "run": storekit.now()}
    pins_path.write_text(json.dumps(pins, indent=2, sort_keys=True) + "\n")


def _summary(result: GateResult) -> str:
    lines = [f"{result.reviewer}: {'PASS' if result.passed else 'REFUSED'}"
             + (" (provisional)" if result.provisional else ""),
             f"  pinned:    {asdict(result.pinned)}", f"  candidate: {asdict(result.candidate)}"]
    lines += [f"  - {r}" for r in result.reasons]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.evals.reviewer_eval")
    ap.add_argument("--init", action="store_true", help="pin every unpinned reviewer, provisionally")
    ap.add_argument("--reviewer", choices=tuple(configs()))
    ap.add_argument("--live", action="store_true", help="run the gate, calling agents")
    ap.add_argument("--k", type=int, default=3, help="samples per side where they disagree")
    sub = ap.add_subparsers(dest="cmd")
    one = sub.add_parser("replay", help="print one run's verdict under this checkout's code")
    one.add_argument("--verdict-id", type=int, required=True)
    one.add_argument("--model", required=True)
    args = ap.parse_args(argv)
    if args.init:
        init_pins()
        return 0
    store = Store()
    if args.cmd == "replay":
        row = store.review_verdict(args.verdict_id)
        case = next((c for c in load_cases(store, row.reviewer) if c.row.id == row.id), None) if row else None
        if case is None:
            print(f"no labeled run {args.verdict_id} with inputs", file=sys.stderr)
            return 2
        try:
            print(replay_current(case, store, args.model))
        except Unreplayable as e:
            print(str(e), file=sys.stderr)
            return UNREPLAYABLE_EXIT
        return 0
    if not (args.reviewer and args.live):
        ap.error("give --init, replay, or --reviewer X --live")
    started = storekit.now()
    result = gate(store, args.reviewer, k=args.k)
    store.append_run({"phase": EVAL_RUN, "started": started, "finished": storekit.now(),
                      "stats": {"reviewer": args.reviewer, "passed": result.passed,
                                "provisional": result.provisional,
                                "pinned": asdict(result.pinned),
                                "candidate": asdict(result.candidate), "reasons": result.reasons}})
    print(_summary(result))
    if result.passed:
        write_pin(args.reviewer, result)
        print(f"re-pinned {args.reviewer}; commit {PINS.relative_to(REPO_ROOT)}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
