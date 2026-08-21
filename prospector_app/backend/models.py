"""Pydantic models for the app's FastAPI boundary.

The single source of truth for the wire shapes the frontend round-trips: the
execute/close action payload, the per-endpoint request bodies, and the
discriminated `accept` union suggest.py produces. Parsed at the `Body(...)` edge
(a malformed body becomes a 422 instead of a silent `.get()`-default), then
carried through the executor as typed objects. Mirrored on the frontend by the
TS `SuggestAccept` interface in frontend/src/api.ts.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class CloseAction(BaseModel):
    """One PR's close disposition plus the references its verb needs — a
    duplicate's `canonical`, an already-fixed PR's `upstream_*`, an oversized
    close's `merge_prs`. Each close kind carries only the refs it uses; the rest
    stay None. `action` is a plain str (not a Literal) so the executor's
    CLOSE_ACTIONS membership check still skips an unknown verb gracefully rather
    than rejecting the body with a 422. The body of POST /api/execute/pr/{n} and
    the executor's close-action parameter type."""
    action: str
    pr: int | None = None
    override_action: str | None = None
    # Post over a "the head moved since we analyzed this" refusal. Set by the
    # operator confirming the drift the app just showed them.
    override_stale: bool = False
    canonical: int | None = None
    upstream_pr: int | None = None
    upstream_commit: str | None = None
    upstream_date: str | None = None
    merge_prs: list[int] | None = None
    dup_reason: str | None = None
    comment: str | None = None
    reason: str | None = None
    tags: list[str] | None = None


class MergeAccept(BaseModel):
    kind: Literal["merge"] = "merge"
    method: str = "squash"
    tags: list[str] = Field(default_factory=list)


class ReviewAccept(BaseModel):
    kind: Literal["review"] = "review"
    event: str
    body: str
    tags: list[str] = Field(default_factory=list)


class CloseAccept(BaseModel):
    kind: Literal["close"] = "close"
    action: str
    canonical: int | None = None
    upstream_pr: int | None = None
    upstream_commit: str | None = None
    upstream_date: str | None = None
    merge_prs: list[int] | None = None
    tags: list[str] = Field(default_factory=list)


# The polymorphic suggestion `accept`, discriminated on `kind`: each variant
# routes to its own executor endpoint (merge / review / close) on the frontend.
SuggestAccept = Annotated[CloseAccept | ReviewAccept | MergeAccept,
                          Field(discriminator="kind")]


class ReviewBody(BaseModel):
    event: str = "comment"
    body: str = ""
    reason: str | None = None
    tags: list[str] | None = None
    override_stale: bool = False


class LineCommentBody(BaseModel):
    file: str = ""
    line: int = 0
    body: str = ""
    reason: str | None = None
    override_stale: bool = False


class IssueCloseDupBody(BaseModel):
    canonical: int | None = None
    comment: str | None = None


class IssueCloseFixedBody(BaseModel):
    fixed_by: int
    comment: str | None = None


class IssueCloseBody(BaseModel):
    # Operator-directed close. `disposition` picks the semantics: not-planned /
    # completed are plain GitHub state_reasons (comment required); fixed needs a
    # `fixed_by` PR, dup needs a `canonical` issue (comment optional — a template
    # is posted when empty).
    disposition: Literal["not-planned", "completed", "fixed", "dup"] = "not-planned"
    comment: str | None = None
    fixed_by: int | None = None
    canonical: int | None = None


class IssueCommentBody(BaseModel):
    comment: str = ""


class AlertDismissBody(BaseModel):
    # The per-source reason vocabulary lives in alert_triage.alert_gates
    # (DISMISS_REASONS); the gate validates it, so this stays a plain string.
    reason: str
    comment: str | None = None


class BulkExecuteBody(BaseModel):
    prs: list[int] = []
    action: str = "CLOSE"
    comment: str | None = None
    comments: dict[int, str] | None = None
    canonical: int | None = None
    method: str = "squash"
    reason: str | None = None
    tags: list[str] | None = None
    dry_run: bool = True


class ClusterItem(BaseModel):
    pr: int
    action: str
    comment: str | None = None
    reason: str | None = None
    canonical: int | None = None
    method: str | None = None
    upstream_pr: int | None = None
    upstream_commit: str | None = None
    upstream_date: str | None = None
    tags: list[str] | None = None


class ClusterExecuteBody(BaseModel):
    items: list[ClusterItem] = []
    dry_run: bool = True


class OnboardingProbe(BaseModel):
    """Candidate configuration to check without committing any of it."""
    store_url: str | None = None
    repo: str | None = None
    key_file: str | None = None


class OnboardingApply(BaseModel):
    """One step of setup. `bundle` is a teammate's pasted deployment bundle;
    when it is present it supplies `env` and `profile` in their place."""
    step: str
    env: dict[str, str] = {}
    profile: dict[str, object] | None = None
    bundle: str | None = None


class WorkerFlags(BaseModel):
    """The worker lane switches the Setup view writes. Values are validated
    against the allowlist in worker_control, which is the one that matters —
    this only carries them."""
    flags: dict[str, str]
