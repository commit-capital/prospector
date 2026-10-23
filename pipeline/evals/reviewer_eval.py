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

from pipeline import diff_cache, reviewers, settings
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
