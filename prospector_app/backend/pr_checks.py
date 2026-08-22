"""Per-PR "checks" rollup — how thoroughly has this PR been vetted?

Reads ONLY the store record (signals + reviews + security + drift + analysis
freshness); the pass bar matches gates.py exactly (every active reviewer's and
scanner's bar, CI passing, mergeable, fresh facts, GREEN security).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import freshness
from pipeline import gates
from pipeline import review_policy
from pipeline import reviewers
from pipeline import settings

if TYPE_CHECKING:
    from pipeline.model import Pr


def _c(key: str, name: str, status: str, detail: str = "", at: str | None = None) -> dict:
    return {"key": key, "name": name, "status": status, "detail": detail, "at": at}


# Stable identifiers for each named check, independent of the display name
# (which varies with the configured review provider / default branch) — what
# the PR Explorer's per-check filter (#578) matches against.
CHECK_KEYS = ("review", "ci", "scans", "mergeable", "tests", "drift", "secrets", "security",
              "verify")


def _aggregate(key: str, name: str, rec: Pr, kind: str, at: str | None) -> dict:
    """One check row over every active reviewer of `kind`: pass when all pass,
    fail when any fails, warn when any is stale or pending, na when none is
    active. The detail names each reviewer's verdict."""
    active = review_policy.active_reviewers(kind)
    if not active:
        return _c(key, name, "na", "no active " + ("reviewer" if kind == reviewers.REVIEW else "scanner"), None)
    parts: list[str] = []
    statuses: list[str] = []
    for r in active:
        b = review_policy.bar(rec, r)
        d = reviewers.digest(r, rec.review_entry(r.id), b, rec.head_sha)
        parts.append(d["summary_line"])
        statuses.append(b.status)
    live = [s for s in statuses if s != reviewers.NA]
    if not live:
        status = "na"
    elif reviewers.FAIL in live:
        status = "fail"
    elif reviewers.STALE in live or reviewers.PENDING in live:
        status = "warn"
    else:
        status = "pass"
    return _c(key, name, status, " · ".join(parts), at)


def checks_for_record(rec: Pr, today: str | None = None) -> dict:
    """Roll `rec` up into named vetting checks — every check this deployment
    could run on a PR, not only the ones that have. A check with no data yet
    still gets a row, `status="na"`, so the panel shows the full check surface
    up front instead of growing rows in as phases happen to run.
    `today` (an ISO date) is the reference for age-window checks; None means
    the current UTC date."""
    by_key: dict[str, dict] = {}
    sig = rec.signals or {}
    sig_at = sig.get("checked_at")

    reviews_at = (rec.reviews or {}).get("checked_at")
    by_key["review"] = _aggregate("review", "Code review", rec, reviewers.REVIEW, reviews_at)
    by_key["scans"] = _aggregate("scans", "Security scans", rec, reviewers.SCANNER, reviews_at)

    ci = sig.get("ci")
    if ci:
        by_key["ci"] = _c("ci", "CI", "pass" if ci == "passing" else "fail" if ci == "failing" else "na", ci, sig_at)
    else:
        by_key["ci"] = _c("ci", "CI", "na", "no CI signal recorded yet", None)

    if "mergeable" in sig:
        by_key["mergeable"] = _c("mergeable", "No merge conflicts", "pass" if sig["mergeable"] else "fail",
                                  "clean" if sig["mergeable"] else "has conflicts", sig_at)
    else:
        by_key["mergeable"] = _c("mergeable", "No merge conflicts", "na", "not checked yet", None)

    if sig.get("has_tests") is not None:
        by_key["tests"] = _c("tests", "Includes tests", "pass" if sig["has_tests"] else "warn",
                              "tests present" if sig["has_tests"] else "no tests", sig_at)
    else:
        by_key["tests"] = _c("tests", "Includes tests", "na", "not checked yet", None)

    extra: list[dict] = []
    if sig and not freshness.is_current(rec, "signals"):
        extra.append(_c("signals_fresh", "Signals fresh", "warn", "head moved since signals were fetched", sig_at))

    drift = rec.section("drift") or {}
    ds = rec.drift_state
    if ds:
        st = {"applicable": "pass", "conflicts": "fail", "already-fixed": "fail"}.get(ds, "na")
        by_key["drift"] = _c("drift", f"Still applies to {settings.default_branch()}", st, ds, drift.get("checked_at"))
    else:
        by_key["drift"] = _c("drift", f"Still applies to {settings.default_branch()}", "na", "not checked yet", None)

    # The threat gate (gates.pr_clean): a committed credential or a malicious
    # verdict is a hard merge block. Surface it as a check so the refusal is
    # visible, not just buried in the API's clean_reasons.
    threat = rec.section("threat")
    if threat:
        sigs = rec.threat_signatures
        verdict = rec.threat_verdict
        threat_at = threat.get("checked_at")
        if verdict == "malicious":
            by_key["secrets"] = _c("secrets", "No committed secrets", "fail", "malicious: " + (", ".join(sigs) or "flagged"), threat_at)
        elif "secret-leak" in sigs:
            by_key["secrets"] = _c("secrets", "No committed secrets", "fail", "a live-looking credential is committed in the diff", threat_at)
        else:
            by_key["secrets"] = _c("secrets", "No committed secrets", "pass", "threat scan clear", threat_at)
    else:
        by_key["secrets"] = _c("secrets", "No committed secrets", "na", "not scanned yet", None)

    security = rec.section("security")
    if security:
        current = freshness.is_current(rec, "security", max_age_days=gates.SECURITY_MAX_AGE_DAYS,
                                       today=today)
        v = rec.security_verdict
        st = {"GREEN": "pass", "YELLOW": "warn", "RED": "fail"}.get(v or "", "na")
        detail = f"{v} · {len(rec.findings)} finding(s)"
        if not current:
            reason = freshness.currency_failure(rec, "security", max_age_days=gates.SECURITY_MAX_AGE_DAYS,
                                                today=today) or "stale"
            tail = "earlier head" if reason.startswith("stale") else reason
            st, detail = "warn", f"{detail} · STALE — {tail}"
        by_key["security"] = _c("security", "Deep security review", st, detail, security.get("checked_at"))
    else:
        by_key["security"] = _c("security", "Deep security review", "na", "not run yet", None)

    # Dynamic verification (VERIFY): the sandbox red→green run. A current
    # verified-fix passes the automatic bar; every other concluded current
    # outcome fails that strict check. This display is stricter than the human
    # merge gate, where an unverifiable outcome is non-blocking because it carries
    # no negative evidence. A null outcome warns because it concluded nothing.
    # A verified-fix whose attempted signals did not all corroborate warns
    # instead of passing, naming the gap: the red→green stands, but a partial
    # record is evidence the operator has to weigh rather than a clean bill,
    # and merge_allowed already refuses to auto-recommend it.
    verify = rec.section("verify")
    if verify:
        o = rec.verify_outcome
        detail = o or "in progress — blind verdict committed, no conclusion yet"
        if rec.verify_findings:
            detail += f" · {len(rec.verify_findings)} finding(s)"
        why_stale = freshness.currency_failure(rec, "verify",
                                               max_age_days=gates.VERIFY_MAX_AGE_DAYS)
        if o is None:
            st = "warn"
        elif why_stale:
            tail = "earlier head" if why_stale.startswith("stale") else why_stale
            st, detail = "warn", f"{detail} · STALE — {tail}"
        else:
            st = "pass" if o == "verified-fix" else "fail"
            partial = gates.verify_signals_incomplete(rec) if st == "pass" else None
            if partial:
                st, detail = "warn", f"{detail} · PARTIAL — {partial}"
        by_key["verify"] = _c("verify", "Dynamic verification", st, detail, verify.get("checked_at"))
    else:
        by_key["verify"] = _c("verify", "Dynamic verification", "na", "not run yet", None)

    out = [by_key[k] for k in CHECK_KEYS if k in by_key] + extra
    ran = [c for c in out if c["status"] != "na"]
    passed = [c for c in ran if c["status"] == "pass"]
    return {"checks": out, "passed": len(passed), "total": len(ran)}
