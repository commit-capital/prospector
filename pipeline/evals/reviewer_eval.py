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

import contextlib
import gzip
import json
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pipeline import (diff_cache, reviewers, settings, settings_registry, storekit,
                      verdict_labels, verdict_log)
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
        lane_tree.apply_patch(tree, case.inputs.get("worktree_patch") or case.inputs["patch"])
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
            except ValueError:
                return "failed"
    if reviewer == "fix-review":
        from issue_triage import review_issue_fix
        title, body = _report(inputs)
        with _worktree(repo, case) as (_, tree):
            return review_issue_fix.verdict_of(review_issue_fix.full_review(
                str(tree), inputs["patch"], title=title, body=body,
                root_cause=inputs.get("root_cause", ""),
                test_paths=[p for p in inputs.get("test_paths", "").split("\n") if p],
                evidence=inputs.get("evidence", ""),
                inventory_veto=inputs.get("inventory_veto", "1") == "1",
                history=inputs.get("history") or None))
    if reviewer == "trust-boundary":
        from issue_triage import trust_boundary
        title, body = _report(inputs)
        with _worktree(repo, case) as (base, tree):
            return trust_boundary.verdict_of(trust_boundary.judge(
                str(tree), base, inputs["patch"], title=title, body=body))
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
