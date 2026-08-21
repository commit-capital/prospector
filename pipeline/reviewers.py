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
_SUMMARY_SHA_RE = re.compile(r"[Ll]ast reviewed commit:[^\n]*?/commits?/([0-9a-f]{7,40})")


def parse_confidence_score(body: str | None) -> int | None:
    """Greptile's `Confidence Score: N/5` from any comment or review body."""
    m = _SCORE_RE.search(body or "")
    return int(m.group(1)) if m else None


def strip_html(s: str) -> str:
    """A bot's comment as displayable text: HTML comments dropped, tags
    unwrapped, entities and literal `\\uXXXX` decoded, blank-line runs
    collapsed."""
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


def _first_line(body: str | None) -> str | None:
    for line in (body or "").splitlines():
        if line.strip():
            return line.strip()[:160]
    return None


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


def open_findings(entry: dict | None) -> list[dict]:
    """Findings still standing: unresolved, not outdated by a later push."""
    if not entry:
        return []
    return [f for f in entry.get("findings") or []
            if isinstance(f, dict) and not f.get("resolved") and not f.get("outdated")]


def open_counts(entry: dict | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in open_findings(entry):
        sev = str(f.get("severity") or "unclassified")
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --- Greptile --------------------------------------------------------------

def _greptile_parse(feed: PrFeed, head_sha: str | None, previous: dict | None) -> dict | None:
    checks = _own_checks(GREPTILE, feed)
    if not feed.conversation:
        entry = _carry(previous, GREPTILE, checks)
    else:
        comments = _own(GREPTILE, feed.comments)
        reviews = _own(GREPTILE, feed.reviews)
        threads = _own(GREPTILE, feed.threads)
        scored = [c for c in comments if parse_confidence_score(c.get("body")) is not None]
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
        findings = [_finding(t, None, _first_line(t.get("body"))) for t in threads]
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
    mx = GREPTILE.score_max or 5
    th = threshold if threshold is not None else mx
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


# --- CodeRabbit ------------------------------------------------------------

_CR_ACTIONABLE_RE = re.compile(r"Actionable comments posted:\s*\*{0,2}(\d+)")
_CR_PREMERGE_RE = re.compile(r"Pre-merge checks\s*\|\s*✅\s*(\d+)\s*\|\s*❌\s*(\d+)")
_CR_SEVERITIES = (("critical", "critical"), ("major", "major"), ("minor", "minor"),
                  ("nitpick", "nitpick"))
_CR_BLOCKING = frozenset({"critical", "major"})
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _coderabbit_severity_of(body: str | None) -> str | None:
    head = (_first_line(body) or "").lower()
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
    blocking = [f for f in open_findings(entry) if f.get("severity") in _CR_BLOCKING]
    if blocking:
        worst = "critical" if any(f["severity"] == "critical" for f in blocking) else "major"
        return Bar(FAIL, f"coderabbit: {_plural(len(blocking), f'open {worst} finding')}",
                   f"CodeRabbit left {_plural(len(blocking), f'unresolved {worst} finding')} — "
                   "address or resolve them.")
    reviewed = entry.get("reviewed_sha")
    if reviewed and head_sha and reviewed != head_sha:
        return Bar(STALE, "coderabbit review stale",
                   "CodeRabbit reviewed an earlier commit — a re-review of the current head is needed.")
    return Bar(PASS, None, None)


def _coderabbit_severity(entry: dict | None) -> str | None:
    if entry is None:
        return None
    sevs = {f.get("severity") for f in open_findings(entry)}
    if sevs & _CR_BLOCKING:
        return "defects"
    if sevs:
        return "nits"
    return "clean" if entry.get("reviewed_sha") else None


def _coderabbit_findings_for_fix(entry: dict | None) -> list[dict]:
    return [{"headline": f.get("title") or (f.get("body") or "")[:120],
             "class": "substantive" if f.get("severity") in _CR_BLOCKING else "nitpick",
             "why": (f.get("body") or "")[:500], "path": f.get("path"), "line": f.get("line")}
            for f in open_findings(entry)]


# --- Superagent ------------------------------------------------------------

_SA_CONCERNS_RE = re.compile(r"Superagent found\s+(\d+)\s+security concern")
_SA_PRIORITY_RE = re.compile(r"\*\*(P[1-3]):\*\*\s*(.+)")
_SA_TRUST_RE = re.compile(r"Score:\s*(\d+)\s*/\s*100\s*·\s*Verdict:\s*(\w+)")
_SA_BLOCKING = frozenset({"P1", "P2"})
_SA_SCAN = "Superagent Security Scan"
_SA_TRUST = "Contributor trust"


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
        findings.append(_finding(t, m.group(1) if m else None,
                                 m.group(2).strip()[:200] if m else _first_line(t.get("body"))))
    trust = next((_SA_TRUST_RE.search(c.get("summary") or "") for c in checks
                  if c.get("name") == _SA_TRUST), None)
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
    blocking = [f for f in open_findings(entry) if f.get("severity") in _SA_BLOCKING]
    scan = _check_named(entry, _SA_SCAN)
    if blocking:
        worst = "P1" if any(f["severity"] == "P1" for f in blocking) else "P2"
        return Bar(FAIL, f"superagent: {_plural(len(blocking), f'open {worst} finding')}",
                   f"Superagent flagged {_plural(len(blocking), f'{worst} security concern')} — "
                   "resolve or refute them.")
    if scan is not None and scan.get("conclusion") in ("action_required", "failure"):
        return Bar(FAIL, "superagent: scan requires security review",
                   "Superagent's security scan requires review — resolve its concerns.")
    if scan is not None and scan.get("status") != "completed":
        return Bar(PENDING, "superagent scan pending", None)
    if scan is None and not entry.get("findings"):
        return Bar(PENDING, "awaiting superagent scan", None)
    return Bar(PASS, None, None)


# --- Socket ----------------------------------------------------------------

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
    alerts = _check_named({"checks": checks}, _SOCKET_ALERTS)
    report = _check_named({"checks": checks}, _SOCKET_REPORT)
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
        prev = (previous or {}).get(rid)
        e = parse(r, feed, head_sha, prev if isinstance(prev, dict) else None)
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
        return _coderabbit_findings_for_fix(entry)
    return []


def digest(reviewer: Reviewer, entry: dict | None, b: Bar, head_sha: str | None) -> dict:
    """The compact row projection: what the app, chat, the analyze bundle and
    deep search read."""
    reviewed = (entry or {}).get("reviewed_sha")
    stale: bool | None = (reviewed != head_sha) if reviewed and head_sha else None
    return {"id": reviewer.id, "label": reviewer.label, "kind": reviewer.kind,
            "status": b.status, "reason": b.reason,
            "score": (entry or {}).get("score"), "score_max": reviewer.score_max,
            "reviewed_sha": reviewed, "stale": stale,
            "open": open_counts(entry),
            "observed_at": (entry or {}).get("observed_at"),
            "checks": [{"name": c.get("name"), "conclusion": c.get("conclusion"),
                        "status": c.get("status"), "title": c.get("title")}
                       for c in (entry or {}).get("checks") or []],
            "extra": dict((entry or {}).get("extra") or {}),
            "summary_line": _summary_line_one(reviewer, entry, b, stale)}


def _summary_line_one(reviewer: Reviewer, entry: dict | None, b: Bar, stale: bool | None) -> str:
    if entry is None:
        return f"{reviewer.label} {b.status}"
    if reviewer is GREPTILE and entry.get("score") is not None:
        s = f"{reviewer.label} {entry['score']}/{reviewer.score_max}"
    else:
        counts = open_counts(entry)
        s = reviewer.label + (" " + ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items()))
                              if counts else f" {b.status}")
    if stale:
        s += " ⚠stale"
    return s


def summary_line(digests: Iterable[dict]) -> str:
    """One line over every reviewer digest: `Greptile 4/5 ⚠stale · CodeRabbit 2 major · Superagent pass`."""
    return " · ".join(d.get("summary_line") or d.get("label") or "" for d in digests) or "no automated review"


def digests(pr: Pr) -> dict[str, dict]:
    """Every reviewer's digest on `pr` — each reviewer that left an entry, plus
    each active one — keyed by id, in registry order."""
    from pipeline import review_policy
    out: dict[str, dict] = {}
    active = {r.id for r in review_policy.active_reviewers()}
    for rid, r in REVIEWERS.items():
        entry = pr.review_entry(rid)
        if entry is None and rid not in active:
            continue
        out[rid] = digest(r, entry, review_policy.bar(pr, r), pr.head_sha)
    return out


def evidence(entries: dict[str, dict] | None, head_sha: str | None) -> list[dict]:
    """Open bot findings as evidence for the security agents: every reviewer's
    unresolved findings, with reviewer, severity, location and text."""
    out: list[dict] = []
    for rid, entry in (entries or {}).items():
        r = REVIEWERS.get(rid)
        if r is None or not isinstance(entry, dict):
            continue
        for f in open_findings(entry):
            out.append({"reviewer": r.label, "kind": r.kind, "severity": f.get("severity"),
                        "path": f.get("path"), "line": f.get("line"), "title": f.get("title"),
                        "body": (f.get("body") or "")[:600]})
    return out


def version(reviewer: Reviewer, entry: dict | None) -> str | None:
    """An opaque change token for one reviewer's entry — what a post-retrigger
    wait polls on. None when the bot has left nothing."""
    if entry is None:
        return None
    extra = entry.get("extra") or {}
    raw = "|".join([str(entry.get("observed_at") or ""), str(entry.get("score") or ""),
                    str(entry.get("reviewed_sha") or ""), str(len(entry.get("findings") or [])),
                    str(extra.get("actionable") or ""), str(extra.get("concerns") or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def seen_summary(prs: Iterable[Pr]) -> dict:
    """The `reviewers` registry: each reviewer's latest observed activity over
    the open corpus, what `review_policy` auto-detection reads."""
    seen: dict[str, dict] = {}
    for pr in prs:
        if pr.state != "open":
            continue
        section = pr.reviews or {}
        for rid, entry in section.items():
            if rid not in REVIEWERS or not isinstance(entry, dict):
                continue
            at = entry.get("observed_at") or section.get("checked_at")
            if not at:
                continue
            cur = seen.setdefault(rid, {"last_observed_at": at, "prs": 0})
            cur["prs"] += 1
            if at > cur["last_observed_at"]:
                cur["last_observed_at"] = at
    return {"seen": seen, "computed_at": storekit.now()}
