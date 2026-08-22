"""pipeline/reviewers.py — the ONE registry of automated PR reviewers and scanners."""
from pipeline import reviewers
from pipeline.model import Pr
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
        assert "greptile-apps" in reviewers.app_slugs()


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
        e = reviewers.parse(reviewers.GREPTILE, _feed(conversation=False), HEAD, prev)
        assert e["score"] == 3 and e["summary"] == "s" and e["checks"] == []
        assert reviewers.parse(reviewers.GREPTILE, _feed(conversation=False), HEAD, None) is None

    def test_bar(self):
        at_head = {"kind": "review", "score": 5, "reviewed_sha": HEAD, "findings": [], "checks": [], "extra": {}}
        assert reviewers.bar(reviewers.GREPTILE, at_head, HEAD, threshold=5).status == "pass"
        b = reviewers.bar(reviewers.GREPTILE, dict(at_head, score=3), HEAD, threshold=5)
        assert b.status == "fail" and b.reason == "greptile 3/5" and "5/5" in (b.ask or "")
        assert reviewers.bar(reviewers.GREPTILE, dict(at_head, reviewed_sha=OLD), HEAD, threshold=5).status == "stale"
        pending = reviewers.bar(reviewers.GREPTILE, None, HEAD, threshold=5)
        assert pending.status == "pending" and pending.reason == "awaiting greptile review"
        assert reviewers.bar(reviewers.GREPTILE, dict(at_head, score=4), HEAD, threshold=4).status == "pass"

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


SA_P1 = ("<!-- brin-pr-finding -->\n**P1:** Hidden webhook plugin with hardcoded private IP default "
         "exfiltrates issue data\n\nNew webhook plugin …")


def _sa_checks(scan: str = "success", status: str = "completed") -> list[dict]:
    return [{"app": "superagent-security", "name": "Superagent Security Scan", "status": status,
             "conclusion": scan if status == "completed" else None,
             "title": "PR requires security review" if scan == "action_required" else "PR scan passed",
             "summary": "2 security concern(s) detected." if scan == "action_required"
             else "No suspicious PR changes were detected.", "url": "c1"},
            {"app": "superagent-security", "name": "Superagent Supply Chain Scan", "status": "completed",
             "conclusion": "neutral", "title": "Supply chain scan inconclusive",
             "summary": "motion (npm) changed", "url": "c2"},
            {"app": "superagent-security", "name": "Contributor trust", "status": "completed",
             "conclusion": "success", "title": "Contributor verified",
             "summary": "Score: 89/100 · Verdict: safe", "url": "c3"}]


class TestSuperagent:
    def test_parse_with_findings(self):
        feed = _feed(
            reviews=[{"id": 3, "login": "superagent-security[bot]", "state": "COMMENTED", "commit": HEAD,
                      "body": "<!-- brin-pr-finding -->\nSuperagent found 2 security concern(s).",
                      "at": "2026-08-21T15:21:03Z", "url": "r"}],
            threads=[{"id": 1, "login": "superagent-security[bot]", "path": "m.ts", "line": 28, "body": SA_P1,
                      "commit": HEAD, "original_commit": HEAD, "resolved": False, "outdated": False,
                      "at": "2026-08-21T15:21:03Z", "url": "t"}],
            check_runs=_sa_checks("action_required"))
        e = reviewers.parse(reviewers.SUPERAGENT, feed, HEAD, None)
        assert e["kind"] == "scanner" and e["reviewed_sha"] == HEAD
        assert e["extra"] == {"trust_score": 89, "trust_verdict": "safe", "concerns": 2}
        assert e["findings"][0]["severity"] == "P1"
        assert e["findings"][0]["title"].startswith("Hidden webhook plugin")
        assert len(e["checks"]) == 3
        b = reviewers.bar(reviewers.SUPERAGENT, e, HEAD)
        assert b.status == "fail" and b.reason == "superagent: 1 open P1 finding"

    def test_parse_checks_only_passes(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=_sa_checks()), HEAD, None)
        assert e is not None and e["findings"] == [] and e["reviewed_sha"] == HEAD
        assert reviewers.bar(reviewers.SUPERAGENT, e, HEAD).status == "pass"

    def test_bar_pending_while_scan_runs(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=_sa_checks(status="in_progress")), HEAD, None)
        b = reviewers.bar(reviewers.SUPERAGENT, e, HEAD)
        assert b.status == "pending" and b.reason == "superagent scan pending" and b.ask is None

    def test_bar_fails_on_action_required_without_threads(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=_sa_checks("action_required")), HEAD, None)
        assert reviewers.bar(reviewers.SUPERAGENT, e, HEAD).status == "fail"

    def test_no_entry_is_pending(self):
        assert reviewers.parse(reviewers.SUPERAGENT, _feed(), HEAD, None) is None
        assert reviewers.bar(reviewers.SUPERAGENT, None, HEAD).status == "pending"

    def test_scanner_never_feeds_fix(self):
        e = reviewers.parse(reviewers.SUPERAGENT, _feed(check_runs=_sa_checks()), HEAD, None)
        assert reviewers.findings_for_fix(reviewers.SUPERAGENT, e, HEAD, None) == []
        assert reviewers.severity(reviewers.SUPERAGENT, e, None) is None


def _socket_checks(alerts: str = "success") -> list[dict]:
    return [{"app": "socket-security", "name": "Socket Security: Pull Request Alerts", "status": "completed",
             "conclusion": alerts, "title": f"Pull Request #1 Alerts: {alerts.title()}",
             "summary": "|Report|Status|Message|", "url": "s1"},
            {"app": "socket-security", "name": "Socket Security: Project Report", "status": "completed",
             "conclusion": "success", "title": "Project Report: Success", "summary": "x",
             "url": "https://socket.dev/dashboard/org/x/sbom/abc"}]


class TestSocket:
    def test_parse_and_pass(self):
        feed = _feed(comments=[{"id": 7, "login": "socket-security[bot]",
                                "body": "**Review the following changes in direct dependencies.** <table>…</table>",
                                "at": "2026-08-21T00:00:00Z", "updated_at": None, "url": "c"}],
                     check_runs=_socket_checks())
        e = reviewers.parse(reviewers.SOCKET, feed, HEAD, None)
        assert e["kind"] == "scanner" and e["reviewed_sha"] == HEAD
        assert e["extra"]["alerts_status"] == "success"
        assert e["extra"]["report_url"] == "https://socket.dev/dashboard/org/x/sbom/abc"
        assert e["summary"].startswith("**Review the following changes")
        assert reviewers.bar(reviewers.SOCKET, e, HEAD).status == "pass"

    def test_bar_fails_on_alerts(self):
        e = reviewers.parse(reviewers.SOCKET, _feed(check_runs=_socket_checks("failure")), HEAD, None)
        b = reviewers.bar(reviewers.SOCKET, e, HEAD)
        assert b.status == "fail" and b.reason == "socket: new dependency alerts"

    def test_neutral_passes_and_absent_is_na(self):
        e = reviewers.parse(reviewers.SOCKET, _feed(check_runs=_socket_checks("neutral")), HEAD, None)
        assert reviewers.bar(reviewers.SOCKET, e, HEAD).status == "pass"
        assert reviewers.parse(reviewers.SOCKET, _feed(), HEAD, None) is None
        assert reviewers.bar(reviewers.SOCKET, None, HEAD).status == "na"


class TestProjections:
    def test_parse_all_and_digest(self):
        feed = _feed(check_runs=_sa_checks() + _socket_checks() + [
            {"app": "greptile-apps", "name": "Greptile Review", "status": "completed", "conclusion": "failure",
             "title": "Confidence 4/5 — below your required 5/5", "summary": "", "url": "g"}])
        entries = reviewers.parse_all(feed, HEAD, None)
        assert set(entries) == {"greptile", "superagent", "socket"}
        d = reviewers.digest(reviewers.GREPTILE, entries["greptile"],
                             reviewers.bar(reviewers.GREPTILE, entries["greptile"], HEAD, threshold=5), HEAD)
        assert d["status"] == "fail" and d["score"] == 4 and d["stale"] is False
        assert d["summary_line"] == "Greptile 4/5"
        assert reviewers.summary_line([d, {"summary_line": "Socket pass"}]) == "Greptile 4/5 · Socket pass"
        assert reviewers.summary_line([]) == "no automated review"

    def test_evidence_and_version(self):
        entries = {"superagent": {"kind": "scanner", "findings": [
            {"severity": "P1", "path": "m.ts", "line": 28, "title": "t", "body": "b", "resolved": False, "outdated": False},
            {"severity": "P2", "path": "n.ts", "line": 1, "title": "u", "body": "b", "resolved": True, "outdated": False}]}}
        ev = reviewers.evidence(entries, HEAD)
        assert len(ev) == 1 and ev[0]["reviewer"] == "Superagent" and ev[0]["severity"] == "P1"
        e = {"observed_at": "x", "score": 4, "reviewed_sha": HEAD, "findings": [], "extra": {}}
        assert reviewers.version(reviewers.GREPTILE, e) != reviewers.version(reviewers.GREPTILE, dict(e, score=5))
        assert reviewers.version(reviewers.GREPTILE, None) is None

    def test_seen_summary_over_open_corpus(self):
        prs = [Pr(None, {"pr": 1, "meta": {"state": "open", "head_sha": "h"},
                         "reviews": {"greptile": {"kind": "review", "observed_at": "2026-08-20T00:00:00Z"},
                                     "socket": {"kind": "scanner", "observed_at": None,
                                                "checks": [{"name": "Socket Security: Project Report"}]},
                                     "coderabbit": {"kind": "review", "observed_at": None, "checks": []},
                                     "checked_at": "2026-08-21T00:00:00Z"}}),
               Pr(None, {"pr": 2, "meta": {"state": "closed", "head_sha": "h"},
                         "reviews": {"coderabbit": {"kind": "review", "observed_at": "2026-08-21T00:00:00Z"}}})]
        seen = reviewers.seen_summary(prs)["seen"]
        assert seen["greptile"] == {"last_observed_at": "2026-08-20T00:00:00Z", "prs": 1}
        assert seen["socket"]["last_observed_at"] == "2026-08-21T00:00:00Z"   # check run at the head
        assert "coderabbit" not in seen   # an empty shell dates nothing; PR 2 is closed


def test_greptile_finding_title_drops_its_html_badge():
    feed = _feed(threads=[{"id": 1, "login": "greptile-apps[bot]", "path": "w.ts", "line": 3,
                           "body": '<a href="#"><img alt="P1" src="https://x/p1.svg"></a> **logic:** retry never exits\n\nmore',
                           "commit": HEAD, "original_commit": HEAD, "resolved": False, "outdated": False,
                           "at": "2026-08-21T00:00:00Z", "url": "t"}])
    e = reviewers.parse(reviewers.GREPTILE, feed, HEAD, None)
    assert e["findings"][0]["title"] == "**logic:** retry never exits"


def test_coderabbit_stray_comment_dates_the_entry():
    feed = _feed(comments=[{"id": 1, "login": "coderabbitai[bot]", "body": "Review paused.",
                            "at": "2026-06-18T00:00:00Z", "updated_at": None, "url": "c"}])
    e = reviewers.parse(reviewers.CODERABBIT, feed, HEAD, None)
    assert e is not None and e["observed_at"] == "2026-06-18T00:00:00Z"
