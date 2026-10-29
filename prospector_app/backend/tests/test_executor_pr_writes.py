"""The executor's non-merge PR writes against a real store and a faked GitHub.

A close, a request-changes review and an inline comment each quote evidence
derived from the diff, so a moved head blocks them unless the operator
overrides, and the override is logged. A PR that is gone refuses regardless. A
landed write reflects its effect into the shared store and deep-links the
GitHub event it created.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline import settings
from pipeline import store as S
from prospector_app.backend import data, executor, models

HEAD = "abc123def456" + "0" * 28
NEWHEAD = "999fedcba321" + "0" * 28
COMMENT_URL = "https://github.com/o/r/pull/5#issuecomment-9"
REVIEW_URL = "https://github.com/o/r/pull/5#pullrequestreview-7"


@pytest.fixture
def gh_fake(tmp_path, monkeypatch):
    st = S.Store(tmp_path / "store")
    st.save_pr({"pr": 5,
                "meta": {"title": "PR 5", "state": "open", "head_sha": HEAD,
                         "checked_at": "2026-08-16T22:24:14+00:00"},
                "security": {"verdict": "GREEN", "against_head_sha": HEAD, "findings": [],
                             "checked_at": "2026-08-16T23:00:00+00:00"}})
    monkeypatch.setattr(data, "_store", st)
    monkeypatch.setattr("pipeline.bot_token.get", lambda: "tok")
    data.refresh()
    bot = settings.bot_login() + "[bot]"
    g = SimpleNamespace(
        store=st, runs=[], recorded=[],
        live={"state": "open", "merged": False, "head": {"sha": HEAD}},
        comments=[{"id": 11, "user": {"login": "someone"}, "body": "hi"}],
        reviews=[{"id": 21, "user": {"login": bot}, "state": "CHANGES_REQUESTED",
                  "html_url": REVIEW_URL}],
        bot=bot)
    monkeypatch.setattr(executor.gh, "fetch_pr", lambda n, timeout=30: g.live)
    monkeypatch.setattr(executor.gh, "issue_comments", lambda n, **k: g.comments)
    monkeypatch.setattr(executor.gh, "pr_reviews", lambda n, **k: g.reviews)

    def bot_run(argv, **kw):
        g.runs.append(argv)
        out = COMMENT_URL if argv[:3] == ["gh", "pr", "comment"] else ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(executor, "bot_run", bot_run)
    monkeypatch.setattr(executor.activity, "record",
                        lambda kind, **f: g.recorded.append({"kind": kind, **f}))
    return g


def _close(**kw):
    return executor.execute_pr(5, models.CloseAction(action="CLOSE_STALE", comment="going stale",
                                                     **kw), dry_run=False)


def test_a_close_comments_closes_and_reflects_closed(gh_fake):
    res = _close()
    assert res["status"] == "executed"
    assert [a[:3] for a in gh_fake.runs] == [["gh", "pr", "comment"], ["gh", "pr", "close"]]
    assert res["event_url"] == gh_fake.recorded[-1]["event_url"] == COMMENT_URL
    assert gh_fake.store.load_pr(5).state == "closed"


def test_a_close_does_not_repeat_its_own_comment(gh_fake):
    gh_fake.comments.append({"id": 12, "user": {"login": gh_fake.bot}, "body": "going stale"})
    res = _close()
    assert res["status"] == "executed" and "event_url" not in res
    assert [a[:3] for a in gh_fake.runs] == [["gh", "pr", "close"]]


def test_a_dry_run_close_writes_nothing(gh_fake):
    res = executor.execute_pr(5, models.CloseAction(action="CLOSE"), dry_run=True)
    assert res["status"] == "dry-run"
    assert gh_fake.runs == [] and gh_fake.store.load_pr(5).state == "open"


def test_a_reopen_withdraws_the_bots_comments_and_change_requests(gh_fake):
    gh_fake.store.edit_pr(5).record_live_state(state="closed")
    gh_fake.comments.append({"id": 12, "user": {"login": gh_fake.bot}, "body": "closing"})
    res = executor.reopen_pr(5, dry_run=False)
    assert res["status"] == "reopened"
    runs = [" ".join(a) for a in gh_fake.runs]
    assert any("DELETE" in r and r.endswith("comments/12") for r in runs)
    assert not any("comments/11" in r for r in runs)
    assert any("reviews/21/dismissals" in r for r in runs)
    assert gh_fake.store.load_pr(5).state == "open"


def test_a_request_changes_review_needs_a_body_and_links_its_review(gh_fake):
    assert executor.submit_review(5, "request-changes", "  ", dry_run=False)["status"] == "error"
    res = executor.submit_review(5, "request-changes", "fix X", dry_run=False)
    assert res["status"] == "executed" and res["event_url"] == REVIEW_URL


EVIDENCE_WRITES = {
    "close": lambda **kw: _close(**kw),
    "review": lambda override_stale=False: executor.submit_review(
        5, "request-changes", "fix the sentinel", dry_run=False,
        override_stale=override_stale),
    "line comment": lambda override_stale=False: executor.comment_line(
        5, "src/a.ts", 12, "this line is wrong", dry_run=False,
        override_stale=override_stale),
}


@pytest.mark.parametrize("write", EVIDENCE_WRITES)
def test_a_moved_head_holds_an_evidence_quoting_write(gh_fake, write):
    gh_fake.live["head"] = {"sha": NEWHEAD}

    res = EVIDENCE_WRITES[write]()

    assert res["status"] == "stale"
    assert HEAD[:7] in res["detail"] and NEWHEAD[:7] in res["detail"]
    assert res["stale"]["was"] == HEAD and res["stale"]["now"] == NEWHEAD
    assert {s["section"]: s["checked_at"] for s in res["stale"]["sections"]}["security"] \
        == "2026-08-16T23:00:00+00:00"
    assert gh_fake.runs == []
    # The observation is shared: every reader now sees the PR's facts as stale.
    assert gh_fake.store.load_pr(5).live_head_sha == NEWHEAD


@pytest.mark.parametrize("write", EVIDENCE_WRITES)
def test_an_override_posts_over_a_moved_head_and_is_logged(gh_fake, write):
    gh_fake.live["head"] = {"sha": NEWHEAD}

    res = EVIDENCE_WRITES[write](override_stale=True)

    assert res["status"] == "executed"
    assert gh_fake.recorded[-1]["stale_override"] is True


def test_a_clean_post_is_not_logged_as_an_override(gh_fake):
    EVIDENCE_WRITES["review"](override_stale=True)
    assert not gh_fake.recorded[-1].get("stale_override")


def test_a_merged_pr_refuses_even_with_the_override(gh_fake):
    gh_fake.live.update(state="closed", merged=True, head={"sha": NEWHEAD})
    res = EVIDENCE_WRITES["review"](override_stale=True)
    assert res["status"] == "skipped" and "merged" in res["detail"]
    assert gh_fake.runs == []
    assert gh_fake.store.load_pr(5).state == "merged"


def test_an_unreachable_github_does_not_wedge_a_comment(gh_fake):
    gh_fake.live = None
    assert EVIDENCE_WRITES["review"]()["status"] == "executed"


def test_a_review_retrigger_targets_the_current_head_so_is_not_held(gh_fake):
    gh_fake.live["head"] = {"sha": NEWHEAD}
    res = executor.retrigger_review(5, "greptile", dry_run=False)
    assert res["status"] == "executed" and res["reviewer"] == "greptile"
    assert executor.retrigger_review(5, "socket", dry_run=False)["status"] == "skipped"
