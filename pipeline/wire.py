"""Transient wire-structs the pipeline drivers pass between each other (and hand
to the JS workflows as JSON), distinct from the persisted Pr/Cluster domain model
in model.py. These types own the fixed shapes that were previously bare dicts
built at scattered, duplicated call sites; `to_dict()` is the single boundary
where a struct becomes the JSON a workflow batch file or a CLI manifest carries,
so the wire format stays byte-identical to what the JS workflows already read.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

if TYPE_CHECKING:
    from pipeline.model import Pr


@dataclass(frozen=True)
class DiffManifestItem:
    """One PR a wave/eligibility pass selects for diff-grounded work — enough to
    fetch the diff and label it. Produced identically by the CLUSTER wave and the
    SECURITY eligibility pass; serialized into the summarize workflow's per-batch
    files and the `wave` CLI manifest, both of which the JS reads as
    {pr, head_sha, title, diff_path}."""
    pr: int
    head_sha: str | None
    title: str | None
    diff_path: str

    @classmethod
    def for_pr(cls, pr: int, rec: Pr, diffs_dir: Path) -> DiffManifestItem:
        return cls(pr=pr, head_sha=rec.head_sha, title=rec.title,
                   diff_path=str(diffs_dir / f"{rec.head_sha}.diff"))

    def to_dict(self) -> dict[str, object]:
        return {"pr": self.pr, "head_sha": self.head_sha,
                "title": self.title, "diff_path": self.diff_path}


@dataclass(frozen=True)
class SummaryEntry:
    """The mechanism-level projection of a PR's summary section that the cluster,
    assign, and analyze workflows compare PRs on. from_pr() owns the one-place
    projection logic — the `.get` defaults and the primary_change↦one_liner
    fallback that the CLUSTER unit files and the ANALYZE bundle both need. The
    cluster-unit JSON is to_dict(); the bundle member embeds these fields among
    its own (signals, drift, issues, diff_path)."""
    pr: int
    title: str | None
    one_liner: str | None
    mechanism: str | None
    identifiers: list[str]
    paths: list[str]
    primary_change: str | None
    secondary_changes: list[str]

    @classmethod
    def from_pr(cls, pr: int, rec: Pr, summary: dict) -> SummaryEntry:
        return cls(
            pr=pr,
            title=rec.title,
            one_liner=summary.get("one_liner"),
            mechanism=summary.get("mechanism"),
            identifiers=summary.get("identifiers", []),
            paths=summary.get("paths", []),
            primary_change=summary.get("primary_change") or summary.get("one_liner"),
            secondary_changes=summary.get("secondary_changes", []),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "pr": self.pr, "title": self.title, "one_liner": self.one_liner,
            "mechanism": self.mechanism, "identifiers": self.identifiers,
            "paths": self.paths, "primary_change": self.primary_change,
            "secondary_changes": self.secondary_changes,
        }


class Finding(TypedDict):
    """A non-green security finding a review lens emits and the verifier upholds.
    The agent's schema mandates severity/category/title/detail; location and
    confidence are optional, and the pipeline preserves whatever else it carries
    (e.g. lens) and stores it verbatim — so this documents the contract the
    cockpit reads by key without closing the shape (extra keys survive at
    runtime)."""
    severity: str
    category: str
    title: str
    detail: str
    location: NotRequired[str]
    confidence: NotRequired[str]
    lens: NotRequired[str]


@dataclass(frozen=True, kw_only=True)
class VerdictItem:
    """One PR's security verdict ready for security_driver.commit_verdicts — the
    adversarial review's outcome plus the confirmed findings. The producer
    (security_review.review_pr) constructs it directly; from_dict() adapts the
    per-PR JSON the security workflow writes, which is a SUPERSET of these fields
    (it also carries lenses_ok/lenses_total diagnostics) and omits tier — so a
    plain VerdictItem(**d) would raise on the extra keys. from_dict selects the
    fields it owns and defaults tier."""
    pr: int
    head_sha: str | None
    verdict: str
    findings: list[Finding]
    tier: str = "adversarial"

    @classmethod
    def from_dict(cls, d: dict) -> VerdictItem:
        return cls(
            pr=d["pr"],
            head_sha=d.get("head_sha"),
            verdict=d.get("verdict", ""),
            findings=d.get("findings") or [],
            tier=d.get("tier", "adversarial"),
        )

    def to_dict(self) -> dict[str, object]:
        return {"pr": self.pr, "head_sha": self.head_sha, "verdict": self.verdict,
                "findings": self.findings, "tier": self.tier}


@dataclass(frozen=True, kw_only=True)
class BlindItem:
    """One PR's blind adequacy verdict — Signal 1, judged from the diff and the
    linked issue before any sandbox boots, and committed to the store in that state.

    `test_cmd` is the command that exercises the claimed defect (the author's test,
    or a linked issue's repro steps when the PR ships none); it drives red->green.
    `repro_command` is the agent's own independent repro (Signal 4), run against the
    base only and recorded as corroborating evidence.
    `expected_red_signature` is the pre-committed prediction Signal 3 is checked
    against, so a red for the wrong reason cannot be rationalized backward.
    `expected_repro_signature` is the same prediction for `repro_command`, checked
    by Signal 4's own reason-check (the judge's `repro_reason_match`) — so a repro
    that exits non-zero for an unrelated reason (a timeout, an import error) is not
    mistaken for corroboration.
    `repro_rejected` is the DRIVER's record of a pre-run repro rejection — the
    reason a structurally vacuous `repro_command` was nulled before commit. It is
    set only by the driver (dataclasses.replace after vetting); from_dict never
    reads it from agent output."""
    pr: int
    head_sha: str | None
    has_test: bool
    faithful: bool
    reasoning: str
    test_cmd: str | None = None
    repro_command: str | None = None
    from_linked_issue: bool = False
    requires_live_agent: bool = False
    confidence: str = "medium"
    claimed_symptom: str | None = None
    expected_red_signature: str | None = None
    expected_repro_signature: str | None = None
    repro_rejected: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> BlindItem:
        # Every boolean field is accepted ONLY as an actual bool — `is True`,
        # never bool(...). An agent that emits the string "false" would coerce
        # to True under bool(), and for `faithful` that silently flips an
        # unfaithful verdict to faithful, disarming the escalate anchor. A
        # malformed value fails toward the safe reading (not faithful → the
        # blind verdict routes to escalate/human, never to verified-fix),
        # matching the isinstance-bool guard the live queue path already applies.
        return cls(
            pr=d["pr"], head_sha=d.get("head_sha"),
            has_test=d.get("has_test") is True, faithful=d.get("faithful") is True,
            reasoning=d.get("reasoning", ""), test_cmd=d.get("test_cmd") or None,
            repro_command=d.get("repro_command") or None,
            from_linked_issue=d.get("from_linked_issue") is True,
            requires_live_agent=d.get("requires_live_agent") is True,
            confidence=d.get("confidence", "medium"),
            claimed_symptom=d.get("claimed_symptom"),
            expected_red_signature=d.get("expected_red_signature"),
            expected_repro_signature=d.get("expected_repro_signature"),
        )

    def to_signal(self) -> dict[str, object]:
        """The `signals.blind_adequacy` shape stored on the PR. Routing fields
        (pr/head_sha) belong to the envelope, not the signal."""
        return {"has_test": self.has_test, "faithful": self.faithful,
                "confidence": self.confidence, "claimed_symptom": self.claimed_symptom,
                "expected_red_signature": self.expected_red_signature,
                "requires_live_agent": self.requires_live_agent,
                "test_cmd": self.test_cmd, "repro_command": self.repro_command,
                "expected_repro_signature": self.expected_repro_signature,
                "repro_rejected": self.repro_rejected,
                "from_linked_issue": self.from_linked_issue,
                "reasoning": self.reasoning}


@dataclass(frozen=True, kw_only=True)
class JudgeItem:
    """One PR's post-run judgment — Signal 3's red-reason rating, Signal 4's
    repro-reason rating (when a repro ran), plus findings prose.

    `repro_reason_match` is informational, like `independent_repro` itself:
    gates.verify_outcome does not consult it. It exists so a repro that exited
    non-zero for the wrong reason (a timeout, an import error) is recorded as
    such rather than read as confirming evidence.

    There is deliberately NO outcome field: gates.verify_outcome computes the
    outcome from the committed blind verdict and the host-observed exit codes, so
    the agent is structurally incapable of resolving an escalation. from_dict
    selects only the fields it owns and drops anything else the agent emits."""
    pr: int
    red_reason_match: dict
    repro_reason_match: dict
    findings: list[dict]

    @classmethod
    def from_dict(cls, d: dict) -> JudgeItem:
        return cls(pr=d["pr"], red_reason_match=d.get("red_reason_match") or {},
                   repro_reason_match=d.get("repro_reason_match") or {},
                   findings=d.get("findings") or [])


@dataclass(frozen=True, kw_only=True)
class AuthorItem:
    """One PR's authored-test proposal — the AUTHOR pass's output for a PR that
    ships no test. The agent authors test file CONTENTS only: the driver
    validates the paths, builds the patch, and derives the red/green command
    from the paths (verify_driver.validate_authored), so no agent-authored
    string is ever executed as a command.

    `expected_red_signature` is the pre-committed prediction the post-run judge
    checks the authored test's red output against, exactly like the test lane's.
    Booleans are accepted only as actual bools (`is True`), and files are
    filtered to well-formed {path, contents} string pairs — a malformed entry is
    dropped rather than trusted."""
    pr: int
    can_author: bool
    files: list[dict[str, str]]
    expected_red_signature: str | None = None
    confidence: str = "medium"
    reasoning: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> AuthorItem:
        files = [f for f in (d.get("files") or [])
                 if isinstance(f, dict) and isinstance(f.get("path"), str)
                 and isinstance(f.get("contents"), str)]
        return cls(
            pr=d["pr"], can_author=d.get("can_author") is True,
            files=[{"path": f["path"], "contents": f["contents"]} for f in files],
            expected_red_signature=d.get("expected_red_signature"),
            confidence=d.get("confidence", "medium"),
            reasoning=d.get("reasoning", ""),
        )

    def to_signal(self) -> dict[str, object]:
        """The stored `signals.authored_test` core. `attempted` marks that the
        AUTHOR pass ran for this PR; the driver adds `test_cmd` /
        `skipped_reason` after validation and the host adds the lane's exit
        codes after the run."""
        return {"attempted": True, "can_author": self.can_author,
                "files": self.files,
                "expected_red_signature": self.expected_red_signature,
                "confidence": self.confidence, "reasoning": self.reasoning}
