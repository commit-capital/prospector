"""Posting a PR's confirmed design findings for its author: what is posted, on
whose PRs, once per set of findings, and the standing it leaves the PR in."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import design_comment, reviewers, settings
from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import reviews_section, threat_section
from prospector_app.backend import automation, data, executor, service

HEAD = "abc123def456" + "0" * 28
URL = "https://github.com/o/r/pull/5#issuecomment-9"


def _finding(title: str, severity: str = "major", verdict: str | None = "confirmed",
             **kw: object) -> dict:
    f: dict = {"path": "src/a.py", "line": 3, "severity": severity, "category": "reuse",
               "title": title, "body": f"why {title}", "resolved": False, "outdated": False}
    if verdict is not None:
        f["verification"] = {"verdict": verdict, "reason": ""}
    return {**f, **kw}


FINDINGS = [_finding("dup helper"), _finding("wrong layer", "blocking", category="layering"),
            _finding("refuted one", verdict="refuted"), _finding("a nit", "minor", verdict=None),
            _finding("unsure one", "minor", verdict="unsure", claimed_severity="major"),
            _finding("fixed one", resolved=True)]


def _design(findings: list[dict]) -> dict:
    return {"kind": "review", "reviewed_sha": HEAD, "observed_at": _now(), "score": None,
            "findings": findings, "summary": "s", "checks": [],
            "extra": {"rubric": reviewers.DESIGN_RUBRIC}}


def _save(st: S.Store, *, association: str = "MEMBER",
          findings: list[dict] | None = None) -> None:
    st.save_pr({"pr": 5,
                "meta": {"title": "PR 5", "author": "al", "author_association": association,
                         "state": "open", "head_sha": HEAD, "updated_at": _now()},
                "signals": {"ci": "passing", "mergeable": True, "checked_at": _now(),
                            "against_head_sha": HEAD},
                "threat": threat_section(HEAD),
                "security": {"verdict": "GREEN", "against_head_sha": HEAD, "findings": [],
                             "checked_at": _now()},
                "reviews": reviews_section(HEAD, design=_design(
                    FINDINGS if findings is None else findings))})
    data.refresh()


@pytest.fixture
def gh_fake(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr(service, "changed_paths", lambda pr: ["src/a.py"])
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    monkeypatch.setenv("TRIAGE_DESIGN_REVIEW", "gate")
    monkeypatch.setenv("TRIAGE_DESIGN_COMMENT", "maintainers")
    _save(st)
    g = SimpleNamespace(store=st, runs=[], recorded=[], comments=[],
                        live={"state": "open", "merged": False, "head": {"sha": HEAD}})
    monkeypatch.setattr(executor.gh, "fetch_pr", lambda n, timeout=30: g.live)
    monkeypatch.setattr(executor.gh, "issue_comments", lambda n, **k: g.comments)

    def bot_run(argv, **kw):
        g.runs.append(argv)
        g.comments.append({"user": {"login": settings.bot_login() + "[bot]"},
                           "body": argv[argv.index("--body") + 1], "html_url": URL})
        return SimpleNamespace(returncode=0, stdout=URL + "\n", stderr="")
    monkeypatch.setattr(executor, "bot_run", bot_run)
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **f: g.recorded.append({"kind": kind, **f}))
    return g


def _post(dry_run: bool = False) -> dict:
    return executor.post_design_findings(5, dry_run=dry_run)


def test_failing_findings_are_posted_with_the_minor_ones_as_optional(gh_fake):
    pr = gh_fake.store.load_pr(5)
    found, optional = design_comment.findings(pr), design_comment.minor(pr)
    assert [f["title"] for f in found] == ["dup helper", "wrong layer"]
    assert [f["title"] for f in optional] == ["a nit"]
    text = design_comment.body(found, optional, HEAD)
    assert "**1. dup helper** (major, reuse)" in text and "**2. wrong layer**" in text
    assert text.index("Smaller things, optional") < text.index("**3. a nit** (minor, reuse)")
    assert "<details><summary>Details</summary>" in text and "`src/a.py:3`" in text
    for absent in ("refuted one", "unsure one", "fixed one"):
        assert absent not in text
    assert design_comment.head_marker(HEAD) in text
    assert text.endswith(design_comment.findings_marker(design_comment.fingerprint(found)) + " -->")


def test_without_a_failing_finding_there_is_no_optional_section():
    assert "Smaller things" not in design_comment.body([_finding("dup")], [], HEAD)


def test_finding_text_cannot_mention_link_or_break_out_of_its_details():
    text = design_comment.body([_finding("ping @alice in #12", body=(
        "</details><!-- x --> @bob [click](https://x.example) ![p](https://x.example/p.png)"))],
        [], HEAD)
    assert "@alice" not in text and "@bob" not in text and "#12" not in text
    assert text.count("</details>") == 1 and "<!-- x" not in text
    assert "[click](" not in text and "![p](" not in text


def test_a_maintainers_pr_gets_one_comment_and_reads_as_the_authors_move(gh_fake):
    res = _post()
    assert res["status"] == "executed" and res["event_url"] == URL
    argv = gh_fake.runs[0]
    assert argv[:4] == ["gh", "pr", "comment", "5"]
    assert gh_fake.recorded[-1]["kind"] == "design-findings"
    data.refresh()
    pr = gh_fake.store.load_pr(5)
    assert design_comment.posted(pr) is not None
    standing = automation.classify(pr)
    assert standing is not None
    assert (standing["bucket"], standing["owner"]) == ("author-design", "author")


def test_the_same_findings_are_never_posted_twice(gh_fake):
    assert _post()["status"] == "executed"
    _save(gh_fake.store)  # a re-review of the head drops the record, not the comment
    res = _post()
    assert res["status"] == "exists" and res["event_url"] == URL
    assert len(gh_fake.runs) == 1
    data.refresh()
    assert design_comment.posted(gh_fake.store.load_pr(5)) is not None


def test_a_reworded_review_of_the_same_head_is_not_posted_again(gh_fake):
    _post()
    _save(gh_fake.store, findings=[_finding("the same problem, reworded")])
    assert _post()["status"] == "exists"
    assert len(gh_fake.runs) == 1
    data.refresh()
    assert design_comment.posted(gh_fake.store.load_pr(5)) is not None


def test_a_new_head_with_different_findings_gets_a_new_comment(gh_fake):
    _post()
    gh_fake.comments[0]["body"] = gh_fake.comments[0]["body"].replace(HEAD, "e" * 40)
    _save(gh_fake.store, findings=[_finding("another problem")])
    assert _post()["status"] == "executed"
    assert len(gh_fake.runs) == 2


def test_a_red_security_verdict_posts_nothing(gh_fake):
    rec = gh_fake.store.load_pr(5).raw
    rec["security"]["verdict"] = "RED"
    gh_fake.store.save_pr(rec)
    data.refresh()
    assert _post()["status"] == "skipped" and gh_fake.runs == []


@pytest.mark.parametrize("audience, association, posts", [
    ("off", "MEMBER", False), ("maintainers", "CONTRIBUTOR", False),
    ("maintainers", "MEMBER", True), ("everyone", "CONTRIBUTOR", True)])
def test_the_rollout_switch_picks_whose_prs_are_posted(gh_fake, monkeypatch, audience,
                                                       association, posts):
    monkeypatch.setenv("TRIAGE_DESIGN_COMMENT", audience)
    _save(gh_fake.store, association=association)
    assert (_post()["status"] == "executed") is posts
    assert bool(gh_fake.runs) is posts


def test_the_switch_defaults_off(monkeypatch):
    monkeypatch.delenv("TRIAGE_DESIGN_COMMENT", raising=False)
    assert settings.design_comment_audience() == "off"


def test_minor_findings_alone_post_nothing(gh_fake):
    _save(gh_fake.store, findings=[f for f in FINDINGS if f["title"] not in
                                   ("dup helper", "wrong layer")])
    res = _post()
    assert res["status"] == "skipped" and gh_fake.runs == []


def test_a_moved_head_posts_nothing(gh_fake):
    gh_fake.live["head"] = {"sha": "f" * 40}
    assert _post()["status"] == "skipped" and gh_fake.runs == []


@pytest.mark.parametrize("token", ["tok", None])
def test_a_dry_run_posts_and_records_nothing(gh_fake, monkeypatch, token):
    monkeypatch.setattr("pipeline.bot_token.get", lambda: token)
    res = _post(dry_run=token is not None)
    assert res["status"] == "dry-run" and gh_fake.runs == []
    assert gh_fake.recorded[-1]["dry_run"] is True
    data.refresh()
    assert design_comment.posted(gh_fake.store.load_pr(5)) is None
