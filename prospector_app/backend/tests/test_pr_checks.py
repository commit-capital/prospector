"""Checks rollup surfaces the threat gate (gates.pr_clean): a committed secret
is a failing check, so the merge block is visible, not just in clean_reasons."""
from pipeline import model
from prospector_app.backend import pr_checks
from pipeline.testsupport import reviews_section
from pipeline import review_policy

HEAD = "abc123"
NOW = "2026-06-10T00:00:00+00:00"


def _pr(**over):
    rec = {
        "pr": 1,
        "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                 "head_sha": HEAD, "checked_at": NOW},
        "signals": {"ci": "passing", "mergeable": True, "has_tests": True,
                    "checked_at": NOW, "against_head_sha": HEAD},
        "reviews": reviews_section(HEAD, NOW),
        "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": HEAD},
    }
    rec.update(over)
    return model.Pr(None, rec)


def _check(rec):
    c = pr_checks.checks_for_record(rec)
    return next((x for x in c["checks"] if x["name"] == "No committed secrets"), None)


def test_secret_leak_shows_failing_check():
    chk = _check(_pr(threat={"verdict": "suspicious", "signatures": ["secret-leak"]}))
    assert chk and chk["status"] == "fail" and "credential" in chk["detail"]


def test_malicious_shows_failing_check():
    chk = _check(_pr(threat={"verdict": "malicious", "signatures": ["blocked-actor"]}))
    assert chk and chk["status"] == "fail" and "malicious" in chk["detail"]


def test_clear_threat_passes_check():
    chk = _check(_pr(threat={"verdict": "clear", "signatures": []}))
    assert chk and chk["status"] == "pass"


def test_checks_carry_stable_keys():
    # #578: the per-check filter matches on this key, not the display name
    # (which varies with the configured review provider / default branch).
    rec = _pr(threat={"verdict": "clear", "signatures": []},
              security={"verdict": "GREEN", "findings": [], "checked_at": NOW, "against_head_sha": HEAD},
              verify={"outcome": "verified-fix", "signals": {}, "findings": [],
                      "against_head_sha": HEAD, "against_base_sha": "b", "checked_at": NOW})
    c = pr_checks.checks_for_record(rec)
    by_key = {x["key"]: x["name"] for x in c["checks"]}
    assert by_key == {
        "review": "Code review", "ci": "CI", "scans": "Security scans",
        "mergeable": "No merge conflicts",
        "tests": "Includes tests", "drift": "Still applies to trunk",
        "secrets": "No committed secrets", "security": "Deep security review",
        "verify": "Dynamic verification",
    }


def test_all_checks_shown_even_with_no_data():
    # #581: the panel shows every check this deployment could run, not only
    # the ones a phase has actually produced data for — so a brand-new PR
    # shows every row (na/never-run for the ones nothing has run yet).
    c = pr_checks.checks_for_record(_pr())  # signals + drift only; no threat/security/verify
    keys = [x["key"] for x in c["checks"]]
    assert keys == ["review", "ci", "scans", "mergeable", "tests", "drift", "secrets",
                    "security", "verify"]
    never_run = {x["key"] for x in c["checks"] if x["status"] == "na"}
    assert never_run == {"scans", "secrets", "security", "verify"}


def test_no_threat_section_shows_never_run_check():
    # A PR the threat scan never ran on shouldn't assert "no secrets" — but the
    # check still shows, as never-run, so the panel lists every check this
    # deployment could run (#581).
    chk = _check(_pr())
    assert chk and chk["status"] == "na" and chk["at"] is None


def _review_check(rec):
    c = pr_checks.checks_for_record(rec)
    return next((x for x in c["checks"] if x["key"] == "review"), None)


def test_greptile_review_check_at_bar():
    chk = _review_check(_pr())      # greptile 5 under the pinned greptile profile
    assert chk and chk["name"] == "Code review"
    assert chk["status"] == "pass" and chk["detail"] == "Greptile 5/5"


def test_review_check_na_when_no_reviewer_active(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    chk = _review_check(_pr())
    assert chk and chk["status"] == "na"


def test_review_and_scans_rows_aggregate_every_active_reviewer(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile,coderabbit,superagent,socket")
    review_policy.reset()
    rec = _pr(reviews=reviews_section(
        HEAD, NOW,
        superagent={"kind": "scanner", "reviewed_sha": HEAD, "checks": [], "extra": {},
                    "findings": [{"severity": "P1", "resolved": False, "outdated": False}]}))
    c = pr_checks.checks_for_record(rec)
    rows = {x["key"]: x for x in c["checks"]}
    assert rows["review"]["status"] == "warn"           # CodeRabbit pending
    assert "CodeRabbit pending" in rows["review"]["detail"]
    assert rows["scans"]["status"] == "fail"            # Superagent P1; Socket na is ignored
    assert "Superagent 1 P1" in rows["scans"]["detail"]


def _verify_check(rec):
    c = pr_checks.checks_for_record(rec)
    return next((x for x in c["checks"] if x["name"] == "Dynamic verification"), None)


def _verify_section(outcome, *, head=HEAD, at=None, findings=(), signals=None):
    from datetime import datetime, timezone
    return {"outcome": outcome, "signals": signals or {}, "findings": list(findings),
            "against_head_sha": head, "against_base_sha": "b606869",
            "checked_at": at or datetime.now(timezone.utc).isoformat()}


def _repro_signals(*, exit_code):
    """A verify record whose blind pass authored a repro that ran and exited
    `exit_code`, with the judge rating it a match — the rating that carries the
    record to verified-fix when the exit itself means something."""
    return {"blind_adequacy": {"repro_command": "node --test repro.mjs"},
            "independent_repro": {"ran": True, "exit_code": exit_code},
            "repro_reason_match": {"matches": True, "applicable": True,
                                   "confidence": "high"}}


def test_current_verified_fix_passes_check():
    # A clean bill needs every attempted signal corroborating, the independent
    # repro included — so the fixture carries one that ran and was rated a match.
    chk = _verify_check(_pr(verify=_verify_section(
        "verified-fix", signals=_repro_signals(exit_code=20))))
    assert chk and chk["status"] == "pass" and chk["detail"] == "verified-fix"


def test_verified_fix_on_partial_evidence_warns():
    # The reported case (#20): the repro's container errored, so the run proved
    # nothing about the base, yet the stored outcome is still verified-fix. The
    # check must not read as a clean pass — the harness is what needs fixing.
    chk = _verify_check(_pr(verify=_verify_section(
        "verified-fix", signals=_repro_signals(exit_code=137))))
    assert chk and chk["status"] == "warn"
    assert "PARTIAL" in chk["detail"] and "harness level" in chk["detail"]


def test_verified_fix_on_corroborated_evidence_still_passes():
    # The demotion is scoped to a repro whose exit carries no meaning: a plain
    # failing repro on the pinned base still corroborates and still passes.
    chk = _verify_check(_pr(verify=_verify_section(
        "verified-fix", signals=_repro_signals(exit_code=20))))
    assert chk and chk["status"] == "pass" and "PARTIAL" not in chk["detail"]


def test_partial_evidence_never_promotes_a_failing_outcome():
    # This display is stricter than the merge gate: agent-verified already
    # fails it, and partial evidence must not lift anything to warn.
    chk = _verify_check(_pr(verify=_verify_section(
        "agent-verified", signals=_repro_signals(exit_code=137))))
    assert chk and chk["status"] == "fail"


def test_a_verified_fix_with_no_repro_authored_warns_not_passes():
    # Operator decision 2026-07-30: a red->green with no independent repro at
    # all attests only as far as the author's own test reaches, so it is
    # partial evidence rather than a clean bill.
    chk = _verify_check(_pr(verify=_verify_section("verified-fix", signals={
        "blind_adequacy": {"repro_command": None}})))
    assert chk and chk["status"] == "warn"
    assert "no independent repro was authored" in chk["detail"]


def test_current_blocking_outcomes_fail_check():
    # exactly the set merge_eligibility blocks on: a concluded current
    # non-verified-fix outcome.
    for outcome in ("escalate", "not-verified", "needs-rebase", "deps-touched",
                    "unverifiable-no-test"):
        chk = _verify_check(_pr(verify=_verify_section(outcome)))
        assert chk and chk["status"] == "fail" and outcome in chk["detail"]


def test_stale_verify_warns():
    chk = _verify_check(_pr(verify=_verify_section("verified-fix", head="older")))
    assert chk and chk["status"] == "warn" and "STALE" in chk["detail"]


def test_null_outcome_warns_as_in_progress():
    chk = _verify_check(_pr(verify=_verify_section(None)))
    assert chk and chk["status"] == "warn" and "in progress" in chk["detail"]


def test_verify_findings_counted():
    chk = _verify_check(_pr(verify=_verify_section(
        "verified-fix", findings=[{"title": "t"}, {"title": "u"}])))
    assert chk and "2 finding(s)" in chk["detail"]


def test_no_verify_section_shows_never_run_check():
    chk = _verify_check(_pr())
    assert chk and chk["status"] == "na" and chk["at"] is None


def test_checks_carry_their_own_section_timestamp():
    # #550: each check needs its own "when" — a shared page-load timestamp
    # can't tell a 25-day-old security verdict from a fresh CI run.
    sec_at = "2026-05-01T00:00:00+00:00"
    rec = _pr(security={"verdict": "GREEN", "findings": [], "checked_at": sec_at, "against_head_sha": HEAD})
    c = pr_checks.checks_for_record(rec)
    review = next(x for x in c["checks"] if x["key"] == "review")
    assert review["at"] == NOW  # from the reviews section
    sec = next(x for x in c["checks"] if x["name"] == "Deep security review")
    assert sec["at"] == sec_at  # from the security section, not the signals one
