"""The design review: a reviewer Prospector runs itself on each PR head, judging
what the GitHub reviewers do not — reuse of what the repository already has,
the layer a change lands in, drift from the repository's own conventions, and
whether the PR's tests would catch a broken implementation.

One locked-down agent reads two things: this machine's pinned default-branch
clone, where only maintainers' code lives, and the head's diff. It has no
GitHub, no edits, and no environment beyond the CLI's own. The host keeps only
findings it can anchor — on a file the diff touches, and for reuse on an
existing file in the base — and stores the result as `reviews["design"]`
(`reviewers.DESIGN`), advisory unless `TRIAGE_DESIGN_REVIEW=gate`.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import (authoring, capacity, diffpaths, freshness, gates, headless_agent, reviewers,
                      storekit)

if TYPE_CHECKING:
    from pipeline.model import Pr

PHASE = "design-review:run"
AGENT_TIMEOUT_SECONDS = 900
TITLE_CHARS = 200
BODY_CHARS = 2000

PROMPT = """\
You are reviewing pull request #__PR__ ("__TITLE__") to this repository, an
outside contribution. You judge its design, not its security and not its style
nits: the repository has other reviewers for those.

The repository's default branch is checked out at __CLONE__ — code only its
maintainers changed. The pull request's diff against it is the file
__DIFF__. Read both; grep the checkout as much as you need.

Judge the diff on four things:

1. reuse — a function, type, constant, component or test helper the diff adds
   that the checkout already has. Grep for it. Every reuse finding names the
   existing file as `evidence_path`, a path you opened in the checkout.
2. layering — a change placed outside the module or layer that owns the
   behavior, where the surrounding code would not put it.
3. convention — code that departs from what the repository's contributor docs
   below and its `rules/` directory (read any `rules/*.mdc` in the checkout)
   ask of code, design and tests.
4. test — a test that would still pass with the implementation broken: an
   assertion on a mock rather than behavior, one that checks nothing the
   change does, over-mocking of the code under test, or an edge case the diff
   introduces that no test reaches.

Severity: `blocking` — the change should not merge as written for this
reason; `major` — a maintainer would ask for it to be changed; `minor` — worth
saying, not worth a round trip.

Report only what you can point at. A finding you cannot tie to a line of the
diff, or a reuse you did not find in the checkout, is noise: leave it out. An
empty list is a good answer for a good change.

__DOCS__Your final message is exactly one JSON object, nothing else:
  {"summary": "<two or three sentences on the change's design>",
   "findings": [{"category": "reuse" | "layering" | "convention" | "test",
                 "severity": "blocking" | "major" | "minor",
                 "path": "<a file the diff touches>",
                 "line": <line in the new file, or null>,
                 "title": "<one line>",
                 "body": "<what is wrong and what to do instead>",
                 "evidence_path": "<checkout file it duplicates, for reuse; else null>"}]}
"""


class DesignReviewFailed(RuntimeError):
    """The agent finished without a usable review: the machine's failure to
    judge, never a verdict on the PR."""


def huntable(pr: Pr) -> bool:
    """Whether the idle hunter may spend a design review on `pr`'s head: open,
    not a draft, threat-scanned clear at this head, not a dependency bump, not
    routed to a close by a current analysis, and with no design review of this
    head yet."""
    head = pr.head_sha
    if not head or pr.state != "open" or pr.draft or gates.bump_exempt(pr):
        return False
    if freshness.is_current(pr, "analysis") and str(pr.disposition or "").startswith("close-"):
        return False
    if pr.threat_verdict != "clear" or gates.threat_blocks(pr) or not freshness.is_current(pr, "threat"):
        return False
    return (pr.review_entry(reviewers.DESIGN.id) or {}).get("reviewed_sha") != head


def _within(clone: Path, path: str) -> bool:
    target = (clone / path).resolve()
    return target.is_relative_to(clone) and target.is_file()


def validate(answer: dict, *, touched: set[str], clone: Path) -> tuple[str, list[dict]]:
    """The answer's summary and the findings the host can anchor, in the
    `reviews` entry's finding shape."""
    base = clone.resolve()
    kept: list[dict] = []
    raw = answer.get("findings")
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict):
            continue
        category, severity = f.get("category"), f.get("severity")
        path, title = str(f.get("path") or ""), str(f.get("title") or "").strip()
        evidence = f.get("evidence_path")
        if (category not in reviewers.DESIGN_CATEGORIES
                or severity not in reviewers.DESIGN_SEVERITIES
                or path not in touched or not title):
            continue
        if category == "reuse" and not (isinstance(evidence, str) and _within(base, evidence)):
            continue
        line = f.get("line")
        kept.append({"path": path, "line": line if isinstance(line, int) else None,
                     "severity": severity, "category": category, "title": title[:TITLE_CHARS],
                     "body": str(f.get("body") or "")[:BODY_CHARS],
                     "evidence_path": evidence if category == "reuse" else None,
                     "resolved": False, "outdated": False, "commit": None, "url": None})
    return str(answer.get("summary") or ""), kept


def review(clone: Path, diff: Path, *, pr: int, title: str, head_sha: str,
           base_sha: str) -> dict:
    """Review the diff at `diff` against the base checkout at `clone` and
    return the `reviews["design"]` entry for `head_sha`. Raises
    DesignReviewFailed when the agent did not finish or gave no usable answer;
    an agent outage, a decline, an overload, and a closed capacity gate
    propagate as headless_agent / capacity raise them."""
    clone, diff = clone.resolve(), diff.resolve()
    prompt = headless_agent.fill(PROMPT, {
        "__PR__": pr, "__TITLE__": title[:TITLE_CHARS], "__CLONE__": clone, "__DIFF__": diff,
        "__DOCS__": authoring.docs_block(authoring.docs_from_tree(clone))})

    def run() -> str:
        return headless_agent.run_agent(
            prompt, allow_gh=False, cwd=str(clone), edit_root=None,
            read_root=[str(clone), str(diff)], env_allow=(), timeout=AGENT_TIMEOUT_SECONDS)

    try:
        answer, _ = headless_agent.json_reply(run)
    except (headless_agent.AgentUnavailable, headless_agent.AgentDeclined,
            headless_agent.AgentTransient, capacity.CapacityPaused):
        raise
    except (RuntimeError, ValueError) as e:
        raise DesignReviewFailed(f"the design reviewer did not finish: {e}") from e
    if not isinstance(answer.get("findings"), list):
        raise DesignReviewFailed("the design reviewer's answer carried no findings list")
    touched = set(diffpaths.changed_paths(diff.read_text(errors="replace")))
    summary, findings = validate(answer, touched=touched, clone=clone)
    return {"kind": reviewers.REVIEW, "reviewed_sha": head_sha, "observed_at": storekit.now(),
            "score": None, "findings": findings, "summary": summary[:reviewers.SUMMARY_CHARS] or None,
            "checks": [], "extra": {"base_sha": base_sha}}
