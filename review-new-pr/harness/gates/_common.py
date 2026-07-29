"""Shared utilities for harness gates.

Every gate is a function that takes a PR context and returns a verdict dict.
Verdicts are merged into the per-PR verdict JSON by the runner.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import json
import os


def default_branch() -> str:
    """Default branch of the reviewed repository, used when a PR payload carries
    no base ref: TRIAGE_DEFAULT_BRANCH when set, else "main". The harness runs
    standalone under bare python3, so this reads the environment directly rather
    than pipeline.settings."""
    return os.environ.get("TRIAGE_DEFAULT_BRANCH") or "main"


def _profile_doc() -> tuple[dict, Path] | None:
    """The parsed repository-profile JSON named by TRIAGE_PROFILE (path relative
    to the repo root) and its resolved path, or None with no profile configured.
    Malformed or unreadable files fail loud (SystemExit). The harness reads only
    the keys it consumes; pipeline/profile.py is the strict validator of the
    whole file."""
    raw = os.environ.get("TRIAGE_PROFILE", "")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[3] / path
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(f"TRIAGE_PROFILE {path}: file not found")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"TRIAGE_PROFILE {path}: unreadable: {exc}")
    if not isinstance(doc, dict):
        raise SystemExit(f"TRIAGE_PROFILE {path}: top level: must be a JSON object")
    return doc, path


def trusted_authors() -> frozenset[str]:
    """The profile's trusted_authors (repository maintainers) as a set; empty
    with no profile or no such key. Wrong-typed values fail loud (SystemExit)."""
    loaded = _profile_doc()
    if loaded is None:
        return frozenset()
    doc, path = loaded
    value = doc.get("trusted_authors")
    if value is None:
        return frozenset()
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise SystemExit(f"TRIAGE_PROFILE {path}: trusted_authors: must be a list of strings")
    return frozenset(value)


def template_sections() -> tuple[list[str], list[str]]:
    """(required, recommended) PR-template sections from the repository profile.
    No profile or no harness section means no template policy — the gate passes
    trivially. Absent keys default to empty; wrong-typed values fail loud
    (SystemExit)."""
    loaded = _profile_doc()
    if loaded is None:
        return [], []
    doc, path = loaded
    harness = doc.get("harness")
    if harness is None:
        return [], []
    if not isinstance(harness, dict):
        raise SystemExit(f"TRIAGE_PROFILE {path}: harness: must be an object")
    tmpl = harness.get("pr_template")
    if tmpl is None:
        return [], []
    if not isinstance(tmpl, dict):
        raise SystemExit(f"TRIAGE_PROFILE {path}: harness.pr_template: must be an object")
    out: list[list[str]] = []
    for key in ("required_sections", "recommended_sections"):
        value = tmpl.get(key)
        if value is None:
            out.append([])
            continue
        if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
            raise SystemExit(
                f"TRIAGE_PROFILE {path}: harness.pr_template.{key}: must be a list of strings")
        out.append(list(value))
    return out[0], out[1]


Verdict = Literal["pass", "fail", "uncertain"]
Severity = Literal["low", "medium", "high", "critical"]


@dataclass
class PRContext:
    """All inputs a gate may consult about one PR.

    Gates SHOULD NOT reach out beyond this struct — keeps the trust boundary
    explicit and lets the runner mock inputs for tests.
    """
    number: int
    title: str
    body: str
    author: str
    head_sha: str
    base_ref: str
    additions: int | None
    deletions: int | None
    changed_files: int | None
    files: list[dict] = field(default_factory=list)  # [{filename, status, additions, deletions, patch}]
    updated_at: str = ""
    draft: bool = False

    @classmethod
    def from_caches(cls, pr_num: int, prs_cache_path: str, diffs_dir: str) -> PRContext:
        """Build a PRContext from the local caches we already have."""
        with open(prs_cache_path) as f:
            for line in f:
                p = json.loads(line)
                if p["number"] == pr_num:
                    break
            else:
                raise ValueError(f"PR #{pr_num} not in {prs_cache_path}")

        files = []
        try:
            with open(f"{diffs_dir}/{pr_num}.json") as f:
                files = json.load(f)
        except FileNotFoundError:
            pass  # PR may genuinely have no cached diff; gate handles None

        return cls(
            number=p["number"],
            title=p.get("title") or "",
            body=p.get("body") or "",
            author=p.get("author") or "",
            head_sha=p.get("head_sha") or "",
            base_ref=p.get("base") or default_branch(),
            additions=p.get("additions"),
            deletions=p.get("deletions"),
            changed_files=p.get("changed_files"),
            files=files,
            updated_at=p.get("updated_at") or "",
            draft=bool(p.get("draft")),
        )


def gate_result(
    verdict: Verdict,
    *,
    severity: Severity | None = None,
    evidence: str = "",
    details: dict | None = None,
) -> dict:
    """Standard shape for every gate's return value."""
    result: dict = {"verdict": verdict}
    if severity is not None:
        result["severity"] = severity
    if evidence:
        result["evidence"] = evidence
    if details:
        result["details"] = details
    return result
