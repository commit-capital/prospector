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


def test_only_open_confirmed_blocking_and_major_findings_are_posted(gh_fake):
    found = design_comment.findings(gh_fake.store.load_pr(5))
    assert [f["title"] for f in found] == ["dup helper", "wrong layer"]
    text = design_comment.body(found, HEAD)
    assert "**1. dup helper** (major, reuse)" in text and "**2. wrong layer**" in text
    assert "<details><summary>Details</summary>" in text and "`src/a.py:3`" in text
    for absent in ("refuted one", "a nit", "unsure one", "fixed one"):
        assert absent not in text
    assert text.endswith(design_comment.marker(design_comment.fingerprint(found)))


def test_finding_text_cannot_mention_anyone_or_break_out_of_its_details():
    text = design_comment.body([_finding("ping @alice", body="</details><!-- x --> @bob")], HEAD)
    assert "@alice" not in text and "@bob" not in text
    assert text.count("</details>") == 1 and "<!-- x" not in text


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


def test_different_findings_get_a_new_comment(gh_fake):
    _post()
    _save(gh_fake.store, findings=[_finding("another problem")])
    assert _post()["status"] == "executed"
    assert len(gh_fake.runs) == 2


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


def test_no_confirmed_blocking_or_major_finding_posts_nothing(gh_fake):
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
