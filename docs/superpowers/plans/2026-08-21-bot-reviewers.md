# Bot Reviewers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single hardcoded Greptile review provider with a registry of every automated PR reviewer and security scanner the triaged repository runs (Greptile, CodeRabbit, Superagent, Socket), auto-detected, each gating `pr_clean` under its own name, fed to the agents, and surfaced on the PR page and in PR Explorer columns/filters.

**Architecture:** A new `pipeline/reviewers.py` registry holds one `Reviewer` per bot with adapter functions (`parse`, `bar`, `severity`, `digest`, `findings_for_fix`); `pipeline/review_fetch.py` fetches a per-PR GitHub feed (one GraphQL call per ≤10 PRs: reviews, review threads with resolution, comments, head check runs) and ingest stores the normalized entries in a new stamped `reviews` section (`{<reviewer id>: entry}`) beside `signals`. `pipeline/review_policy.py` decides which reviewers are *active* (`TRIAGE_REVIEW_PROVIDER=auto|none|<ids>`; `auto` reads a `reviewers` registry that ingest recomputes from the bots' last activity) and exposes structured `clean_blockers(pr, kind)`; gates, checks, filters, agents, and the frontend read through it.

**Tech Stack:** Python 3.14 (`uv run`), pytest, pyright (0 errors), ruff (0 findings); React/TypeScript (Vite, pnpm), `tsc -b` 0 errors, ESLint no new errors; GitHub GraphQL via `gh api graphql`.

**Spec:** `docs/superpowers/specs/2026-08-21-bot-reviewers-design.md`

## Global Constraints

- Every function signature fully typed (most specific type); no quoted annotations; qualified imports (`from pipeline import …`); no `sys.path` hacks.
- Comments describe the present code only — no "previously", "instead of", "now does".
- `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` → 0 errors; `uv run ruff check .` → 0 findings; `uv run pytest` green.
- Frontend: `pnpm run build` (`tsc -b` 0 errors) and `pnpm exec eslint <changed files>` adds no errors; double-quoted strings, inline prop types.
- `schema.STORE_SCHEMA_VERSION` → 19 with a changelog line.
- `pyproject.toml` dependency lists stay sorted (no new deps expected).
- Reviewer ids are exactly `greptile`, `coderabbit`, `superagent`, `socket`; kinds exactly `review`, `scanner`; bar statuses exactly `pass`, `fail`, `stale`, `pending`, `na`.
- Store section name `reviews`; registry name `reviewers`; filter spec key `reviewer_status`; check row key `scans`; bulk action `REVIEW_RETRIGGER`; activity kind `review_retrigger`; history kind `bot_review`; live-freshness divergence kind `review`.
- Commit after every task (`Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer).

---

## File map

| File | Responsibility |
|---|---|
| `pipeline/reviewers.py` (new) | Registry + adapters + bars + digests + seen-summary |
| `pipeline/review_fetch.py` (new) | GraphQL feed fetch → `PrFeed` |
| `pipeline/ci_signal.py` (new) | CI verdict from check runs/statuses, excluding reviewer apps; GraphQL context normalizer |
| `pipeline/review_policy.py` (rewrite) | Active-reviewer policy, `bar`, `clean_blockers`, `merge_bar_sentence`, `describe` |
| `pipeline/greptile.py` (delete) | text-parsing helpers move into `reviewers.py` |
| `pipeline/migrate_reviews.py` (new) | one-shot `signals.greptile*` → `reviews.greptile` |
| `pipeline/model.py`, `store.py`, `schema.py`, `freshness.py`, `settings.py`, `gates.py`, `ingest.py`, `live_prs.py`, `greptile_read_driver.py`, `analyze_driver.py`, `security_driver.py`, `security_review.py`, `workflows/security.js`, `triage_cluster.py` | migrate |
| `prospector_app/backend/{caps,service,pr_checks,filters,pr_search,app,executor,bulk,review_refresh,freshness_live,pr_history,fix_worker,chat,deep_search,training}.py` | migrate |
| `prospector_app/frontend/src/{api.ts,ExecContext.tsx,useColumnPrefs.ts,glossary.ts}`, `components/explorer/{checkDefs.ts,columns.tsx,ReviewCell.tsx(new),ScansCell.tsx(new),ColumnFilterPopout.tsx,prFilterParts.ts,lanes.ts,BulkActionBar.tsx,BulkConfirmDialog.tsx,ExplorerSearchBar.tsx}`, `components/{FactFreshness,PRHistory}.tsx`, `views/{PRDetail.tsx,homeCards.ts,PRExplorer.tsx}` | migrate; delete `GreptileCell.tsx` |

---

### Task 1: `pipeline/reviewers.py` — registry, shared helpers, Greptile adapter

**Files:**
- Create: `pipeline/reviewers.py`
- Create: `pipeline/tests/test_reviewers.py`
- Create: `pipeline/tests/fixtures/reviewers/` — JSON feeds built from the captured payloads

**Interfaces:**
- Produces: `Reviewer`, `REVIEW`, `SCANNER`, `REVIEWERS: dict[str, Reviewer]`, `GREPTILE/CODERABBIT/SUPERAGENT/SOCKET`, `by_login(login) -> Reviewer | None`, `by_app(slug) -> Reviewer | None`, `Bar(status, reason, ask)`, `PASS/FAIL/STALE/PENDING/NA`, `parse(reviewer, feed, head_sha, previous) -> dict | None`, `bar(reviewer, entry, head_sha, *, threshold) -> Bar`, `severity(reviewer, entry, greptile_review) -> str | None`, `open_findings(entry, head_sha) -> list[dict]`, `open_counts(entry, head_sha) -> dict[str, int]`, `digest(reviewer, entry, b, head_sha) -> dict`, `findings_for_fix(reviewer, entry, head_sha, greptile_review) -> list[dict]`, `evidence(entries, head_sha) -> list[dict]`, `summary_line(digests) -> str`, `version(reviewer, entry) -> str`, `seen_summary(prs) -> dict`, `parse_confidence_score(text) -> int | None`, `strip_html(s) -> str`, `PrFeed` (imported from review_fetch — Task 2 defines it; this task defines the dataclass in `review_fetch.py` first with no fetch logic).

- [ ] **Step 1: Create `pipeline/review_fetch.py` with only the `PrFeed` dataclass** (fetch logic comes in Task 2):

```python
"""GitHub's on-PR bot feed: every review, review thread, issue comment and head
check run, fetched for a batch of PRs in one GraphQL call each and handed to
`pipeline.reviewers` to parse per bot."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PrFeed:
    """One PR's raw bot-relevant activity. `conversation` is False when only the
    head's check runs were fetched (the PR's conversation is unchanged since the
    stored entry) — adapters then keep the stored conversation fields."""
    pr: int
    head_sha: str | None
    updated_at: str | None
    reviews: list[dict] = field(default_factory=list)     # {id, login, state, commit, body, at, url}
    threads: list[dict] = field(default_factory=list)     # {id, login, path, line, body, commit, original_commit, resolved, outdated, at, url}
    comments: list[dict] = field(default_factory=list)    # {id, login, body, at, updated_at, url}
    check_runs: list[dict] = field(default_factory=list)  # {app, name, status, conclusion, title, summary, url}
    statuses: list[dict] = field(default_factory=list)    # {context, state}
    conversation: bool = True
```

- [ ] **Step 2: Write the failing Greptile tests** in `pipeline/tests/test_reviewers.py`:

```python
"""pipeline/reviewers.py — the ONE registry of automated PR reviewers and scanners."""
from pipeline import reviewers
from pipeline.review_fetch import PrFeed

HEAD = "cb7342d3aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OLD = "816a0611bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

GREPTILE_SUMMARY = (
    "<h3>Greptile Summary</h3><p>Adds X.</p><p><b>Confidence Score: 3/5</b></p>"
    "<sub>Last reviewed commit: https://github.com/o/r/pull/1/commits/" + OLD + "</sub>")


def _feed(**over) -> PrFeed:
    base = dict(pr=1, head_sha=HEAD, updated_at="2026-08-21T00:00:00Z")
    base.update(over)
    return PrFeed(**base)


class TestRegistry:
    def test_four_reviewers_with_kinds(self):
        assert set(reviewers.REVIEWERS) == {"greptile", "coderabbit", "superagent", "socket"}
        assert reviewers.GREPTILE.kind == reviewers.REVIEW
        assert reviewers.SUPERAGENT.kind == reviewers.SCANNER

    def test_by_login_and_app(self):
        assert reviewers.by_login("greptile-apps[bot]") is reviewers.GREPTILE
        assert reviewers.by_login("coderabbitai") is reviewers.CODERABBIT
        assert reviewers.by_login("superagent-security[bot]") is reviewers.SUPERAGENT
        assert reviewers.by_login("octocat") is None
        assert reviewers.by_app("socket-security") is reviewers.SOCKET
        assert reviewers.by_app("github-actions") is None


class TestGreptile:
    def test_parse_score_from_summary_comment(self):
        feed = _feed(comments=[{"id": 1, "login": "greptile-apps[bot]", "body": GREPTILE_SUMMARY,
                                "at": "2026-08-20T00:00:00Z", "updated_at": None, "url": "u"}])
        e = reviewers.parse(reviewers.GREPTILE, feed, HEAD, None)
        assert e is not None
        assert e["kind"] == "review" and e["score"] == 3 and e["reviewed_sha"] == OLD
        assert "Confidence Score: 3/5" in e["summary"] and "<h3>" not in e["summary"]
        assert e["observed_at"] == "2026-08-20T00:00:00Z"

    def test_parse_prefers_check_run_at_head(self):
        feed = _feed(
            comments=[{"id": 1, "login": "greptile-apps[bot]", "body": GREPTILE_SUMMARY,
                       "at": "2026-08-20T00:00:00Z", "updated_at": None, "url": "u"}],
            check_runs=[{"app": "greptile-apps", "name": "Greptile Review", "status": "completed",
                         "conclusion": "failure", "title": "Confidence 4/5 — below your required 5/5",
                         "summary": "…", "url": "c"}])
        e = reviewers.parse(reviewers.GREPTILE, feed, HEAD, None)
        assert e["score"] == 4 and e["reviewed_sha"] == HEAD
        assert e["checks"][0]["name"] == "Greptile Review"

    def test_parse_none_when_bot_absent(self):
        assert reviewers.parse(reviewers.GREPTILE, _feed(), HEAD, None) is None

    def test_parse_without_conversation_keeps_previous(self):
        prev = {"kind": "review", "score": 3, "reviewed_sha": OLD, "summary": "s", "findings": [],
                "checks": [], "extra": {}, "observed_at": "2026-08-20T00:00:00Z"}
        feed = _feed(conversation=False)
        e = reviewers.parse(reviewers.GREPTILE, feed, HEAD, prev)
        assert e["score"] == 3 and e["summary"] == "s" and e["checks"] == []

    def test_bar(self):
        at_head = {"kind": "review", "score": 5, "reviewed_sha": HEAD, "findings": [], "checks": [], "extra": {}}
        assert reviewers.bar(reviewers.GREPTILE, at_head, HEAD, threshold=5).status == "pass"
        below = dict(at_head, score=3)
        b = reviewers.bar(reviewers.GREPTILE, below, HEAD, threshold=5)
        assert b.status == "fail" and b.reason == "greptile 3/5" and "5/5" in (b.ask or "")
        stale = dict(at_head, reviewed_sha=OLD)
        assert reviewers.bar(reviewers.GREPTILE, stale, HEAD, threshold=5).status == "stale"
        assert reviewers.bar(reviewers.GREPTILE, None, HEAD, threshold=5).status == "pending"
        assert reviewers.bar(reviewers.GREPTILE, None, HEAD, threshold=5).reason == "awaiting greptile review"

    def test_severity_from_semantic_read(self):
        e = {"kind": "review", "score": 3, "reviewed_sha": HEAD, "findings": [], "checks": [], "extra": {}}
        assert reviewers.severity(reviewers.GREPTILE, e, {"severity": "nits"}) == "nits"
        assert reviewers.severity(reviewers.GREPTILE, e, None) is None

    def test_findings_for_fix_uses_semantic_read(self):
        e = {"kind": "review", "score": 3, "reviewed_sha": HEAD, "findings": [], "checks": [], "extra": {}}
        read = {"findings": [{"headline": "h", "class": "substantive", "why": "w"}]}
        assert reviewers.findings_for_fix(reviewers.GREPTILE, e, HEAD, read) == read["findings"]
        assert reviewers.findings_for_fix(reviewers.GREPTILE, e, HEAD, None) == []

    def test_parse_confidence_score(self):
        assert reviewers.parse_confidence_score("x Confidence Score: 4/5 y") == 4
        assert reviewers.parse_confidence_score(None) is None
```

- [ ] **Step 3: Run to verify failure**: `uv run pytest pipeline/tests/test_reviewers.py -q` → `ModuleNotFoundError: pipeline.reviewers`.

- [ ] **Step 4: Write `pipeline/reviewers.py`** (registry, helpers, Greptile adapter; the other adapters land in Tasks 2–4 but the dispatch tables are created here):

```python
"""The ONE registry of automated PR reviewers and security scanners.

A reviewer is a bot that posts feedback on PRs. Two kinds: `review` (code-review
providers) and `scanner` (security scanners). Each has an adapter that turns a
PR's raw GitHub feed (`review_fetch.PrFeed`) into one normalized entry stored
under `reviews[<id>]`, a `bar` that judges the entry against the reviewer's pass
condition, and projections for the app, the agents and the autofix goal.
`review_policy` decides which reviewers are active for the repository."""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline import storekit

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.review_fetch import PrFeed

REVIEW = "review"
SCANNER = "scanner"
KINDS = (REVIEW, SCANNER)

PASS, FAIL, STALE, PENDING, NA = "pass", "fail", "stale", "pending", "na"
BAR_STATUSES = (PASS, FAIL, STALE, PENDING, NA)

SUMMARY_CHARS = 6000
FINDING_BODY_CHARS = 2000


@dataclass(frozen=True)
class Reviewer:
    id: str
    label: str
    kind: str
    logins: tuple[str, ...]      # substrings matched against a bot's login, lower-cased
    app_slugs: tuple[str, ...]   # check-run app slugs
    retrigger_mention: str | None
    score_max: int | None


@dataclass(frozen=True)
class Bar:
    status: str
    reason: str | None   # operator-facing clause for pr_clean reasons; None for pass/na
    ask: str | None      # author-facing sentence for bar_asks; None when nothing to ask


GREPTILE = Reviewer("greptile", "Greptile", REVIEW, ("greptile",), ("greptile-apps",),
                    "@greptileai", 5)
CODERABBIT = Reviewer("coderabbit", "CodeRabbit", REVIEW, ("coderabbitai",), ("coderabbitai",),
                      "@coderabbitai review", None)
SUPERAGENT = Reviewer("superagent", "Superagent", SCANNER, ("superagent-security",),
                      ("superagent-security",), None, None)
SOCKET = Reviewer("socket", "Socket", SCANNER, ("socket-security",), ("socket-security",),
                  None, None)

REVIEWERS: dict[str, Reviewer] = {r.id: r for r in (GREPTILE, CODERABBIT, SUPERAGENT, SOCKET)}


def by_login(login: str | None) -> Reviewer | None:
    low = (login or "").lower()
    if not low:
        return None
    for r in REVIEWERS.values():
        if any(s in low for s in r.logins):
            return r
    return None


def by_app(slug: str | None) -> Reviewer | None:
    for r in REVIEWERS.values():
        if slug in r.app_slugs:
            return r
    return None


def app_slugs() -> frozenset[str]:
    return frozenset(s for r in REVIEWERS.values() for s in r.app_slugs)


# --- shared text helpers ---------------------------------------------------

_SCORE_RE = re.compile(r"Confidence Score:\s*(\d)\s*/\s*5")
_CHECK_SCORE_RE = re.compile(r"Confidence\s+(\d)\s*/\s*(\d)")
_SUMMARY_SHA_RE = re.compile(r"[Ll]ast reviewed commit:[^\n]*?/commit/([0-9a-f]{7,40})")


def parse_confidence_score(body: str | None) -> int | None:
    """Greptile's `Confidence Score: N/5` from any comment or review body."""
    m = _SCORE_RE.search(body or "")
    return int(m.group(1)) if m else None


def strip_html(s: str) -> str:
    """A bot's comment as displayable text: HTML tags unwrapped, entities and
    literal `\\uXXXX` decoded, runs of blank lines collapsed."""
    s = re.sub(r"<!--.*?-->", "", s or "", flags=re.DOTALL)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</(p|h\d|li|ul|ol|details|summary|tr)>", "\n", s)
    s = re.sub(r"<li>", "• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&#39;", "'").replace("&quot;", '"'))
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def _own(reviewer: Reviewer, items: list[dict]) -> list[dict]:
    return [i for i in items if by_login(i.get("login")) is reviewer]


def _own_checks(reviewer: Reviewer, feed: PrFeed) -> list[dict]:
    return [{"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion"),
             "title": c.get("title"), "summary": (c.get("summary") or "")[:600] or None,
             "url": c.get("url")}
            for c in feed.check_runs if by_app(c.get("app")) is reviewer]


def _latest(items: list[dict], key: str = "at") -> dict | None:
    dated = [i for i in items if i.get(key)]
    return max(dated, key=lambda i: str(i.get(key))) if dated else None


def _max_at(*stamps: str | None) -> str | None:
    real = [s for s in stamps if s]
    return max(real) if real else None


def _finding(thread: dict, severity: str | None, title: str | None) -> dict:
    return {"path": thread.get("path"), "line": thread.get("line"), "severity": severity,
            "title": title, "body": (thread.get("body") or "")[:FINDING_BODY_CHARS],
            "resolved": bool(thread.get("resolved")), "outdated": bool(thread.get("outdated")),
            "commit": thread.get("original_commit") or thread.get("commit"),
            "url": thread.get("url")}


def _entry(reviewer: Reviewer, *, reviewed_sha: str | None, observed_at: str | None,
           score: int | None, findings: list[dict], summary: str | None,
           checks: list[dict], extra: dict) -> dict:
    return {"kind": reviewer.kind, "reviewed_sha": reviewed_sha, "observed_at": observed_at,
            "score": score, "findings": findings,
            "summary": (summary or "")[:SUMMARY_CHARS] or None, "checks": checks, "extra": extra}


def _carry(previous: dict | None, reviewer: Reviewer, checks: list[dict]) -> dict | None:
    """The stored entry with the head's check runs refreshed — the shape of an
    entry whose conversation is unchanged since it was parsed."""
    if previous is None:
        if not checks:
            return None
        return _entry(reviewer, reviewed_sha=None, observed_at=None, score=None, findings=[],
                      summary=None, checks=checks, extra={})
    return {**previous, "checks": checks}


def open_findings(entry: dict | None, head_sha: str | None) -> list[dict]:
    """Findings still standing: unresolved, not outdated by a later push."""
    if not entry:
        return []
    return [f for f in entry.get("findings") or []
            if not f.get("resolved") and not f.get("outdated")]


def open_counts(entry: dict | None, head_sha: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in open_findings(entry, head_sha):
        sev = str(f.get("severity") or "unclassified")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


# --- Greptile --------------------------------------------------------------

def _greptile_parse(feed: PrFeed, head_sha: str | None, previous: dict | None) -> dict | None:
    checks = _own_checks(GREPTILE, feed)
    if not feed.conversation:
        entry = _carry(previous, GREPTILE, checks)
    else:
        comments = _own(GREPTILE, feed.comments)
        reviews = _own(GREPTILE, feed.reviews)
        threads = _own(GREPTILE, feed.threads)
        scored = [c for c in comments if parse_confidence_score(c.get("body"))]
        summary_c = _latest(scored, "updated_at") or _latest(scored)
        if summary_c is None and not reviews and not threads and not checks:
            return None
        body = (summary_c or {}).get("body") or ""
        footer = _SUMMARY_SHA_RE.search(body)
        latest_review = _latest(reviews)
        reviewed = ((footer.group(1) if footer else None)
                    or (latest_review or {}).get("commit")
                    or next((t.get("original_commit") or t.get("commit") for t in reversed(threads)
                             if t.get("original_commit") or t.get("commit")), None))
        findings = [_finding(t, None, (t.get("body") or "").strip().splitlines()[0][:160]
                             if t.get("body") else None) for t in threads]
        observed = _max_at((summary_c or {}).get("updated_at"), (summary_c or {}).get("at"),
                           (latest_review or {}).get("at"),
                           *[t.get("at") for t in threads])
        entry = _entry(GREPTILE, reviewed_sha=reviewed, observed_at=observed,
                       score=parse_confidence_score(body), findings=findings,
                       summary=strip_html(body) if body else None, checks=checks,
                       extra={"summary_sha": footer.group(1) if footer else None,
                              "check_title": None})
    if entry is None:
        return None
    # A completed Greptile check at the head names the score it reached there.
    for c in checks:
        m = _CHECK_SCORE_RE.search(c.get("title") or "")
        if c.get("status") == "completed" and m:
            entry = {**entry, "score": int(m.group(1)), "reviewed_sha": head_sha,
                     "extra": {**(entry.get("extra") or {}), "check_title": c.get("title")}}
            break
    return entry


def _greptile_bar(entry: dict | None, head_sha: str | None, threshold: int | None) -> Bar:
    th = threshold if threshold is not None else (GREPTILE.score_max or 5)
    mx = GREPTILE.score_max or 5
    if entry is None or entry.get("score") is None:
        return Bar(PENDING, "awaiting greptile review",
                   f"Greptile has not scored this PR yet — it needs {th}/{mx} to merge.")
    score = int(entry["score"])
    reviewed = entry.get("reviewed_sha")
    if reviewed and head_sha and reviewed != head_sha:
        return Bar(STALE, "greptile review stale",
                   "Greptile scored an earlier commit — a re-review of the current head is needed.")
    if score != th:
        return Bar(FAIL, f"greptile {score}/{mx}",
                   f"Greptile review is {score}/{mx} — address its review comments so it reaches "
                   f"{th}/{mx} (our merge bar).")
    return Bar(PASS, None, None)


# --- dispatch --------------------------------------------------------------

def parse(reviewer: Reviewer, feed: PrFeed, head_sha: str | None,
          previous: dict | None) -> dict | None:
    """The normalized entry for `reviewer` on this PR, or None when the bot has
    left nothing on it. `previous` is the stored entry, kept for its conversation
    fields when `feed.conversation` is False."""
    if reviewer is GREPTILE:
        return _greptile_parse(feed, head_sha, previous)
    if reviewer is CODERABBIT:
        return _coderabbit_parse(feed, head_sha, previous)
    if reviewer is SUPERAGENT:
        return _superagent_parse(feed, head_sha, previous)
    return _socket_parse(feed, head_sha, previous)


def parse_all(feed: PrFeed, head_sha: str | None, previous: dict | None) -> dict[str, dict]:
    """Every registry reviewer's entry on this PR, keyed by id; reviewers that
    left nothing are absent."""
    out: dict[str, dict] = {}
    for rid, r in REVIEWERS.items():
        e = parse(r, feed, head_sha, (previous or {}).get(rid))
        if e is not None:
            out[rid] = e
    return out


def bar(reviewer: Reviewer, entry: dict | None, head_sha: str | None, *,
        threshold: int | None = None) -> Bar:
    if reviewer is GREPTILE:
        return _greptile_bar(entry, head_sha, threshold)
    if reviewer is CODERABBIT:
        return _coderabbit_bar(entry, head_sha)
    if reviewer is SUPERAGENT:
        return _superagent_bar(entry, head_sha)
    return _socket_bar(entry, head_sha)


def severity(reviewer: Reviewer, entry: dict | None, greptile_review: dict | None) -> str | None:
    """`defects | nits | clean` for a review-kind reviewer, None when unknown or
    for scanners. Greptile's comes from its semantic read (passed current or
    None); CodeRabbit names its own severities."""
    if reviewer is GREPTILE:
        return (greptile_review or {}).get("severity")
    if reviewer is CODERABBIT:
        return _coderabbit_severity(entry)
    return None


def findings_for_fix(reviewer: Reviewer, entry: dict | None, head_sha: str | None,
                     greptile_review: dict | None) -> list[dict]:
    """Open review findings in the `{headline, class, why, path, line}` shape the
    autofix brief renders. Scanners return nothing: a security finding is a
    human's call, never a fix target."""
    if reviewer is GREPTILE:
        return [f for f in ((greptile_review or {}).get("findings") or []) if isinstance(f, dict)]
    if reviewer is CODERABBIT:
        return _coderabbit_findings_for_fix(entry, head_sha)
    return []


def digest(reviewer: Reviewer, entry: dict | None, b: Bar, head_sha: str | None) -> dict:
    """The compact row projection: what the app, chat, the analyze bundle and
    deep search read."""
    reviewed = (entry or {}).get("reviewed_sha")
    stale: bool | None = (reviewed != head_sha) if reviewed and head_sha else None
    extra = dict((entry or {}).get("extra") or {})
    return {"id": reviewer.id, "label": reviewer.label, "kind": reviewer.kind,
            "status": b.status, "reason": b.reason,
            "score": (entry or {}).get("score"), "score_max": reviewer.score_max,
            "reviewed_sha": reviewed, "stale": stale,
            "open": open_counts(entry, head_sha),
            "observed_at": (entry or {}).get("observed_at"),
            "checks": [{"name": c.get("name"), "conclusion": c.get("conclusion"),
                        "status": c.get("status"), "title": c.get("title")}
                       for c in (entry or {}).get("checks") or []],
            "extra": extra,
            "summary_line": _summary_line_one(reviewer, entry, b, stale)}


def _summary_line_one(reviewer: Reviewer, entry: dict | None, b: Bar, stale: bool | None) -> str:
    if entry is None:
        return f"{reviewer.label} {b.status}"
    if reviewer is GREPTILE and entry.get("score") is not None:
        s = f"{reviewer.label} {entry['score']}/{reviewer.score_max}"
    else:
        counts = open_counts(entry, None)
        s = reviewer.label + (" " + ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items()))
                              if counts else f" {b.status}")
    if stale:
        s += " ⚠stale"
    return s


def summary_line(digests: Iterable[dict]) -> str:
    """One line over every reviewer digest: `Greptile 4/5 ⚠stale · CodeRabbit 2 major · Superagent pass`."""
    return " · ".join(d.get("summary_line") or d.get("label") or "" for d in digests) or "no automated review"


def evidence(entries: dict[str, dict], head_sha: str | None) -> list[dict]:
    """Open bot findings as evidence for the security agents: every reviewer's
    unresolved findings, with reviewer, severity, location and text."""
    out: list[dict] = []
    for rid, entry in entries.items():
        r = REVIEWERS.get(rid)
        if r is None:
            continue
        for f in open_findings(entry, head_sha):
            out.append({"reviewer": r.label, "kind": r.kind, "severity": f.get("severity"),
                        "path": f.get("path"), "line": f.get("line"), "title": f.get("title"),
                        "body": (f.get("body") or "")[:600]})
    return out


def version(reviewer: Reviewer, entry: dict | None) -> str | None:
    """An opaque change token for one reviewer's entry — what a post-retrigger
    wait polls on. None when the bot has left nothing."""
    if entry is None:
        return None
    raw = "|".join([str(entry.get("observed_at") or ""), str(entry.get("score") or ""),
                    str(entry.get("reviewed_sha") or ""), str(len(entry.get("findings") or [])),
                    str((entry.get("extra") or {}).get("actionable") or ""),
                    str((entry.get("extra") or {}).get("concerns") or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def seen_summary(prs: Iterable[Pr]) -> dict:
    """The `reviewers` registry: each reviewer's latest observed activity over
    the open corpus, what `review_policy` auto-detection reads."""
    seen: dict[str, dict] = {}
    for pr in prs:
        if pr.state != "open":
            continue
        for rid, entry in (pr.reviews or {}).items():
            if rid not in REVIEWERS or not isinstance(entry, dict):
                continue
            at = entry.get("observed_at") or (pr.reviews or {}).get("checked_at")
            if not at:
                continue
            cur = seen.setdefault(rid, {"last_observed_at": at, "prs": 0})
            cur["prs"] += 1
            if at > cur["last_observed_at"]:
                cur["last_observed_at"] = at
    return {"seen": seen, "computed_at": storekit.now()}
```

(Leave `_coderabbit_parse/_coderabbit_bar/_coderabbit_severity/_coderabbit_findings_for_fix`, `_superagent_parse/_superagent_bar`, `_socket_parse/_socket_bar` as module functions that `raise NotImplementedError` for now — Tasks 2–4 fill them. `Pr.reviews` is added in Task 5; `seen_summary` is exercised there.)

- [ ] **Step 5: Run** `uv run pytest pipeline/tests/test_reviewers.py -q` → all Greptile tests PASS. Run `uv run ruff check pipeline/reviewers.py pipeline/review_fetch.py` → clean.

- [ ] **Step 6: Commit** `git add pipeline/reviewers.py pipeline/review_fetch.py pipeline/tests/test_reviewers.py && git commit -m "Add the reviewer registry with the Greptile adapter"`.

---

### Task 2: CodeRabbit adapter

**Files:** Modify `pipeline/reviewers.py`; Test `pipeline/tests/test_reviewers.py`

**Interfaces:** Produces `_coderabbit_parse/_coderabbit_bar/_coderabbit_severity/_coderabbit_findings_for_fix`; entry `extra = {"actionable": int|None, "premerge": {"passed": int, "failed": int} | None, "review_id": int|None}`; finding severities `critical|major|minor|nitpick`.

- [ ] **Step 1: Failing tests** (append to `test_reviewers.py`):

```python
CR_REVIEW_BODY = "**Actionable comments posted: 2**\n\n<details><summary>🤖 Prompt</summary>x</details>"
CR_WALKTHROUGH = ("<!-- walkthrough_start -->\n<details><summary>📝 Walkthrough</summary>\n\n"
                  "## Summary by CodeRabbit\n* **New Features**\n  * Adds Y.\n\n"
                  "## Walkthrough\nAdds automatic review child-issue management.\n</details>\n"
                  "<!-- walkthrough_end -->\n<details><summary>🚥 Pre-merge checks | ✅ 4 | ❌ 1</summary>x</details>")
CR_MAJOR = ("_⚠️ Potential issue_ | _🟠 Major_ | _⚡ Quick win_\n\n"
            "**Verify metadata-linked children with the review marker.**\n\nbody")
CR_NIT = "_🧹 Nitpick_ | _🔵 Trivial_\n\n**Prefer const.**\n\nbody"


class TestCodeRabbit:
    def _feed(self, resolved: bool = False, commit: str = HEAD) -> PrFeed:
        return _feed(
            reviews=[{"id": 9, "login": "coderabbitai[bot]", "state": "COMMENTED", "commit": commit,
                      "body": CR_REVIEW_BODY, "at": "2026-06-18T10:48:04Z", "url": "r"}],
            comments=[{"id": 5, "login": "coderabbitai[bot]", "body": CR_WALKTHROUGH,
                       "at": "2026-06-18T10:40:00Z", "updated_at": "2026-06-18T10:50:00Z", "url": "c"}],
            threads=[{"id": 1, "login": "coderabbitai[bot]", "path": "a.ts", "line": 10, "body": CR_MAJOR,
                      "commit": commit, "original_commit": commit, "resolved": resolved, "outdated": False,
                      "at": "2026-06-18T10:48:04Z", "url": "t1"},
                     {"id": 2, "login": "coderabbitai[bot]", "path": "b.ts", "line": 3, "body": CR_NIT,
                      "commit": commit, "original_commit": commit, "resolved": False, "outdated": False,
                      "at": "2026-06-18T10:48:04Z", "url": "t2"}])

    def test_parse(self):
        e = reviewers.parse(reviewers.CODERABBIT, self._feed(), HEAD, None)
        assert e["kind"] == "review" and e["reviewed_sha"] == HEAD and e["score"] is None
        assert e["extra"]["actionable"] == 2 and e["extra"]["premerge"] == {"passed": 4, "failed": 1}
        assert e["extra"]["review_id"] == 9
        assert [f["severity"] for f in e["findings"]] == ["major", "nitpick"]
        assert e["findings"][0]["title"] == "Verify metadata-linked children with the review marker."
        assert "Adds automatic review child-issue management." in e["summary"]
        assert "<details>" not in e["summary"]
        assert e["observed_at"] == "2026-06-18T10:50:00Z"

    def test_bar_fails_on_open_major(self):
        e = reviewers.parse(reviewers.CODERABBIT, self._feed(), HEAD, None)
        b = reviewers.bar(reviewers.CODERABBIT, e, HEAD)
        assert b.status == "fail" and b.reason == "coderabbit: 1 open major finding"

    def test_bar_passes_when_major_resolved(self):
        e = reviewers.parse(reviewers.CODERABBIT, self._feed(resolved=True), HEAD, None)
        assert reviewers.bar(reviewers.CODERABBIT, e, HEAD).status == "pass"

    def test_bar_stale_when_reviewed_elsewhere(self):
        e = reviewers.parse(reviewers.CODERABBIT, self._feed(resolved=True, commit=OLD), HEAD, None)
        assert reviewers.bar(reviewers.CODERABBIT, e, HEAD).status == "stale"

    def test_bar_pending_without_entry(self):
        assert reviewers.bar(reviewers.CODERABBIT, None, HEAD).status == "pending"

    def test_severity_and_fix_findings(self):
        e = reviewers.parse(reviewers.CODERABBIT, self._feed(), HEAD, None)
        assert reviewers.severity(reviewers.CODERABBIT, e, None) == "defects"
        fx = reviewers.findings_for_fix(reviewers.CODERABBIT, e, HEAD, None)
        assert [f["class"] for f in fx] == ["substantive", "nitpick"]
        assert fx[0]["path"] == "a.ts" and fx[0]["line"] == 10
        resolved = reviewers.parse(reviewers.CODERABBIT, self._feed(resolved=True), HEAD, None)
        assert reviewers.severity(reviewers.CODERABBIT, resolved, None) == "nits"
```

- [ ] **Step 2: Run** → NotImplementedError failures.

- [ ] **Step 3: Implement** (replace the CodeRabbit stubs):

```python
_CR_ACTIONABLE_RE = re.compile(r"Actionable comments posted:\s*\*{0,2}(\d+)")
_CR_PREMERGE_RE = re.compile(r"Pre-merge checks\s*\|\s*✅\s*(\d+)\s*\|\s*❌\s*(\d+)")
_CR_SEVERITIES = (("critical", "critical"), ("major", "major"), ("minor", "minor"), ("nitpick", "nitpick"))
_CR_BLOCKING = frozenset({"critical", "major"})
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _coderabbit_severity_of(body: str | None) -> str | None:
    head = (body or "").strip().splitlines()[0].lower() if body else ""
    for needle, sev in _CR_SEVERITIES:
        if needle in head:
            return sev
    return None


def _bold_title(body: str | None) -> str | None:
    m = _BOLD_RE.search(body or "")
    return m.group(1).strip()[:200] if m else None


def _coderabbit_parse(feed: PrFeed, head_sha: str | None, previous: dict | None) -> dict | None:
    checks = _own_checks(CODERABBIT, feed)
    if not feed.conversation:
        return _carry(previous, CODERABBIT, checks)
    reviews = _own(CODERABBIT, feed.reviews)
    comments = _own(CODERABBIT, feed.comments)
    threads = _own(CODERABBIT, feed.threads)
    if not reviews and not comments and not threads and not checks:
        return None
    latest = _latest(reviews)
    walk = _latest([c for c in comments if "Summary by CodeRabbit" in (c.get("body") or "")
                    or "walkthrough_start" in (c.get("body") or "")], "updated_at")
    body = (walk or {}).get("body") or ""
    pm = _CR_PREMERGE_RE.search(body)
    act = _CR_ACTIONABLE_RE.search((latest or {}).get("body") or "")
    findings = [_finding(t, _coderabbit_severity_of(t.get("body")), _bold_title(t.get("body")))
                for t in threads]
    return _entry(CODERABBIT,
                  reviewed_sha=(latest or {}).get("commit"),
                  observed_at=_max_at((latest or {}).get("at"), (walk or {}).get("updated_at"),
                                      (walk or {}).get("at"), *[t.get("at") for t in threads]),
                  score=None, findings=findings,
                  summary=strip_html(body) if body else None, checks=checks,
                  extra={"actionable": int(act.group(1)) if act else None,
                         "premerge": ({"passed": int(pm.group(1)), "failed": int(pm.group(2))}
                                      if pm else None),
                         "review_id": (latest or {}).get("id")})


def _coderabbit_bar(entry: dict | None, head_sha: str | None) -> Bar:
    if entry is None:
        return Bar(PENDING, "awaiting coderabbit review",
                   "CodeRabbit has not reviewed this PR yet.")
    blocking = [f for f in open_findings(entry, head_sha) if f.get("severity") in _CR_BLOCKING]
    if blocking:
        n = len(blocking)
        worst = "critical" if any(f["severity"] == "critical" for f in blocking) else "major"
        return Bar(FAIL, f"coderabbit: {n} open {worst} finding{'s' if n != 1 else ''}",
                   f"CodeRabbit left {n} unresolved {worst} finding{'s' if n != 1 else ''} — "
                   "address or resolve them.")
    reviewed = entry.get("reviewed_sha")
    if reviewed and head_sha and reviewed != head_sha:
        return Bar(STALE, "coderabbit review stale",
                   "CodeRabbit reviewed an earlier commit — a re-review of the current head is needed.")
    return Bar(PASS, None, None)


def _coderabbit_severity(entry: dict | None) -> str | None:
    if entry is None:
        return None
    sevs = {f.get("severity") for f in open_findings(entry, None)}
    if sevs & _CR_BLOCKING:
        return "defects"
    if sevs:
        return "nits"
    return "clean" if entry.get("reviewed_sha") else None


def _coderabbit_findings_for_fix(entry: dict | None, head_sha: str | None) -> list[dict]:
    return [{"headline": f.get("title") or (f.get("body") or "")[:120],
             "class": "substantive" if f.get("severity") in _CR_BLOCKING else "nitpick",
             "why": (f.get("body") or "")[:500], "path": f.get("path"), "line": f.get("line")}
            for f in open_findings(entry, head_sha)]
```

- [ ] **Step 4: Run** the file → PASS. **Step 5: Commit** `"Add the CodeRabbit reviewer adapter"`.

---

### Task 3: Superagent adapter

**Files:** Modify `pipeline/reviewers.py`; Test `pipeline/tests/test_reviewers.py`

**Interfaces:** `extra = {"trust_score": int|None, "trust_verdict": str|None, "concerns": int|None}`; finding severities `P1|P2|P3`.

- [ ] **Step 1: Failing tests**:

```python
SA_P1 = ("<!-- brin-pr-finding -->\n**P1:** Hidden webhook plugin with hardcoded private IP default "
         "exfiltrates issue data\n\nNew webhook plugin …")


class TestSuperagent:
    def _checks(self, scan: str = "success", status: str = "completed") -> list[dict]:
        return [{"app": "superagent-security", "name": "Superagent Security Scan", "status": status,
                 "conclusion": scan if status == "completed" else None,
                 "title": "PR requires security review" if scan == "action_required" else "PR scan passed",
                 "summary": "2 security concern(s) detected." if scan == "action_required" else "No suspicious PR changes were detected.", "url": "c1"},
                {"app": "superagent-security", "name": "Superagent Supply Chain Scan", "status": "completed",
                 "conclusion": "neutral", "title": "Supply chain scan inconclusive", "summary": "motion (npm) changed", "url": "c2"},
                {"app": "superagent-security", "name": "Contributor trust", "status": "completed",
                 "conclusion": "success", "title": "Contributor verified", "summary": "Score: 89/100 · Verdict: safe", "url": "c3"}]

    def test_parse_with_findings(self):
        feed = _feed(
            reviews=[{"id": 3, "login": "superagent-security[bot]", "state": "COMMENTED", "commit": HEAD,
                      "body": "<!-- brin-pr-finding -->\nSuperagent found 2 security concern(s).",
                      "at": "2026-08-21T15:21:03Z", "url": "r"}],
            threads=[{"id": 1, "login": "superagent-security[bot]", "path": "m.ts", "line": 28, "body": SA_P1,
                      "commit": HEAD, "original_commit": HEAD, "resolved": False, "outdated": False,
                      "at": "2026-08-21T15:21:03Z", "url": "t"}],
            check_runs=self._checks("action_required"))
        e = reviewers.parse(reviewers.SUPERAGENT, feed, HEAD, None)
        assert e["kind"] == "scanner" and e["reviewed_sha"] == HEAD
        assert e["extra"] == {"trust_score": 89, "trust_verdict": "safe", "concerns": 2}
        assert e["findings"][0]["severity"] == "P1"
        assert e["findings"][0]["title"].startswith("Hidden webhook plugin")
        assert len(e["checks"]) == 3
        b = reviewers.bar(reviewers.SUPERAGENT, e, HEAD)
        assert b.status == "fail" and b.reason == "superagent: 1 open P1 finding"

    def test_parse_checks_only_passes(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=self._checks()), HEAD, None)
        assert e is not None and e["findings"] == [] and e["reviewed_sha"] == HEAD
        assert reviewers.bar(reviewers.SUPERAGENT, e, HEAD).status == "pass"

    def test_bar_pending_while_scan_runs(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=self._checks(status="in_progress")), HEAD, None)
        b = reviewers.bar(reviewers.SUPERAGENT, e, HEAD)
        assert b.status == "pending" and b.reason == "superagent scan pending" and b.ask is None

    def test_bar_fails_on_action_required_without_threads(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=self._checks("action_required")), HEAD, None)
        assert reviewers.bar(reviewers.SUPERAGENT, e, HEAD).status == "fail"

    def test_no_entry_is_pending(self):
        assert reviewers.parse(reviewers.SUPERAGENT, _feed(), HEAD, None) is None
        assert reviewers.bar(reviewers.SUPERAGENT, None, HEAD).status == "pending"

    def test_scanner_never_feeds_fix(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=self._checks()), HEAD, None)
        assert reviewers.findings_for_fix(reviewers.SUPERAGENT, e, HEAD, None) == []
        assert reviewers.severity(reviewers.SUPERAGENT, e, None) is None
```

- [ ] **Step 2: Run** → failing. **Step 3: Implement**:

```python
_SA_CONCERNS_RE = re.compile(r"Superagent found\s+(\d+)\s+security concern")
_SA_PRIORITY_RE = re.compile(r"\*\*(P[1-3]):\*\*\s*(.+)")
_SA_TRUST_RE = re.compile(r"Score:\s*(\d+)\s*/\s*100\s*·\s*Verdict:\s*(\w+)")
_SA_BLOCKING = frozenset({"P1", "P2"})
_SA_SCAN = "Superagent Security Scan"


def _superagent_parse(feed: PrFeed, head_sha: str | None, previous: dict | None) -> dict | None:
    checks = _own_checks(SUPERAGENT, feed)
    if not feed.conversation:
        return _carry(previous, SUPERAGENT, checks)
    reviews = _own(SUPERAGENT, feed.reviews)
    threads = _own(SUPERAGENT, feed.threads)
    if not reviews and not threads and not checks:
        return None
    latest = _latest(reviews)
    concerns = _SA_CONCERNS_RE.search((latest or {}).get("body") or "")
    findings = []
    for t in threads:
        m = _SA_PRIORITY_RE.search(t.get("body") or "")
        findings.append(_finding(t, m.group(1) if m else None, m.group(2).strip()[:200] if m else None))
    trust = next((_SA_TRUST_RE.search(c.get("summary") or "") for c in checks
                  if c.get("name") == "Contributor trust"), None)
    reviewed = (latest or {}).get("commit") or (head_sha if checks else None)
    return _entry(SUPERAGENT, reviewed_sha=reviewed,
                  observed_at=_max_at((latest or {}).get("at"), *[t.get("at") for t in threads]),
                  score=None, findings=findings,
                  summary=strip_html((latest or {}).get("body") or "") or None, checks=checks,
                  extra={"trust_score": int(trust.group(1)) if trust else None,
                         "trust_verdict": trust.group(2).lower() if trust else None,
                         "concerns": int(concerns.group(1)) if concerns else None})


def _check_named(entry: dict | None, name: str) -> dict | None:
    return next((c for c in (entry or {}).get("checks") or [] if c.get("name") == name), None)


def _superagent_bar(entry: dict | None, head_sha: str | None) -> Bar:
    if entry is None:
        return Bar(PENDING, "awaiting superagent scan", None)
    blocking = [f for f in open_findings(entry, head_sha) if f.get("severity") in _SA_BLOCKING]
    scan = _check_named(entry, _SA_SCAN)
    if blocking:
        n = len(blocking)
        worst = "P1" if any(f["severity"] == "P1" for f in blocking) else "P2"
        return Bar(FAIL, f"superagent: {n} open {worst} finding{'s' if n != 1 else ''}",
                   f"Superagent flagged {n} {worst} security concern{'s' if n != 1 else ''} — "
                   "resolve or refute them.")
    if scan is not None and scan.get("conclusion") in ("action_required", "failure"):
        return Bar(FAIL, "superagent: scan requires security review",
                   "Superagent's security scan requires review — resolve its concerns.")
    if scan is not None and scan.get("status") != "completed":
        return Bar(PENDING, "superagent scan pending", None)
    if scan is None and not entry.get("findings"):
        return Bar(PENDING, "awaiting superagent scan", None)
    return Bar(PASS, None, None)
```

- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `"Add the Superagent scanner adapter"`.

---

### Task 4: Socket adapter

**Files:** Modify `pipeline/reviewers.py`; Test `pipeline/tests/test_reviewers.py`

**Interfaces:** `extra = {"alerts_status": str|None, "report_url": str|None}`; Socket's bar is `na` with no entry.

- [ ] **Step 1: Failing tests**:

```python
class TestSocket:
    def _checks(self, alerts: str = "success") -> list[dict]:
        return [{"app": "socket-security", "name": "Socket Security: Pull Request Alerts", "status": "completed",
                 "conclusion": alerts, "title": f"Pull Request #1 Alerts: {alerts.title()}",
                 "summary": "|Report|Status|Message|", "url": "s1"},
                {"app": "socket-security", "name": "Socket Security: Project Report", "status": "completed",
                 "conclusion": "success", "title": "Project Report: Success", "summary": "x",
                 "url": "https://socket.dev/dashboard/org/x/sbom/abc"}]

    def test_parse_and_pass(self):
        feed = _feed(comments=[{"id": 7, "login": "socket-security[bot]",
                                "body": "**Review the following changes in direct dependencies.** <table>…</table>",
                                "at": "2026-08-21T00:00:00Z", "updated_at": None, "url": "c"}],
                     check_runs=self._checks())
        e = reviewers.parse(reviewers.SOCKET, feed, HEAD, None)
        assert e["kind"] == "scanner" and e["reviewed_sha"] == HEAD
        assert e["extra"]["alerts_status"] == "success"
        assert e["extra"]["report_url"] == "https://socket.dev/dashboard/org/x/sbom/abc"
        assert e["summary"].startswith("Review the following changes")
        assert reviewers.bar(reviewers.SOCKET, e, HEAD).status == "pass"

    def test_bar_fails_on_alerts(self):
        e = reviewers.parse(reviewers.SOCKET, _feed(check_runs=self._checks("failure")), HEAD, None)
        b = reviewers.bar(reviewers.SOCKET, e, HEAD)
        assert b.status == "fail" and b.reason == "socket: new dependency alerts"

    def test_neutral_passes_and_absent_is_na(self):
        e = reviewers.parse(reviewers.SOCKET, _feed(check_runs=self._checks("neutral")), HEAD, None)
        assert reviewers.bar(reviewers.SOCKET, e, HEAD).status == "pass"
        assert reviewers.parse(reviewers.SOCKET, _feed(), HEAD, None) is None
        assert reviewers.bar(reviewers.SOCKET, None, HEAD).status == "na"
```

- [ ] **Step 2: Implement**:

```python
_SOCKET_ALERTS = "Socket Security: Pull Request Alerts"
_SOCKET_REPORT = "Socket Security: Project Report"


def _socket_parse(feed: PrFeed, head_sha: str | None, previous: dict | None) -> dict | None:
    checks = _own_checks(SOCKET, feed)
    if not feed.conversation:
        return _carry(previous, SOCKET, checks)
    comments = _own(SOCKET, feed.comments)
    if not comments and not checks:
        return None
    latest = _latest(comments, "updated_at") or _latest(comments)
    alerts = next((c for c in checks if c.get("name") == _SOCKET_ALERTS), None)
    report = next((c for c in checks if c.get("name") == _SOCKET_REPORT), None)
    return _entry(SOCKET, reviewed_sha=head_sha if checks else None,
                  observed_at=_max_at((latest or {}).get("updated_at"), (latest or {}).get("at")),
                  score=None, findings=[],
                  summary=strip_html((latest or {}).get("body") or "") or None, checks=checks,
                  extra={"alerts_status": (alerts or {}).get("conclusion"),
                         "report_url": (report or {}).get("url")})


def _socket_bar(entry: dict | None, head_sha: str | None) -> Bar:
    alerts = _check_named(entry, _SOCKET_ALERTS)
    if entry is None or alerts is None:
        return Bar(NA, None, None)
    if alerts.get("status") != "completed":
        return Bar(PENDING, "socket scan pending", None)
    if alerts.get("conclusion") == "failure":
        return Bar(FAIL, "socket: new dependency alerts",
                   "Socket flagged new dependency alerts — review the dependency changes.")
    return Bar(PASS, None, None)
```

- [ ] **Step 3: Run** whole test file → PASS; `uv run ruff check pipeline/reviewers.py` clean. **Step 4: Commit** `"Add the Socket scanner adapter"`.

---

### Task 5: Store shape — `reviews` section, `reviewers` registry, model accessors, schema 19

**Files:**
- Modify: `pipeline/store.py` (PR_SECTIONS, validate_pr, registry accessors), `pipeline/schema.py` (version + changelog), `pipeline/freshness.py` (SHA_BOUND), `pipeline/model.py` (accessors, `set_reviews`, `stage_facts`, Greptile accessors read `reviews`; delete the `review_*` single-provider accessors)
- Test: `pipeline/tests/test_store.py`, `pipeline/tests/test_model.py`, `pipeline/tests/test_reviewers.py` (seen_summary), `pipeline/tests/test_schema_fingerprint.py` / `test_schema_guard.py` (version constant — read them first; update the expected version)

**Interfaces:**
- Produces: `Pr.reviews -> dict | None`, `Pr.review_entry(rid: str) -> dict | None`, `Pr.set_reviews(payload: dict, *, head_sha: str | None = None)`, `Pr.stage_facts(..., reviews: dict | None = None)`, `Store.load_reviewers() -> dict`, `Store.save_reviewers(registry: dict) -> None`. `Pr.greptile`, `Pr.greptile_reviewed_sha`, `Pr.greptile_stale`, `Pr.greptile_severity`, `Pr.greptile_review` keep their names; `Pr.review_score/review_reviewed_sha/review_section/review_stale/review_severity` are removed.

- [ ] **Step 1: Failing tests**

`pipeline/tests/test_store.py` (append):
```python
def test_reviews_section_validates(tmp_path):
    from pipeline import store as st
    rec = {"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "h"},
           "reviews": {"greptile": {"kind": "review", "score": 4, "findings": [], "checks": []},
                       "checked_at": "x", "against_head_sha": "h"}}
    st.validate_pr(rec)  # ok
    bad = dict(rec, reviews={"greptile": {"kind": "bogus", "findings": []}})
    with pytest.raises(st.ValidationError):
        st.validate_pr(bad)
    bad2 = dict(rec, reviews={"greptile": {"kind": "review", "findings": "nope"}})
    with pytest.raises(st.ValidationError):
        st.validate_pr(bad2)


def test_reviewers_registry_roundtrip(tmp_path):
    s = Store(str(tmp_path / "s"))
    assert s.load_reviewers() == {"seen": {}, "computed_at": None}
    s.save_reviewers({"seen": {"greptile": {"last_observed_at": "2026-08-21T00:00:00Z", "prs": 3}},
                      "computed_at": "2026-08-21T01:00:00Z"})
    assert s.load_reviewers()["seen"]["greptile"]["prs"] == 3
```
(Use the file's existing `Store` construction idiom — read the top of `test_store.py` first and match it.)

`pipeline/tests/test_model.py` (append):
```python
def test_greptile_accessors_read_reviews_section():
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": "h2"},
                   "reviews": {"greptile": {"kind": "review", "score": 4, "reviewed_sha": "h1"}}})
    assert pr.greptile == 4 and pr.greptile_reviewed_sha == "h1" and pr.greptile_stale is True
    assert pr.review_entry("greptile")["score"] == 4 and pr.review_entry("socket") is None
    assert not hasattr(pr, "review_score")


def test_stage_facts_stamps_reviews():
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": "h"}})
    pr.stage_facts({"title": "t", "state": "open", "head_sha": "h"}, reviews={"greptile": {"kind": "review"}})
    assert pr.rec["reviews"]["against_head_sha"] == "h" and "checked_at" in pr.rec["reviews"]
```

`pipeline/tests/test_reviewers.py` (append):
```python
from pipeline.model import Pr

def test_seen_summary_over_open_corpus():
    prs = [Pr(None, {"pr": 1, "meta": {"state": "open", "head_sha": "h"},
                     "reviews": {"greptile": {"kind": "review", "observed_at": "2026-08-20T00:00:00Z"},
                                 "socket": {"kind": "scanner", "observed_at": None},
                                 "checked_at": "2026-08-21T00:00:00Z"}}),
           Pr(None, {"pr": 2, "meta": {"state": "closed", "head_sha": "h"},
                     "reviews": {"coderabbit": {"kind": "review", "observed_at": "2026-08-21T00:00:00Z"}}})]
    seen = reviewers.seen_summary(prs)["seen"]
    assert seen["greptile"] == {"last_observed_at": "2026-08-20T00:00:00Z", "prs": 1}
    assert seen["socket"]["last_observed_at"] == "2026-08-21T00:00:00Z"   # falls back to the section stamp
    assert "coderabbit" not in seen
```

- [ ] **Step 2: Implement**
  - `store.py`: add `"reviews"` to `PR_SECTIONS`; in `validate_pr` after the greptile_review block:
    ```python
    rv = rec.get("reviews")
    if rv:
        from pipeline import reviewers
        for rid, entry in rv.items():
            if rid in ("checked_at", "against_head_sha") or rid not in reviewers.REVIEWERS:
                continue
            if not isinstance(entry, dict) or entry.get("kind") not in reviewers.KINDS:
                raise ValidationError(f"reviews.{rid}.kind: {(entry or {}).get('kind')!r} not in {list(reviewers.KINDS)}")
            for field_name in ("findings", "checks"):
                val = entry.get(field_name, [])
                if not isinstance(val, list) or not all(isinstance(x, dict) for x in val):
                    raise ValidationError(f"reviews.{rid}.{field_name}: must be a list of dicts")
    ```
    (Import `reviewers` at module top if no cycle: `reviewers` imports only `storekit` at runtime — safe; prefer the top-level import.) Add registry accessors beside `load_threats`:
    ```python
    def load_reviewers(self) -> dict:
        return self._load_registry("reviewers", {"seen": {}, "computed_at": None})

    def save_reviewers(self, registry: dict) -> None:
        self._save_registry("reviewers", registry)
    ```
  - `schema.py`: changelog entry `# 19 — PR records carry a `reviews` section (every automated reviewer's and scanner's normalized feedback, keyed by reviewer id) and the `reviewers` registry records each bot's latest activity; an older reader finds no Greptile score in `signals` and judges every PR un-reviewed.` and `STORE_SCHEMA_VERSION = 19`.
  - `freshness.py`: `SHA_BOUND` gains `"reviews"` (after `"signals"`).
  - `model.py`: replace lines 149–217 with:
    ```python
    @property
    def reviews(self) -> dict | None:
        """Every automated reviewer's normalized feedback on this PR, keyed by
        reviewer id (pipeline.reviewers), stamped against the head it was read at."""
        return self.rec.get("reviews")

    def review_entry(self, reviewer_id: str) -> dict | None:
        entry = (self.rec.get("reviews") or {}).get(reviewer_id)
        return entry if isinstance(entry, dict) else None

    @property
    def greptile(self) -> int | None:
        return (self.review_entry("greptile") or {}).get("score")

    @property
    def greptile_reviewed_sha(self) -> str | None:
        """The commit Greptile's score describes; None when unknown."""
        return (self.review_entry("greptile") or {}).get("reviewed_sha")

    @property
    def greptile_severity(self) -> str | None:  (unchanged body)
    @property
    def greptile_review(self) -> dict | None:   (unchanged)
    @property
    def greptile_stale(self) -> bool | None:    (unchanged body)
    ```
    Remove the `from pipeline import review_policy` import if nothing else in model uses it (grep). Add `set_reviews` beside `set_signals` and `reviews: dict | None = None` to `apply_facts`/`stage_facts` (`if reviews is not None: _stamp(self.rec, "reviews", reviews, None)`).
- [ ] **Step 3: Run** `uv run pytest pipeline/tests/test_store.py pipeline/tests/test_model.py pipeline/tests/test_reviewers.py pipeline/tests/test_schema_fingerprint.py pipeline/tests/test_schema_guard.py -q`. Expect the schema tests to need their expected-version literal bumped to 19 (fix them). Other suites will fail on the removed `review_*` accessors until Task 6/7 — that is expected at this checkpoint.
- [ ] **Step 4: Commit** `"Store the reviews section and the reviewers registry (schema 19)"`.

---

### Task 6: `pipeline/ci_signal.py` + `review_policy` rewrite + settings

**Files:**
- Create: `pipeline/ci_signal.py`, `pipeline/tests/test_ci_signal.py`
- Rewrite: `pipeline/review_policy.py`; Modify: `pipeline/settings.py`; Rewrite tests: `pipeline/tests/test_review_policy.py`
- Test fixtures: keep both `conftest.py` pins (`TRIAGE_REVIEW_PROVIDER=greptile` → explicit mode).

**Interfaces:**
- `ci_signal.verdict(check_runs: list[dict], statuses: list[dict], *, exclude_apps: frozenset[str] = reviewers.app_slugs()) -> str | None` (`passing|failing|pending|None`), `ci_signal.from_graphql_contexts(nodes: list[dict]) -> tuple[list[dict], list[dict]]` (check runs `{app,name,status,conclusion,title,summary,url}` + statuses `{context,state}`), `ci_signal.from_rest_check_runs(check_runs: list[dict]) -> list[dict]` (normalizes REST `app.slug`, `output.title/summary`, `html_url`).
- `review_policy.ReviewPolicy(mode, explicit, active_days, threshold)`, `policy() -> ReviewPolicy`, `active_reviewers(kind: str | None = None) -> list[Reviewer]`, `is_active(rid) -> bool`, `bar(pr, reviewer) -> Bar`, `Blocker(reviewer, bar)`, `clean_blockers(pr, kind) -> list[Blocker]`, `merge_bar_sentence() -> str`, `describe() -> list[dict]`, `reset()` (clears the seen cache), `_load_seen() -> dict` (the monkeypatch seam; reads `Store().load_reviewers()["seen"]`).
- `settings.parse_review_provider(raw) -> tuple[str, tuple[str, ...]]` returns `("auto", ())`, `("none", ())`, or `("explicit", ("greptile", ...))`; `settings.review_provider()` same; `settings.reviewer_active_days() -> int` (env `TRIAGE_REVIEWER_ACTIVE_DAYS`, default 14); `review_threshold()` unchanged.

- [ ] **Step 1: Tests** — `test_ci_signal.py`:
```python
from pipeline import ci_signal

def _run(app, conclusion, status="completed", name="x"):
    return {"app": app, "name": name, "status": status, "conclusion": conclusion, "title": None, "summary": None, "url": None}

def test_reviewer_apps_are_excluded():
    runs = [_run("github-actions", "success"), _run("greptile-apps", "failure"), _run("superagent-security", "action_required")]
    assert ci_signal.verdict(runs, []) == "passing"

def test_failure_outranks_pending_and_statuses_count():
    assert ci_signal.verdict([_run("github-actions", None, "in_progress")], [{"context": "c", "state": "failure"}]) == "failing"
    assert ci_signal.verdict([_run("github-actions", None, "in_progress")], []) == "pending"
    assert ci_signal.verdict([], []) is None
    assert ci_signal.verdict([_run("greptile-apps", "failure")], []) is None

def test_from_graphql_contexts():
    nodes = [{"__typename": "CheckRun", "name": "Build", "status": "COMPLETED", "conclusion": "SUCCESS",
              "title": "ok", "summary": "s", "detailsUrl": "d", "url": "u", "checkSuite": {"app": {"slug": "github-actions"}}},
             {"__typename": "StatusContext", "context": "lint", "state": "PENDING"}]
    runs, statuses = ci_signal.from_graphql_contexts(nodes)
    assert runs == [{"app": "github-actions", "name": "Build", "status": "completed", "conclusion": "success",
                     "title": "ok", "summary": "s", "url": "u"}]
    assert statuses == [{"context": "lint", "state": "pending"}]
```
`test_review_policy.py` (rewrite fully):
```python
"""review_policy: which reviewers gate, detected or configured."""
import pytest
from pipeline import review_policy, reviewers, settings
from pipeline.model import Pr

HEAD = "h" * 40

def test_parse_modes():
    assert settings.parse_review_provider(None) == ("auto", ())
    assert settings.parse_review_provider("") == ("auto", ())
    assert settings.parse_review_provider("none") == ("none", ())
    assert settings.parse_review_provider("Greptile") == ("explicit", ("greptile",))
    assert settings.parse_review_provider("greptile, coderabbit,socket") == ("explicit", ("greptile", "coderabbit", "socket"))
    with pytest.raises(SystemExit):
        settings.parse_review_provider("bogus")

def test_explicit_mode_ignores_detection(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setattr(review_policy, "_load_seen", lambda: {"coderabbit": {"last_observed_at": "2999-01-01T00:00:00Z", "prs": 1}})
    review_policy.reset()
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile"]
    assert review_policy.active_reviewers(reviewers.SCANNER) == []

def test_none_mode(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    review_policy.reset()
    assert review_policy.active_reviewers() == []
    assert review_policy.clean_blockers(Pr(None, {"meta": {"head_sha": HEAD}}), reviewers.REVIEW) == []

def test_auto_mode_uses_activity_window(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "auto")
    monkeypatch.setenv("TRIAGE_REVIEWER_ACTIVE_DAYS", "14")
    monkeypatch.setattr(review_policy, "_load_seen", lambda: {
        "greptile": {"last_observed_at": "2026-08-20T00:00:00Z", "prs": 10},
        "coderabbit": {"last_observed_at": "2026-06-18T00:00:00Z", "prs": 184},
        "superagent": {"last_observed_at": "2026-08-21T00:00:00Z", "prs": 3}})
    monkeypatch.setattr(review_policy, "_today", lambda: "2026-08-21")
    review_policy.reset()
    assert [r.id for r in review_policy.active_reviewers()] == ["greptile", "superagent"]
    assert review_policy.is_active("coderabbit") is False

def test_clean_blockers_by_kind(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,coderabbit,superagent")
    review_policy.reset()
    pr = Pr(None, {"meta": {"head_sha": HEAD},
                   "reviews": {"greptile": {"kind": "review", "score": 5, "reviewed_sha": HEAD, "findings": [], "checks": []},
                               "superagent": {"kind": "scanner", "reviewed_sha": HEAD, "findings": [
                                   {"severity": "P1", "resolved": False, "outdated": False}], "checks": []}}})
    rev = review_policy.clean_blockers(pr, reviewers.REVIEW)
    assert [(b.reviewer.id, b.bar.status) for b in rev] == [("coderabbit", "pending")]
    scan = review_policy.clean_blockers(pr, reviewers.SCANNER)
    assert [(b.reviewer.id, b.bar.reason) for b in scan] == [("superagent", "superagent: 1 open P1 finding")]

def test_threshold_override_applies_to_greptile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.setenv("TRIAGE_REVIEW_THRESHOLD", "4")
    review_policy.reset()
    pr = Pr(None, {"meta": {"head_sha": HEAD}, "reviews": {"greptile": {"kind": "review", "score": 4, "reviewed_sha": HEAD, "findings": [], "checks": []}}})
    assert review_policy.bar(pr, reviewers.GREPTILE).status == "pass"

def test_merge_bar_sentence(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,superagent")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    review_policy.reset()
    s = review_policy.merge_bar_sentence()
    assert "Greptile at 5/5" in s and "Superagent" in s and "CI passing" in s
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    review_policy.reset()
    assert review_policy.merge_bar_sentence() == "CI passing, mergeable (no conflicts)"

def test_describe(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    review_policy.reset()
    d = {x["id"]: x for x in review_policy.describe()}
    assert d["greptile"]["active"] is True and d["greptile"]["retrigger"] is True and d["greptile"]["threshold"] == 5
    assert d["socket"]["active"] is False and d["socket"]["kind"] == "scanner"
```

- [ ] **Step 2: Implement `ci_signal.py`**:
```python
"""CI verdict from GitHub's check runs and commit statuses.

Runs owned by a registry reviewer (pipeline.reviewers) are left out: a
reviewer's own check reads under the reviewer's name, so CI reflects the
repository's own workflows."""
from __future__ import annotations

from pipeline import reviewers

FAIL_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"})
OK_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


def verdict(check_runs: list[dict], statuses: list[dict], *,
            exclude_apps: frozenset[str] | None = None) -> str | None:
    """'passing' | 'failing' | 'pending', or None when nothing counts. Failure
    outranks pending outranks passing; skipped/neutral do not fail a run."""
    excluded = reviewers.app_slugs() if exclude_apps is None else exclude_apps
    saw_any = saw_fail = saw_pending = False
    for run in check_runs:
        if run.get("app") in excluded:
            continue
        saw_any = True
        if run.get("status") != "completed":
            saw_pending = True
        elif run.get("conclusion") in FAIL_CONCLUSIONS:
            saw_fail = True
        elif run.get("conclusion") not in OK_CONCLUSIONS:
            saw_pending = True
    for st in statuses:
        saw_any = True
        if st.get("state") in ("failure", "error"):
            saw_fail = True
        elif st.get("state") == "pending":
            saw_pending = True
    if not saw_any:
        return None
    return "failing" if saw_fail else "pending" if saw_pending else "passing"


def from_rest_check_runs(check_runs: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in check_runs:
        output = r.get("output") or {}
        out.append({"app": (r.get("app") or {}).get("slug"), "name": r.get("name"),
                    "status": r.get("status"), "conclusion": r.get("conclusion"),
                    "title": output.get("title"), "summary": output.get("summary"),
                    "url": r.get("html_url")})
    return out


def from_graphql_contexts(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """statusCheckRollup.contexts nodes → (check runs, statuses), lower-cased."""
    runs: list[dict] = []
    statuses: list[dict] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if node.get("__typename") == "CheckRun":
            runs.append({"app": ((node.get("checkSuite") or {}).get("app") or {}).get("slug"),
                         "name": node.get("name"),
                         "status": (node.get("status") or "").lower() or None,
                         "conclusion": (node.get("conclusion") or "").lower() or None,
                         "title": node.get("title"), "summary": node.get("summary"),
                         "url": node.get("url") or node.get("detailsUrl")})
        elif node.get("__typename") == "StatusContext":
            statuses.append({"context": node.get("context"),
                             "state": (node.get("state") or "").lower() or None})
    return runs, statuses
```
- [ ] **Step 3: Implement `settings.py`** — replace `parse_review_provider`/`review_provider`:
```python
def parse_review_provider(raw: str | None) -> tuple[str, tuple[str, ...]]:
    """(mode, ids): "auto" detects active reviewers from the repository's PR
    data, "none" requires no external review, "explicit" names exactly the
    reviewer ids that gate (a comma list of pipeline.reviewers ids). An unknown
    id is a hard error so a typo never silently disables a bar."""
    from pipeline import reviewers
    text = (raw or "auto").strip().lower()
    if text in ("", "auto"):
        return ("auto", ())
    if text == "none":
        return ("none", ())
    ids = tuple(p.strip() for p in text.split(",") if p.strip())
    unknown = [i for i in ids if i not in reviewers.REVIEWERS]
    if unknown:
        raise SystemExit(
            f"TRIAGE_REVIEW_PROVIDER: unknown reviewer(s) {unknown!r}; use 'auto', 'none', or a "
            f"comma list of {sorted(reviewers.REVIEWERS)}. Set it in .env — see .env.example.")
    return ("explicit", ids)


def review_provider() -> tuple[str, tuple[str, ...]]:
    return parse_review_provider(os.environ.get("TRIAGE_REVIEW_PROVIDER"))


def reviewer_active_days() -> int:
    """How recently a reviewer must have posted on an open PR to count as
    active in auto mode."""
    raw = os.environ.get("TRIAGE_REVIEWER_ACTIVE_DAYS")
    return int(raw) if raw else 14
```
`review_threshold()` docstring: "Override of Greptile's pass score. None → 5."
- [ ] **Step 4: Rewrite `review_policy.py`**:
```python
"""The ONE review/merge-provider policy: which automated reviewers and scanners
gate a clean merge on this repository, and at what bar.

`TRIAGE_REVIEW_PROVIDER` is `auto` (every registry reviewer seen on an open PR
within `TRIAGE_REVIEWER_ACTIVE_DAYS`, read from the store's `reviewers`
registry that ingest recomputes), `none`, or an explicit comma list of reviewer
ids. Every consumer — the gates, the checks rollup, the prompts, the app
capabilities — reads the active set and each reviewer's bar here."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pipeline import reviewers, settings
from pipeline.reviewers import Bar, Reviewer

if TYPE_CHECKING:
    from pipeline.model import Pr

_SEEN_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class ReviewPolicy:
    mode: str                       # "auto" | "none" | "explicit"
    explicit: tuple[str, ...]
    active_days: int
    threshold: int | None           # Greptile score bar override


@dataclass(frozen=True)
class Blocker:
    reviewer: Reviewer
    bar: Bar


def policy() -> ReviewPolicy:
    mode, ids = settings.review_provider()
    return ReviewPolicy(mode, ids, settings.reviewer_active_days(), settings.review_threshold())


_seen_cache: tuple[float, dict] | None = None


def reset() -> None:
    global _seen_cache
    _seen_cache = None


def _load_seen() -> dict:
    from pipeline.store import Store
    return dict(Store().load_reviewers().get("seen") or {})


def _seen() -> dict:
    global _seen_cache
    now = time.monotonic()
    if _seen_cache is None or now - _seen_cache[0] > _SEEN_TTL_SECONDS:
        try:
            _seen_cache = (now, _load_seen())
        except Exception:
            _seen_cache = (now, {})
    return _seen_cache[1]


def _today() -> str:
    return date.today().isoformat()


def _recent(stamp: str | None, days: int) -> bool:
    if not stamp:
        return False
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    today = datetime.fromisoformat(_today()).replace(tzinfo=timezone.utc)
    return at >= today - timedelta(days=days)


def active_reviewers(kind: str | None = None) -> list[Reviewer]:
    """The reviewers that gate this repository, in registry order."""
    p = policy()
    if p.mode == "none":
        ids: list[str] = []
    elif p.mode == "explicit":
        ids = list(p.explicit)
    else:
        seen = _seen()
        ids = [rid for rid in reviewers.REVIEWERS
               if _recent((seen.get(rid) or {}).get("last_observed_at"), p.active_days)]
    out = [reviewers.REVIEWERS[rid] for rid in reviewers.REVIEWERS if rid in ids]
    return [r for r in out if kind is None or r.kind == kind]


def is_active(reviewer_id: str) -> bool:
    return any(r.id == reviewer_id for r in active_reviewers())


def bar(pr: Pr, reviewer: Reviewer) -> Bar:
    """`reviewer`'s bar on `pr`; `na` when the reviewer is not active."""
    if not is_active(reviewer.id):
        return Bar(reviewers.NA, None, None)
    return reviewers.bar(reviewer, pr.review_entry(reviewer.id), pr.head_sha,
                         threshold=policy().threshold)


def clean_blockers(pr: Pr, kind: str) -> list[Blocker]:
    """Every active reviewer of `kind` whose bar is not pass/na, in registry order."""
    out: list[Blocker] = []
    for r in active_reviewers(kind):
        b = bar(pr, r)
        if b.status not in (reviewers.PASS, reviewers.NA):
            out.append(Blocker(r, b))
    return out


def bar_label(reviewer: Reviewer) -> str:
    """The reviewer's pass condition as prose."""
    if reviewer is reviewers.GREPTILE:
        th = policy().threshold if policy().threshold is not None else reviewer.score_max
        return f"{reviewer.label} at {th}/{reviewer.score_max}"
    if reviewer is reviewers.CODERABBIT:
        return f"{reviewer.label} with no open Critical/Major findings"
    if reviewer is reviewers.SUPERAGENT:
        return f"{reviewer.label} scan clean (no open P1/P2)"
    return f"{reviewer.label} with no new dependency alerts"


def merge_bar_sentence() -> str:
    """The hard merge bar, as the ANALYZE and chat prompts state it."""
    parts = [bar_label(r) for r in active_reviewers(reviewers.REVIEW)]
    parts += [bar_label(r) for r in active_reviewers(reviewers.SCANNER)]
    if parts:
        return "external review: " + ", ".join(parts) + "; CI passing, mergeable (no conflicts)"
    return "CI passing, mergeable (no conflicts)"


def describe() -> list[dict]:
    """The capabilities descriptor: every registry reviewer with its activity."""
    p = policy()
    active = {r.id for r in active_reviewers()}
    out: list[dict] = []
    for r in reviewers.REVIEWERS.values():
        out.append({"id": r.id, "label": r.label, "kind": r.kind, "active": r.id in active,
                    "retrigger": r.retrigger_mention is not None, "score_max": r.score_max,
                    "threshold": (p.threshold if p.threshold is not None else r.score_max)
                    if r is reviewers.GREPTILE else None,
                    "bar_label": bar_label(r)})
    return out
```
- [ ] **Step 5: Run** `uv run pytest pipeline/tests/test_ci_signal.py pipeline/tests/test_review_policy.py -q` → PASS. **Step 6: Commit** `"Rewrite review_policy around the reviewer registry; add ci_signal"`.

---

### Task 7: Gates — multi-reviewer `pr_clean`, structured `bar_asks`, `fix_huntable`

**Files:** Modify `pipeline/gates.py` (`pr_clean` 210–241, `fix_huntable` 555–600, `bar_asks` 1389–1416, `_STALE_BAR_REASONS` 1420, `merge_demotion` 1424–1451); Test `pipeline/tests/test_gates.py`, `pipeline/tests/test_derived_disposition.py`.

**Interfaces:** `bar_asks(reasons: list[str], pr: Pr | None = None) -> list[str]`; `pr_clean` reasons gain `"reviews stale or missing"` and each blocker's `bar.reason`; `_STALE_BAR_REASONS` gains `"reviews stale or missing"`.

- [ ] **Step 1: Update the `_pr` fixture in `test_gates.py`** to the new shape:
```python
def _pr(**over) -> Pr:
    rec = {
        "pr": 1,
        "meta": {...same...},
        "signals": {"ci": "passing", "mergeable": True, "has_tests": True,
                    "checked_at": NOW, "against_head_sha": HEAD},
        "reviews": {"greptile": {"kind": "review", "score": 5, "reviewed_sha": HEAD,
                                 "findings": [], "checks": [], "extra": {}},
                    "checked_at": NOW, "against_head_sha": HEAD},
        "drift": {...same...},
    }
```
Grep the file for `"greptile": ` inside `signals=` overrides and convert each to a `reviews=` override of the same meaning (a helper `_reviews(score=5, sha=HEAD, **extra_entries)` keeps it short). Do the same in `test_derived_disposition.py`. Add tests:
```python
class TestMultiReviewerClean:
    def test_every_active_reviewer_gates(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,coderabbit,superagent")
        review_policy.reset()
        rec = _pr(reviews={"greptile": {"kind": "review", "score": 5, "reviewed_sha": HEAD, "findings": [], "checks": []},
                           "superagent": {"kind": "scanner", "reviewed_sha": HEAD, "checks": [],
                                          "findings": [{"severity": "P2", "resolved": False, "outdated": False}]},
                           "checked_at": NOW, "against_head_sha": HEAD})
        ok, reasons = gates.pr_clean(rec)
        assert not ok
        assert "awaiting coderabbit review" in reasons and "superagent: 1 open P2 finding" in reasons

    def test_missing_reviews_section_blocks_and_does_not_demote(self):
        rec = _pr(reviews=None, analysis=_merge_analysis(), security=_green())
        rec.rec.pop("reviews")
        ok, reasons = gates.pr_clean(rec)
        assert "reviews stale or missing" in reasons
        assert gates.merge_demotion(rec) is None

    def test_bar_asks_from_blockers(self):
        rec = _pr(reviews={"greptile": {"kind": "review", "score": 3, "reviewed_sha": HEAD, "findings": [], "checks": []},
                           "checked_at": NOW, "against_head_sha": HEAD})
        _, reasons = gates.pr_clean(rec)
        asks = gates.bar_asks(reasons, rec)
        assert any("5/5" in a for a in asks)

    def test_fix_not_huntable_under_scanner_block(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,superagent")
        review_policy.reset()
        (profile fixture with fixable_gates=("review",) — copy the idiom used at test_gates.py:2181)
        rec = _pr(reviews={... greptile score 3 at HEAD ..., "superagent": {... open P1 ...}})
        ok, why = gates.fix_huntable(rec, "fix")
        assert not ok and "superagent" in why
```
- [ ] **Step 2: Implement**
`pr_clean`:
```python
    if not is_current(pr, "signals"):
        reasons.append("signals stale or missing")
    else:
        if pr.ci != "passing":
            reasons.append(f"ci {pr.ci}")
        if not pr.mergeable:
            reasons.append("merge conflicts")
    if review_policy.active_reviewers():
        if not is_current(pr, "reviews"):
            reasons.append("reviews stale or missing")
        else:
            for b in (review_policy.clean_blockers(pr, reviewers.REVIEW)
                      + review_policy.clean_blockers(pr, reviewers.SCANNER)):
                if b.bar.reason:
                    reasons.append(b.bar.reason)
```
`bar_asks(reasons, pr=None)`: build `by_reason = {b.bar.reason: b.bar.ask for kind in KINDS for b in review_policy.clean_blockers(pr, kind)} if pr is not None else {}`; for each `r`: `if r in by_reason: (append ask if not None; continue)`; then the existing ci/conflict/drift/draft/secret-leak branches. `merge_demotion` calls `bar_asks(bar, pr)`. `_STALE_BAR_REASONS` adds `"reviews stale or missing"`.
`fix_huntable`:
```python
    if not is_current(pr, "signals") or (review_policy.active_reviewers() and not is_current(pr, "reviews")):
        return False, "signals or reviews stale or missing, so the review bar is unknowable"
    review_blockers = review_policy.clean_blockers(pr, reviewers.REVIEW)
    scanner_blockers = review_policy.clean_blockers(pr, reviewers.SCANNER)
    if action == "fix":
        if pr.ci != "passing": ...
        if pr.mergeable is not True: ...
        if scanner_blockers:
            return False, (f"{scanner_blockers[0].bar.reason} — a security finding is a "
                           "human's call, not a fix target")
        fixable = profile.active().autofix.fixable_gates
        failing = [b for b in review_blockers if b.bar.status == reviewers.FAIL]
        review_fixable = "review" in fixable and bool(failing)
        ci_fixable = "ci" in fixable and pr.ci == "failing"
        if not (review_fixable or ci_fixable):
            if review_blockers and not failing:
                return False, ("the review is stale or pending — a re-review, not a fix, is "
                               "what moves it")
            return False, "no gate a fix could clear is failing"
    elif review_blockers or scanner_blockers:
        return False, (review_blockers + scanner_blockers)[0].bar.reason or "review bar not met"
    return fix_eligibility(pr, action, changed_paths)
```
Update the `fix_huntable` docstring to speak of "every active reviewer's bar". Import `from pipeline import reviewers`.
- [ ] **Step 3: Run** `uv run pytest pipeline/tests/test_gates.py pipeline/tests/test_derived_disposition.py -q` → PASS. **Step 4: Commit** `"Gate pr_clean on every active reviewer and scanner"`.

---

### Task 8: Feed fetch (`review_fetch`) + ingest + live_prs + reviewers registry recompute

**Files:**
- Modify: `pipeline/review_fetch.py` (fetch), `pipeline/live_prs.py` (query + CI via contexts + `updated_at` + `check_runs`), `pipeline/ingest.py` (`_build_signals` loses Greptile params, `_stage_pr`/`upsert_pr` gain `reviews_override`, `_upsert_all` incremental feed, `refresh_prs`, `gh_ci_status`, `_ci_from_github_data` → `ci_signal`, CLI flags, main() registry recompute; delete backfill), `pipeline/gh.py` (`check_runs` → `ci_signal.from_rest_check_runs`), `pipeline/reingest.py` + `pipeline/triage_cluster.py` (prose only), delete `pipeline/greptile.py` + `pipeline/tests/test_greptile.py`
- Test: `pipeline/tests/test_review_fetch.py` (new), `pipeline/tests/test_ingest.py`, `pipeline/tests/test_refresh_prs.py`, `pipeline/tests/test_live_prs.py` (if present)

**Interfaces:**
- `review_fetch.fetch_feeds(numbers: list[int], *, heads: dict[int, str | None] | None = None) -> dict[int, PrFeed]` — chunks of `CHUNK_SIZE = 10`, one `gh.gh_graphql` per chunk; `review_fetch.feed_query(numbers) -> str`; `review_fetch.feed_from_node(n, node) -> PrFeed`.
- `live_prs.fetch` facts gain `"updated_at": str | None` and `"check_runs": list[dict]`, `"statuses": list[dict]`; `"ci"` computed by `ci_signal.verdict(check_runs, statuses)`.
- `ingest.needs_conversation(existing: Pr | None, head_sha: str | None, live_updated_at: str | None) -> bool`; `ingest.stage_reviews(existing, feed, head_sha) -> dict` returns `{**parse_all(...), "pr_updated_at": feed.updated_at}`; `_stage_pr(..., reviews_override: dict | None = None)`; `upsert_pr(..., reviews_override=None)`; `refresh_prs` uses `fetch_feeds([n])`; `main()` saves `store.save_reviewers(reviewers.seen_summary(corpus))` after the upsert sweep (corpus = the staged PRs plus remaining `existing_prs` values).

- [ ] **Step 1: Tests** — `test_review_fetch.py`:
```python
from pipeline import review_fetch

NODE = {"number": 11858, "headRefOid": "cb7342d3", "updatedAt": "2026-08-21T15:21:03Z",
        "reviews": {"nodes": [{"databaseId": 1, "author": {"login": "superagent-security", "__typename": "Bot"},
                               "state": "COMMENTED", "commit": {"oid": "cb7342d3"}, "body": "Superagent found 2 security concern(s).",
                               "submittedAt": "2026-08-21T15:21:03Z", "url": "r"}]},
        "reviewThreads": {"nodes": [{"isResolved": False, "isOutdated": False, "comments": {"nodes": [{
            "databaseId": 2, "author": {"login": "superagent-security"}, "body": "**P1:** x", "path": "m.ts",
            "line": 28, "originalLine": 28, "commit": {"oid": "cb7342d3"}, "originalCommit": {"oid": "cb7342d3"},
            "createdAt": "2026-08-21T15:21:03Z", "updatedAt": "2026-08-21T15:21:03Z", "url": "t"}]}}]},
        "comments": {"nodes": [{"databaseId": 3, "author": {"login": "greptile-apps", "__typename": "Bot"},
                                "body": "Confidence Score: 4/5", "createdAt": "2026-08-21T00:00:00Z",
                                "updatedAt": "2026-08-21T00:00:00Z", "url": "c"}]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "Greptile Review", "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "Confidence 4/5 — below your required 5/5", "summary": "s", "detailsUrl": "d", "url": "u",
             "checkSuite": {"app": {"slug": "greptile-apps"}}}]}}}}]}}

def test_feed_from_node():
    f = review_fetch.feed_from_node(11858, NODE)
    assert f.head_sha == "cb7342d3" and f.updated_at == "2026-08-21T15:21:03Z" and f.conversation
    assert f.reviews[0]["login"] == "superagent-security" and f.reviews[0]["commit"] == "cb7342d3"
    assert f.threads[0]["resolved"] is False and f.threads[0]["original_commit"] == "cb7342d3"
    assert f.comments[0]["body"] == "Confidence Score: 4/5"
    assert f.check_runs[0]["app"] == "greptile-apps" and f.check_runs[0]["conclusion"] == "failure"

def test_fetch_feeds_chunks_and_aliases(monkeypatch):
    calls = []
    def fake(query, **kw):
        calls.append(query)
        return {"data": {"repository": {f"p{i}": dict(NODE, number=n) for i, n in enumerate(range(1, 12)) if f"number: {n})" in query}}}
    monkeypatch.setattr(review_fetch, "gh_graphql", fake)
    monkeypatch.setattr(review_fetch.settings, "repo_owner", lambda: "o")
    monkeypatch.setattr(review_fetch.settings, "repo_name", lambda: "r")
    out = review_fetch.fetch_feeds(list(range(1, 12)))
    assert len(calls) == 2 and set(out) == set(range(1, 12))
```
`test_ingest.py` / `test_refresh_prs.py`: locate every test that passes `greptile_override=`/`greptile_reviewed_sha=` and rewrite it to assert on `reviews_override` staging (e.g. `_stage_pr(..., reviews_override={"greptile": {...}, "pr_updated_at": "x"})` → `pr.rec["reviews"]["greptile"]["score"] == 4` and `pr.rec["reviews"]["against_head_sha"] == head`). Add:
```python
def test_needs_conversation():
    from pipeline import ingest
    assert ingest.needs_conversation(None, "h", "t1") is True
    pr = Pr(None, {"pr": 1, "meta": {"head_sha": "h"}, "reviews": {"pr_updated_at": "t1", "against_head_sha": "h"}})
    assert ingest.needs_conversation(pr, "h", "t1") is False
    assert ingest.needs_conversation(pr, "h2", "t1") is True
    assert ingest.needs_conversation(pr, "h", "t2") is True
```
and a `refresh_prs` test that monkeypatches `review_fetch.fetch_feeds` to return one feed with a Greptile comment + check run and asserts the stored `reviews.greptile.score` and `signals.ci` (the Greptile failing check must NOT make CI failing).
- [ ] **Step 2: Implement `review_fetch.fetch_feeds`**:
```python
CHUNK_SIZE = 10

_FIELDS = (
    "number headRefOid updatedAt "
    "reviews(last: 40) { nodes { databaseId author { login __typename } state commit { oid } body submittedAt url } } "
    "reviewThreads(last: 100) { nodes { isResolved isOutdated comments(first: 1) { nodes { databaseId "
    "author { login } body path line originalLine commit { oid } originalCommit { oid } createdAt updatedAt url } } } } "
    "comments(last: 40) { nodes { databaseId author { login __typename } body createdAt updatedAt url } } "
    "commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename "
    "... on CheckRun { name status conclusion title summary detailsUrl url checkSuite { app { slug } } } "
    "... on StatusContext { context state } } } } } } }")


def feed_query(numbers: list[int]) -> str:
    aliases = " ".join(f"p{i}: pullRequest(number: {int(n)}) {{ {_FIELDS} }}" for i, n in enumerate(numbers))
    return (f'query {{ repository(owner: "{settings.repo_owner()}", name: "{settings.repo_name()}") '
            f"{{ {aliases} }} }}")


def feed_from_node(n: int, node: dict) -> PrFeed:
    def login(x: dict | None) -> str | None:
        return ((x or {}).get("author") or {}).get("login")
    reviews = [{"id": r.get("databaseId"), "login": login(r), "state": r.get("state"),
                "commit": (r.get("commit") or {}).get("oid"), "body": r.get("body"),
                "at": r.get("submittedAt"), "url": r.get("url")}
               for r in ((node.get("reviews") or {}).get("nodes") or []) if isinstance(r, dict)]
    threads = []
    for t in ((node.get("reviewThreads") or {}).get("nodes") or []):
        first = (((t or {}).get("comments") or {}).get("nodes") or [None])[0]
        if not isinstance(first, dict):
            continue
        threads.append({"id": first.get("databaseId"), "login": login(first), "path": first.get("path"),
                        "line": first.get("line") or first.get("originalLine"), "body": first.get("body"),
                        "commit": (first.get("commit") or {}).get("oid"),
                        "original_commit": (first.get("originalCommit") or {}).get("oid"),
                        "resolved": bool(t.get("isResolved")), "outdated": bool(t.get("isOutdated")),
                        "at": first.get("createdAt"), "url": first.get("url")})
    comments = [{"id": c.get("databaseId"), "login": login(c), "body": c.get("body"),
                 "at": c.get("createdAt"), "updated_at": c.get("updatedAt"), "url": c.get("url")}
                for c in ((node.get("comments") or {}).get("nodes") or []) if isinstance(c, dict)]
    commits = ((node.get("commits") or {}).get("nodes")) or [{}]
    rollup = ((commits[0] or {}).get("commit") or {}).get("statusCheckRollup") or {}
    runs, statuses = ci_signal.from_graphql_contexts((rollup.get("contexts") or {}).get("nodes") or [])
    return PrFeed(pr=int(n), head_sha=node.get("headRefOid"), updated_at=node.get("updatedAt"),
                  reviews=reviews, threads=threads, comments=comments, check_runs=runs,
                  statuses=statuses, conversation=True)


def fetch_feeds(numbers: list[int]) -> dict[int, PrFeed]:
    """Feeds for `numbers`, keyed by PR; a PR missing from the result failed to
    fetch (transient) and keeps its stored entry."""
    out: dict[int, PrFeed] = {}
    for i in range(0, len(numbers), CHUNK_SIZE):
        chunk = numbers[i:i + CHUNK_SIZE]
        payload = gh_graphql(feed_query(chunk), timeout=120)
        if payload is None:
            _log.warning("review feed fetch failed for PRs %s-%s", chunk[0], chunk[-1])
            continue
        repo = (payload.get("data") or {}).get("repository") or {}
        for j, n in enumerate(chunk):
            node = repo.get(f"p{j}")
            if isinstance(node, dict):
                out[int(n)] = feed_from_node(int(n), node)
    return out
```
(imports: `logging`, `from pipeline import ci_signal, settings`, `from pipeline.gh import gh_graphql`).
- [ ] **Step 3: `live_prs.py`** — `_query` fields add `updatedAt` and replace the rollup fragment with `commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes { __typename ... on CheckRun { name status conclusion title summary detailsUrl url checkSuite { app { slug } } } ... on StatusContext { context state } } } } } } }`; in `fetch`, `runs, statuses = ci_signal.from_graphql_contexts(...)`; facts gain `"updated_at": node.get("updatedAt")`, `"check_runs": runs`, `"statuses": statuses`, and `"ci": ci_signal.verdict(runs, statuses)`. Drop `_CI_NORM`.
- [ ] **Step 4: `ingest.py`** —
  - delete `_CI_FAIL_CONCLUSIONS/_CI_OK_CONCLUSIONS/_ci_from_github_data`; `gh_ci_status(sha)` → `ci_signal.verdict(ci_signal.from_rest_check_runs(check_runs), statuses)`.
  - `_build_signals`: remove the two Greptile params and their lines.
  - add:
    ```python
    def needs_conversation(existing: Pr | None, head_sha: str | None, live_updated_at: str | None) -> bool:
        """Whether the PR's conversation must be re-read: no stored reviews, a moved
        head, or GitHub's updatedAt past the one the stored section was read at."""
        if existing is None or existing.reviews is None:
            return True
        stored = existing.reviews
        if stored.get("against_head_sha") != head_sha:
            return True
        return stored.get("pr_updated_at") != live_updated_at

    def stage_reviews(existing: Pr | None, feed: PrFeed, head_sha: str | None) -> dict:
        previous = existing.reviews if existing is not None else None
        entries = reviewers.parse_all(feed, head_sha, previous)
        return {**entries, "pr_updated_at": feed.updated_at}
    ```
  - `_stage_pr(..., reviews_override: dict | None = None)` → `pr.stage_facts(meta, signals=sig, drift=drift, issues=issues, reviews=reviews_override)`; `upsert_pr` threads it.
  - `_upsert_all`: before the loop, compute `conv_numbers = [n for gh_pr in prs if needs_conversation(existing_prs.get(n), head, (facts.get(n) or {}).get("updated_at"))]`; `feeds = review_fetch.fetch_feeds(conv_numbers)`; in the loop build `feed = feeds.get(n)` or, when `current` (live facts at this head) exists, `PrFeed(pr=n, head_sha=head_sha, updated_at=current.get("updated_at"), check_runs=current.get("check_runs") or [], statuses=current.get("statuses") or [], conversation=False)`; `reviews_override = stage_reviews(existing, feed, head_sha) if feed is not None else None`. Pass it to `_stage_pr`.
  - `refresh_prs`: `feed = review_fetch.fetch_feeds([n]).get(n)`; `ci = ci_signal.verdict(feed.check_runs, feed.statuses) if feed else gh_ci_status(head_sha)`; `reviews_override = stage_reviews(before, feed, head_sha) if feed else None`.
  - `main()`: remove `--backfill-greptile-data` + both backfill blocks; after the `with store.batch():` block add
    ```python
    corpus = list(existing_prs.values()) + [p for p in staged_prs if p.number not in existing_prs]
    ```
    — simplest: have `_upsert_all` return `"staged": staged` in `_UpsertStats` and recompute `store.save_reviewers(reviewers.seen_summary(list(existing_prs.values()) + counts["staged"]))`. (Existing PRs re-staged are the same `Pr` objects, so duplicates are harmless for a max.) Update the `--prs` help text ("live CI and reviewer signals").
  - `_targeted_ingest --new`: uses `_upsert_all`, inherits feeds.
- [ ] **Step 5: `gh.check_runs`** returns `ci_signal.from_rest_check_runs(...)` items (keys `app,name,status,conclusion,title,summary,url`), still deduped by `(name, conclusion)`. Update its docstring and `pipeline/tests/test_gh*.py` if any.
- [ ] **Step 6: Delete `pipeline/greptile.py`, `pipeline/tests/test_greptile.py`**; fix `reingest.py`/`triage_cluster.py` prose ("CI/reviewer signals"). Grep the tree for `from pipeline import greptile` / `pipeline.greptile` — remaining importers are migrated in Tasks 9–11 (`greptile_read_driver`, `service`, `review_refresh`, `app`, `fix_worker`, `pr_history`).
- [ ] **Step 7: Run** `uv run pytest pipeline/tests/test_review_fetch.py pipeline/tests/test_ingest.py pipeline/tests/test_refresh_prs.py pipeline/tests/test_live_prs.py -q` → PASS. **Step 8: Commit** `"Ingest every reviewer's feed incrementally and recompute the reviewers registry"`.

---

### Task 9: Greptile semantic read from the stored entry; analyze/security/chat prompts; autofix goal

**Files:** `pipeline/greptile_read_driver.py`, `pipeline/analyze_driver.py`, `pipeline/security_driver.py`, `pipeline/security_review.py`, `pipeline/workflows/security.js`, `pipeline/workflows/README.md`, `prospector_app/agent/context.md` (if `{retrigger_mention}` text needs wording), `prospector_app/backend/chat.py`, `prospector_app/backend/fix_worker.py`, `pipeline/author_fix.py` (prose only), `prospector_app/backend/deep_search.py`, `prospector_app/backend/training.py`; tests `test_greptile_read_driver.py`, `test_analyze_driver.py`, `test_merge_bar.py`, `test_security_driver.py`, `test_chat_context.py`, `test_chat_config.py`, `test_fix_worker.py`, `test_autohunt.py`, `test_deep_search.py`.

**Interfaces:** `reviewers.digests(pr: Pr) -> dict[str, dict]` (add to `reviewers.py`: `{rid: digest(r, entry, review_policy.bar(pr, r), pr.head_sha)}` for every reviewer with an entry or active — import `review_policy` lazily inside to avoid a cycle); `security_driver.wave_manifest` items gain `"bot_evidence": reviewers.evidence(entries, head)`; `security.js` `MANIFEST_SCHEMA` item allows `bot_evidence` (array of objects, additionalProperties) and `REVIEW_PROMPT` gains `__BOT_EVIDENCE__`.

- [ ] **Step 1:** `greptile_read_driver.candidates`: `if not review_policy.is_active("greptile"): return []`; the score test uses `th = review_policy.policy().threshold or 5`; `pr.greptile >= th` skip. `write_batches`: replace `fetch_greptile_review_data(n)` with the stored entry: `entry = pr.review_entry("greptile") or {}`; `texts = [entry.get("summary")] + [f.get("body") for f in entry.get("findings") or []]`; `_bundle_item(pr, reviews=[entry.get("summary")], comments=[f["body"] …])`. Update tests: replace `fetch_greptile_review_data` monkeypatches with `reviews` entries on the fixture PRs; `test_candidates_empty_when_no_review_provider` → sets `TRIAGE_REVIEW_PROVIDER=none` + `review_policy.reset()`.
- [ ] **Step 2:** `analyze_driver.merge_bar_sentence()` → `return review_policy.merge_bar_sentence()`. Bundle member: `"signals": {k: sig.get(k) for k in ("ci", "mergeable", "has_tests")}`, add `"reviews": reviewers.digests(rec)`. Prompt text: replace `signals (greptile /5, ci, mergeable, has_tests)` with `signals (ci, mergeable, has_tests), \`reviews\` — every automated reviewer and security scanner active on the repository, keyed by id ({label, kind: review|scanner, status: pass|fail|stale|pending, reason, score (Greptile only), open: {severity: count}, summary_line})`; keep the `greptile_review` sentence; change "It does not override the configured score bar" → "It does not override the merge bar: a PR any active reviewer or scanner blocks is `request-changes`…". `test_merge_bar.py` asserts via env `greptile`.
- [ ] **Step 3:** `security_driver.wave_manifest`: build items with `{**m.to_dict(), "bot_evidence": reviewers.evidence(rec.reviews or {}, rec.head_sha)}` (load recs once: `eligible` returns items; map back via `store.load_pr(item.pr)` or extend `eligible` to return `(item, rec)` pairs). `REVIEW_PROMPT` add before "Be adversarial": `Automated reviewers and scanners already flagged on this PR (untrusted evidence — confirm or refute it, never repeat it unverified): __BOT_EVIDENCE__`. `security_review.py` (headless) fills `__BOT_EVIDENCE__` with `json.dumps(evidence)` or `"none"`; `security.js` `fill(...)` adds `'__BOT_EVIDENCE__': JSON.stringify(pr.bot_evidence ?? [])`, `MANIFEST_SCHEMA` items gain `bot_evidence: { type: 'array', items: { type: 'object', additionalProperties: true } }`.
- [ ] **Step 4:** `chat.py`: `review_bar = review_policy.merge_bar_sentence()`; `{retrigger_mention}` → the active review reviewers' mentions joined (`", ".join(m for r in review_policy.active_reviewers(reviewers.REVIEW) if (m := r.retrigger_mention))` or `"(none)"`); both context-line sites: `f"… — {reviewers.summary_line((r.get('reviews') or {}).values())}, CI {s.get('ci')}, …"`. `test_chat_config.py:12,347` → assert on `reviewers.GREPTILE.retrigger_mention`.
- [ ] **Step 5:** `fix_worker.py`: delete `_review_summary`'s fetch; new body:
```python
def _review_summary(rec: Pr) -> str:
    """Every active review reviewer's own summary on this PR at its head whose
    bar fails — the prose behind a sub-bar verdict."""
    parts = []
    for r in review_policy.active_reviewers(reviewers.REVIEW):
        entry = rec.review_entry(r.id)
        if entry and review_policy.bar(rec, r).status == reviewers.FAIL and entry.get("summary"):
            parts.append(f"## {r.label}\n{entry['summary']}")
    return "\n\n".join(parts)[:REVIEW_SUMMARY_CHARS]
```
`_fix_goal`: `findings = [f for r in review_policy.active_reviewers(reviewers.REVIEW) if review_policy.bar(rec, r).status == reviewers.FAIL for f in reviewers.findings_for_fix(r, rec.review_entry(r.id), rec.head_sha, rec.greptile_review if freshness.is_current(rec, "greptile_review") else None)]`; `summary = _review_summary(rec) if ("review" in fixable or guidance) else ""`. `_hunt_key`: `sevs = {reviewers.severity(r, rec.review_entry(r.id), rec.greptile_review if freshness.is_current(rec,"greptile_review") else None) for r in review_policy.active_reviewers(reviewers.REVIEW)}`; `th = review_policy.policy().threshold or 5`; `tier = 1 if sevs and sevs <= {"nits", "clean", None} and "nits" in sevs else 2 if rec.greptile == th - 1 else 3`. `_retrigger_review(n)`: loop `for r in review_policy.active_reviewers(reviewers.REVIEW): if r.retrigger_mention: … executor.retrigger_review(n, r.id, token=…, dry_run=…)`; `review_refresh.capture(n, r.id)` / `schedule(n, r.id, baseline)` (Task 10 defines these). Update `test_fix_worker.py` 576–849 + `test_autohunt.py` to stage `reviews` entries instead of monkeypatching Greptile fetches.
- [ ] **Step 6:** `deep_search.py:88` → `"reviews": {rid: d["summary_line"] for rid, d in reviewers.digests(rec).items()}`; `training.py:62` → `"greptile": signals.get("greptile")` stays (row signals keep the Greptile projection — Task 10); add `"reviews": {rid: d["status"] for rid, d in (row.get("reviews") or {}).items()}` if `_features` has the row — read the function first.
- [ ] **Step 7:** `workflows/README.md` GREPTILE READ section: "reads the Greptile entry the ingest stored". **Run** all named tests → PASS; `uv run ruff check .`. **Commit** `"Feed every reviewer's findings to the analyze, security, chat and autofix paths"`.

---

### Task 10: App backend — caps, rows, checks, filters, search, routes, executor, bulk, refresh, live freshness, history

**Files:** `prospector_app/backend/{caps,service,pr_checks,filters,pr_search,app,executor,bulk,review_refresh,freshness_live,pr_history}.py`; tests `test_caps_review.py` → rename `test_caps_reviewers.py`, `test_pr_checks.py`, `test_filters.py`, `test_pr_search.py`, `test_bulk.py`, `test_review_refresh.py`, `test_freshness_live.py`, `test_pr_history.py`, `test_service_store.py`, `test_query_row_cache.py`, `test_tables.py`, `test_app_query.py`, `test_home_counts.py`, `test_executor_bot_identity.py`, `test_executor_stale_evidence.py`, `test_responses.py` (unchanged), `test_comment_then_close.py`, `test_live_sweep.py`.

**Interfaces:**
- `caps.capabilities()["reviewers"] = review_policy.describe()`; key `"review"` removed.
- `service.pr_row`: `row["reviews"] = reviewers.digests(rec)`; `_signal_summary(sig, rec)` emits `greptile` = `rec.greptile`, `greptile_stale` = `rec.greptile_stale`, `greptile_severity` (current only); `_SORT_KEYS["greptile"]` unchanged; detail: `row["reviews_detail"] = {rid: {"entry": rec.review_entry(rid), "digest": d} for rid, d in row["reviews"].items()}`; `_greptile_and_ci` → `_ci_checks(head)` only (the reviews come from the store); `row["ci_checks"]` keeps shape `{name, conclusion, status}` (now with `app`, `title`).
- `pr_checks.CHECK_KEYS` gains `"scans"`; `review` row aggregates `review_policy.active_reviewers(REVIEW)` bars (`pass` all pass; `fail` any fail; `warn` any stale/pending; `na` none active); name `"Code review"`; detail `" · ".join(summary_line per reviewer)`; `scans` row same over scanners, name `"Security scans"`.
- `filters.py`: `reviewer_status: {rid: status | [status…]}` matched against `row["reviews"][rid]["status"]` (missing reviewer matches only `"na"`/`"pending"`? — match `"pending"` for an active reviewer with no digest is impossible since digests include active reviewers; missing → no match).
- `pr_search`: `_REVIEWER_FIELDS`; docs list active reviewer ids and statuses; `coerce` drops Greptile keys unless `review_policy.is_active("greptile")` and `reviewer_status` keys for inactive ids.
- Routes: `GET /api/prs/{n}/reviews` → `{"reviews": service.reviews_detail(n)}`; `POST /api/reviews/{reviewer}/retrigger/pr/{n}?dry_run=` (404 on unknown id); delete `/api/prs/{n}/greptile` and `/api/greptile/retrigger/pr/{n}`.
- `executor.retrigger_review(n: int, reviewer_id: str, *, token: str | None, dry_run: bool) -> dict` — action `"REVIEW_RETRIGGER"`, `"reviewer": reviewer_id` in base; `"skipped"` when the reviewer has no mention or is unknown; activity kind `"review_retrigger"`.
- `bulk.py`: action `REVIEW_RETRIGGER` with `reviewer` from the request body (default: first active review reviewer with a mention).
- `review_refresh.capture(n, reviewer_id) -> Baseline(version, captured_at)` where version = `reviewers.version(r, parsed entry from review_fetch.fetch_feeds([n]))`; `wait_and_refresh(n, reviewer_id, baseline, …)`; `schedule(n, reviewer_id, baseline)`; generation keyed by `(n, reviewer_id)`.
- `freshness_live`: for each active review reviewer with an entry whose `reviewed_sha` is behind the live head → `{"kind": "review", "reviewer": r.id, "label": r.label, "was", "now", "message": f"{r.label} reviewed an earlier commit — its verdict may not reflect the current diff"}`. It needs `rec.reviews`: read how `check()` gets its record (`meta`/`sig` come from the snapshot rows) and pass `rec.reviews` the same way.
- `pr_history`: `kind: "bot_review"` with `reviewer: str | None` and `score: int | None` (Greptile `parse_confidence_score`); `HistoryItem` fields renamed (`greptile_score` → `score`, add `reviewer`).

- [ ] **Step 1:** Write/adjust tests first per file (each asserts the interface above; e.g. `test_pr_checks.py`: a PR with Greptile 5 at head + Superagent P1 open under `TRIAGE_REVIEW_PROVIDER=greptile,superagent` yields `review: pass`, `scans: fail` with detail containing `"Superagent 1 P1"`; `test_filters.py`: `{"reviewer_status": {"superagent": "fail"}}` matches a row whose `reviews.superagent.status == "fail"`; `test_bulk.py`: `REVIEW_RETRIGGER` with `reviewer: "greptile"` calls `executor.retrigger_review(n, "greptile", …)`; `test_review_refresh.py`: `_is_new` on version tokens; `test_freshness_live.py`: the `review` divergence item; `test_pr_history.py`: `bot_review` items).
- [ ] **Step 2:** Implement each file per the interfaces. `service.reviews_detail(n)`:
```python
def reviews_detail(n: int) -> dict[str, dict]:
    rec = data.prs().get(int(n))
    if rec is None:
        return {}
    return {rid: {"entry": rec.review_entry(rid), "digest": d}
            for rid, d in reviewers.digests(rec).items()}
```
- [ ] **Step 3:** `uv run pytest prospector_app/backend/tests -q` → PASS; `uv run pyright …` → 0; `uv run ruff check .` → 0. **Commit** `"App backend reads every reviewer: capabilities, rows, checks, filters, retrigger"`.

---

### Task 11: Migration script

**Files:** Create `pipeline/migrate_reviews.py`, `pipeline/tests/test_migrate_reviews.py`. Read `pipeline/store_edit.py` first and reuse its snapshot + runs-ledger helpers (`--apply` flag; dry-run default).

**Interfaces:** `migrate_reviews.plan(store) -> list[int]` (PRs with `signals.greptile`/`greptile_reviewed_sha` and no `reviews.greptile`); `migrate_reviews.apply(store, numbers) -> int`; CLI `uv run python pipeline/migrate_reviews.py [--apply]`.

- [ ] **Step 1: Test** — seed a tmp store with a PR `{signals: {greptile: 4, greptile_reviewed_sha: "h", checked_at: T, against_head_sha: "h"}}`; `plan` lists it; `apply` writes `reviews.greptile == {"kind": "review", "score": 4, "reviewed_sha": "h", "observed_at": T, "findings": [], "summary": None, "checks": [], "extra": {}}` stamped `against_head_sha == "h"`, removes the two signal keys, and appends a run `{"phase": "migrate-reviews", ...}`; a second `plan` is empty.
- [ ] **Step 2: Implement**; **Step 3:** run + commit `"Lift legacy Greptile signals into the reviews section"`.

---

### Task 12: Frontend

**Files:** `api.ts`, `ExecContext.tsx`, `useColumnPrefs.ts`, `glossary.ts`, `components/explorer/{checkDefs.ts,columns.tsx,ReviewCell.tsx,ScansCell.tsx,ColumnFilterPopout.tsx,prFilterParts.ts,lanes.ts,BulkActionBar.tsx,BulkConfirmDialog.tsx,ExplorerSearchBar.tsx,ColumnToggles.tsx}`, `components/{FactFreshness,PRHistory}.tsx`, `views/{PRDetail.tsx,homeCards.ts,PRExplorer.tsx}`, `views/homeCards.test.ts`, `components/explorer/rowReuse.test.ts`; delete `GreptileCell.tsx`.

**Interfaces (types in `api.ts`):**
```ts
export type ReviewerKind = "review" | "scanner";
export type BarStatus = "pass" | "fail" | "stale" | "pending" | "na";
export interface ReviewerCap { id: string; label: string; kind: ReviewerKind; active: boolean; retrigger: boolean; score_max: number | null; threshold: number | null; bar_label: string }
export interface ReviewDigest { id: string; label: string; kind: ReviewerKind; status: BarStatus; reason: string | null; score: number | null; score_max: number | null; reviewed_sha: string | null; stale: boolean | null; open: Record<string, number>; observed_at: string | null; checks: { name: string | null; conclusion: string | null; status: string | null; title: string | null }[]; extra: Record<string, unknown>; summary_line: string }
export interface ReviewFinding { path: string | null; line: number | null; severity: string | null; title: string | null; body: string; resolved: boolean; outdated: boolean; commit: string | null; url: string | null }
export interface ReviewEntry { kind: ReviewerKind; reviewed_sha: string | null; observed_at: string | null; score: number | null; findings: ReviewFinding[]; summary: string | null; checks: ReviewDigest["checks"]; extra: Record<string, unknown> }
FilterSpec.reviewer_status?: Record<string, BarStatus | BarStatus[]>;
PRRow.reviews?: Record<string, ReviewDigest> | null;
PRDetail.reviews_detail?: Record<string, { entry: ReviewEntry | null; digest: ReviewDigest }> | null;
api.capabilities(): { …; reviewers: ReviewerCap[] }  (drop `review`)
api.prReviews(n) → GET /api/prs/{n}/reviews
api.retriggerReview(n, reviewer, dryRun) → POST /api/reviews/{reviewer}/retrigger/pr/{n}
PRHistoryItem.kind adds "bot_review"; fields `reviewer?: string | null; score?: number | null` (drop greptile_score)
BulkAction "REVIEW_RETRIGGER" (drop GREPTILE_RETRIGGER); bulk request carries `reviewer`.
```
ExecContext: `reviewers: ReviewerCap[]` (default `[]`), helper `activeReviewers(kind)`; `review` removed; toast label `REVIEW_RETRIGGER: "Review re-trigger"`.

- [ ] **Step 1:** `checkDefs.ts`: `review` label "Code review"; add `{ key: "scans", label: "Security scans" }` after `ci`. `homeCards.test.ts` `representable` list: replace the three greptile keys with `"reviewer_status"` **plus keep** `"greptile"`, `"greptile_stale"`, `"greptile_severity"` (still valid spec keys).
- [ ] **Step 2:** `columns.tsx`: replace the `greptile` column with
```tsx
{ key: "review", label: "Review", defaultOn: false, term: "col.review", capability: "review",
  cell: (r) => <ReviewCell n={r.number} reviews={r.reviews ?? null} greptileSeverity={r.signals?.greptile_severity ?? null} /> },
{ key: "scans", label: "Scans", defaultOn: false, term: "col.scans", capability: "scanner",
  cell: (r) => <ScansCell reviews={r.reviews ?? null} /> },
```
`ColumnDef.capability?: ReviewerKind`. `ReviewCell`: one chip per digest with `kind === "review"` (`{label} {score}/{max}` for scored, else `{label} {n} {worst severity}` or status), `⚠` when `stale`, 🐛/🧹 from `greptileSeverity` on the Greptile chip, InfoTip body = each digest's `summary_line` + `reason`, lazy `api.prReviews(n)` on hover for the Greptile summary excerpt (MAX 600). `ScansCell`: chips per `kind === "scanner"` digest: `Superagent ✓ | P1 ×2 | pending`, trust `extra.trust_score` in the hover. Delete `GreptileCell.tsx`.
- [ ] **Step 3:** `useColumnPrefs.ts` / `ColumnToggles.tsx`: gate `c.capability` on `reviewers.some(r => r.active && r.kind === c.capability)`; `read()` maps a stored `greptile` key to `review` once.
- [ ] **Step 4:** `ColumnFilterPopout.tsx`: `case "review"`: per active review reviewer a `<select>` of statuses bound to `spec.reviewer_status?.[id]`, plus the existing Greptile score/freshness/severity controls when Greptile is active; `case "scans"`: per active scanner a status select. `FILTERABLE_COLS` swap `greptile` → `review`, add `scans`; `isColFilterActive` for both. `prFilterParts.ts`: chips `"{label}: {status}"` per `reviewer_status` entry (label from `useExec` is unavailable in a pure fn — pass `reviewers` in via a parameter or fall back to the id; keep existing Greptile chips). `ExplorerSearchBar` hint lists active reviewer ids (`"greptile < 3, reviewer_status superagent fail"`).
- [ ] **Step 5:** `lanes.ts` `MERGE_READY_SPEC = { checks: ALL_CHECKS_PASS, safety: "GREEN" }`; stale lane keeps `greptile`/`greptile_stale` clauses. `homeCards.ts`: drop raw Greptile clauses from `base-update`; `verify-pending`/`security-pending` `checksPass(...)` add `"scans"`; `nitpicks` card stays.
- [ ] **Step 6:** `PRDetail.tsx`: `retriggerReview(reviewerId)`; `checksActions.review` renders one button per active review reviewer with `retrigger`; `checksBodies.review` = blocks per `reviews_detail` entry of kind review (status chip, stale banner, score/open counts, `ReactMarkdown` of `entry.summary`, findings list `path:line — severity — title` linking `url`); `checksBodies.scans` = blocks per scanner (check rows with conclusion, findings, `extra.trust_score/trust_verdict`, `extra.report_url`). `FactFreshness.LABEL` add `reviews: "Reviews"`. `PRHistory` maps `bot_review` → 🔍 "reviewed", body `score != null ? `confidence ${score}/5` : summary`, actor label via `it.reviewer`.
- [ ] **Step 7:** `BulkActionBar`/`BulkConfirmDialog`/`ExecContext`: `REVIEW_RETRIGGER` offered once per active review reviewer with `retrigger` (label `re-trigger ${label}`, option value carries the reviewer id); `glossary.ts`: `col.review`, `col.scans`, `bulk.REVIEW_RETRIGGER`, `review` (generic) entries; keep `greptile` term.
- [ ] **Step 8:** `cd prospector_app/frontend && pnpm run build && pnpm exec eslint <changed files> && node --test "src/**/*.test.ts"` → clean. **Commit** `"Review and Scans columns, per-reviewer PR page blocks and filters"`.

---

### Task 13: Docs, env, onboarding, final gates

**Files:** `CLAUDE.md` (section list adds `reviews`; trust-model/gates prose: "every active reviewer's bar"; fix-goal prose), `.env.example` (`TRIAGE_REVIEW_PROVIDER=auto|none|greptile,coderabbit,…`, `TRIAGE_REVIEWER_ACTIVE_DAYS`), the deployment doc (review bar line), `pipeline/workflows/README.md`, `prospector_app/backend/onboarding.py` (no change unless it validates the value — verify), spec status line.

- [ ] **Step 1:** Edit docs. **Step 2:** Run the full gate: `uv run pytest -q`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, `uv run ruff check .`, frontend build/lint. **Step 3:** Run the migration dry-run against the configured store and report the count (`uv run python pipeline/migrate_reviews.py`). **Step 4:** Commit `"Document the reviewer registry and the auto-detected review bar"`.

---

## Self-review notes

- Spec coverage: registry/adapters (T1–4), store (T5), policy/settings/ci (T6), gates (T7), fetch/ingest/registry (T8), semantic read + agents + autofix (T9), app backend (T10), migration (T11), frontend (T12), docs (T13). Spec's `review_status`/`scan_status` keys are unified into `reviewer_status` (one key, both kinds) — update the spec line in T13.
- `Pr.review_*` single-provider accessors are removed in T5; every later task's code uses `review_policy.bar/clean_blockers` or `reviewers.*` — grep `review_score|review_stale|review_severity|review_section|review_reviewed_sha|clean_blocker\(|review_policy.active\(` must be empty by the end of T10.
