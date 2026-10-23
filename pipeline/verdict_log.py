"""The record of each run of Prospector's own reviewers, for the reviewer eval
ratchet (docs/superpowers/specs/2026-10-23-reviewer-eval-ratchet-design.md).

Each reviewer's entry point calls `record` with the subject its caller names;
an eval run names none and records nothing. A reviewer's `verdict` is one of
`VERDICTS[reviewer]`, least severe first, its failure value last. `prompt_sha`
identifies the prompt templates a run used and `logic` the version of the
reviewer's deterministic adjustments. Each write also clears the inputs of
unlabeled runs older than RETENTION_DAYS. A failed write is reported and never
fails the review.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple

from pipeline import storekit
from pipeline.store import Store

VERDICTS: dict[str, tuple[str, ...]] = {
    "design": ("pass", "fail", "failed"),
    "security": ("GREEN", "YELLOW", "RED", "INCOMPLETE"),
    "fix-review": ("safe", "unsafe", "failed"),
    "trust-boundary": ("pass", "hold", "failed"),
    "intake": ("clear", "suspicious", "malicious", "failed"),
    "fixability": ("go", "ask", "skip", "failed"),
}
REVIEWERS: tuple[str, ...] = tuple(VERDICTS)
RETENTION_DAYS = 90


class Subject(NamedTuple):
    kind: Literal["pr", "issue", "replay"]
    number: int


def prompt_sha(*templates: str, logic: int = 0) -> str:
    text = "\0".join(templates) + f"\0logic={logic}"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def record(reviewer: str, subject: Subject | None, *, verdict: str, prompt_sha: str,
           model: str | None, data: dict, inputs: dict[str, str], head_sha: str | None = None,
           base_sha: str | None = None, store: Store | None = None) -> int | None:
    """The new row's id, or None when there is no subject or the write failed. A
    None `model` (the CLI's own default) records as "cli-default"."""
    if verdict not in VERDICTS.get(reviewer, ()):
        raise ValueError(f"{reviewer!r} has no verdict {verdict!r}")
    if subject is None:
        return None
    blob = gzip.compress(json.dumps(inputs, sort_keys=True).encode())
    vid = None
    try:
        st = store or Store()
        vid = st.append_review_verdict(
            at=storekit.now(), reviewer=reviewer, subject_kind=subject.kind,
            subject=subject.number, head_sha=head_sha, base_sha=base_sha,
            prompt_sha=prompt_sha, model=model or "cli-default", verdict=verdict, data=data,
            inputs_gz=blob)
        expired = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        st.clear_review_verdict_inputs(expired.isoformat(timespec="seconds"))
    except Exception as e:  # noqa: BLE001 — a lost record must never fail the review
        print(f"verdict_log: could not record {reviewer} on {subject.kind} "
              f"#{subject.number}: {e}", file=sys.stderr)
    return vid
