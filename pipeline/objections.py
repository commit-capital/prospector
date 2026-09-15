"""An objection: a machine judgment that names a defect an agent may fix.

A reviewer's rejection of a resolution, a compile excerpt the pristine base
passes, a fix reviewer's rejection, or a security finding is handed to the
authoring agent as its goal. The signature keys once-per-head and the budget
bounds unattended spend per worker per UTC day.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pipeline import settings

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.store import Store

KINDS = ("resolve-review", "compile", "fix-review", "security")

_GOALS = {
    "resolve-review": ("A reviewer rejected the previous change for this reason; make the "
                       "smallest change that resolves it without undoing the resolution:"),
    "fix-review": ("A reviewer rejected the previous change for this reason; make the "
                   "smallest change that resolves it while keeping the fix's intent:"),
    "compile": ("The compile check failed with this error; make the smallest change "
                "that makes it pass:"),
    "security": ("A security review flagged this finding; make the smallest change "
                 "that removes it:"),
}

# How much of an objection's text is stored and handed to the agent.
TEXT_CHARS = 2000

# How long a `failed` ending (the machine's) keeps an objection answered before
# a worker may take it up again.
FAILED_COOLDOWN_SECONDS = 3600


def _flatten(text: str) -> str:
    text = re.sub(r"[0-9a-f]{7,}", "#", text.lower())
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()


def build(kind: str, text: str, *, origin: dict[str, object] | None = None) -> dict:
    """The stored objection for `kind` and `text`: `{kind, signature, text,
    from}`, the signature keyed by kind and the text with numbers and hashes
    flattened so the same defect on another line signs the same."""
    if kind not in KINDS:
        raise ValueError(f"unknown objection kind {kind!r}")
    digest = hashlib.sha1(_flatten(text).encode()).hexdigest()[:12]
    return {"kind": kind, "signature": f"{kind}:{digest}", "text": text[:TEXT_CHARS],
            "from": dict(origin or {})}


def goal_text(objection: dict) -> str:
    """The goal handed to the authoring agent for `objection`."""
    return f"{_GOALS[str(objection['kind'])]}\n\n{objection['text']}"


def spent(pr: Pr, signature: str, now: datetime | None = None) -> bool:
    """Whether this head already answered `signature`: a fix_request carrying
    it past `queued`, or a continuation round stamped with it. A `failed`
    ending is the machine's and holds the answer only for
    FAILED_COOLDOWN_SECONDS."""
    req = pr.fix_request or {}
    if req.get("against_head_sha") != pr.head_sha:
        return False
    obj = req.get("objection") or {}
    rounds = (req.get("result") or {}).get("rounds") or []
    carried = (obj.get("signature") == signature and req.get("status") != "queued") or any(
        (r.get("objection") or {}).get("signature") == signature
        for r in rounds if isinstance(r, dict))
    if not carried:
        return False
    if req.get("status") != "failed":
        return True
    try:
        ended = datetime.fromisoformat(str(req.get("finished_at")))
    except ValueError:
        return False
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - ended).total_seconds() < FAILED_COOLDOWN_SECONDS


def used_today(store: Store, worker: str, now: datetime | None = None) -> int:
    """How many distinct objection continuations (one per PR and signature)
    `worker` ended since UTC midnight, read from the fix lane's ledger
    entries; a continuation that parks and later pushes counts once."""
    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seen: set[tuple[object, str]] = set()
    for run in store.runs(since=midnight.isoformat()):
        if getattr(run, "phase", None) != "fix:single":
            continue
        stats = run.raw.get("stats") or {}
        if stats.get("host") == worker and stats.get("objection"):
            seen.add((run.raw.get("pr"), str(stats["objection"])))
    return len(seen)


def budget_left(store: Store, worker: str, now: datetime | None = None) -> int:
    return max(0, settings.fix_objection_budget() - used_today(store, worker, now))
