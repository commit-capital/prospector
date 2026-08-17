"""Evidence-quoting writes re-check the live head before posting. A close
comment, a request-changes review, and an inline comment all state something
derived from the diff, so a push since the analysis makes them wrong. The block
is overridable — a named operator may still want to post — and the override is
recorded in the Activity log.
"""
from prospector_app.backend import data
from prospector_app.backend import executor
from prospector_app.backend import models
from pipeline.model import Pr

HEAD = "abc123def456"
NEWHEAD = "999fedcba321"

_OPEN_MOVED = {"state": "open", "merged": False, "head": NEWHEAD, "mergeable_state": "clean"}
_OPEN_SAME = {"state": "open", "merged": False, "head": HEAD, "mergeable_state": "clean"}
_MERGED = {"state": "closed", "merged": True, "head": NEWHEAD, "mergeable_state": "clean"}


def _rec(n=5):
    return Pr(None, {
        "pr": n,
        "meta": {"title": f"PR {n}", "state": "open", "head_sha": HEAD,
                 "checked_at": "2026-08-16T22:24:14+00:00"},
        "analysis": {"disposition": "request-changes", "against_head_sha": HEAD,
                     "checked_at": "2026-08-16T22:30:53+00:00"},
        "security": {"verdict": "GREEN", "against_head_sha": HEAD,
                     "checked_at": "2026-08-16T23:00:00+00:00"},
    })


def _setup(monkeypatch, *, live, events=None):
    monkeypatch.setattr(data, "prs", lambda: {5: _rec()})
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {5: [1]})
    monkeypatch.setattr(executor, "_pr_live", lambda n: live)
    monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)
    monkeypatch.setattr(executor, "_record_observed_head", lambda n, head: None)
    monkeypatch.setattr(executor.activity, "record",
                        lambda *a, **k: (events.append(k) if events is not None else None))


class TestReviewBlocksOnStaleEvidence:
    def test_moved_head_blocks_request_changes(self, monkeypatch):
        _setup(monkeypatch, live=_OPEN_MOVED)
        res = executor.submit_review(5, "request-changes", "fix the sentinel",
                                     token="tok", dry_run=False)
        assert res["status"] == "stale"
        assert HEAD[:7] in res["detail"] and NEWHEAD[:7] in res["detail"]

    def test_the_block_names_which_facts_went_stale(self, monkeypatch):
        _setup(monkeypatch, live=_OPEN_MOVED)
        res = executor.submit_review(5, "request-changes", "fix the sentinel",
                                     token="tok", dry_run=False)
        stale = res["stale"]
        assert stale["was"] == HEAD and stale["now"] == NEWHEAD
        named = {s["section"]: s["checked_at"] for s in stale["sections"]}
        assert named["analysis"] == "2026-08-16T22:30:53+00:00"
        assert named["security"] == "2026-08-16T23:00:00+00:00"

    def test_unchanged_head_posts_normally(self, monkeypatch):
        _setup(monkeypatch, live=_OPEN_SAME)
        monkeypatch.setattr(executor, "bot_run",
                            lambda argv, token: type("R", (), {"returncode": 0, "stderr": ""})())
        monkeypatch.setattr(executor, "_latest_bot_review_url", lambda n: None)
        res = executor.submit_review(5, "request-changes", "fix the sentinel",
                                     token="tok", dry_run=False)
        assert res["status"] == "executed"

    def test_override_posts_and_is_logged(self, monkeypatch):
        events: list = []
        _setup(monkeypatch, live=_OPEN_MOVED, events=events)
        monkeypatch.setattr(executor, "bot_run",
                            lambda argv, token: type("R", (), {"returncode": 0, "stderr": ""})())
        monkeypatch.setattr(executor, "_latest_bot_review_url", lambda n: None)
        res = executor.submit_review(5, "request-changes", "fix the sentinel",
                                     token="tok", dry_run=False, override_stale=True)
        assert res["status"] == "executed"
        assert any(e.get("stale_override") for e in events)

    def test_a_clean_post_is_not_logged_as_an_override(self, monkeypatch):
        events: list = []
        _setup(monkeypatch, live=_OPEN_SAME, events=events)
        monkeypatch.setattr(executor, "bot_run",
                            lambda argv, token: type("R", (), {"returncode": 0, "stderr": ""})())
        monkeypatch.setattr(executor, "_latest_bot_review_url", lambda n: None)
        executor.submit_review(5, "request-changes", "ok", token="tok", dry_run=False,
                               override_stale=True)
        assert not any(e.get("stale_override") for e in events)

    def test_a_merged_pr_refuses_even_with_the_override(self, monkeypatch):
        # the override covers stale evidence, never a PR that is gone.
        _setup(monkeypatch, live=_MERGED)
        res = executor.submit_review(5, "request-changes", "fix it", token="tok",
                                     dry_run=False, override_stale=True)
        assert res["status"] == "skipped" and "merged" in res["detail"]

    def test_unreachable_github_still_posts(self, monkeypatch):
        # fail-open: a transient read outage must not wedge commenting.
        _setup(monkeypatch, live=None)
        monkeypatch.setattr(executor, "bot_run",
                            lambda argv, token: type("R", (), {"returncode": 0, "stderr": ""})())
        monkeypatch.setattr(executor, "_latest_bot_review_url", lambda n: None)
        res = executor.submit_review(5, "request-changes", "fix it", token="tok", dry_run=False)
        assert res["status"] == "executed"


class TestEveryEvidenceQuotingWriteIsGated:
    def test_close_blocks(self, monkeypatch):
        _setup(monkeypatch, live=_OPEN_MOVED)
        res = executor.execute_pr(5, models.CloseAction(action="CLOSE_STALE", comment="going stale"),
                                  token="tok", dry_run=False)
        assert res["status"] == "stale"

    def test_close_override_proceeds(self, monkeypatch):
        _setup(monkeypatch, live=_OPEN_MOVED)
        called: list = []
        monkeypatch.setattr(executor, "_comment_then_close",
                            lambda n, **k: called.append(n) or {"status": "executed"})
        res = executor.execute_pr(
            5, models.CloseAction(action="CLOSE_STALE", comment="going stale", override_stale=True),
            token="tok", dry_run=False)
        assert called == [5] and res["status"] == "executed"

    def test_line_comment_blocks(self, monkeypatch):
        _setup(monkeypatch, live=_OPEN_MOVED)
        res = executor.comment_line(5, "src/a.ts", 12, "this line is wrong",
                                    token="tok", dry_run=False)
        assert res["status"] == "stale"

    def test_greptile_retrigger_is_not_gated(self, monkeypatch):
        # a re-review targets the current head, so it is the remedy for a stale
        # score rather than a consumer of one — gating it would block the fix.
        _setup(monkeypatch, live=_OPEN_MOVED)
        monkeypatch.setattr(
            executor, "bot_run",
            lambda argv, token: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())
        res = executor.retrigger_greptile(5, token="tok", dry_run=False)
        assert res["status"] == "executed"


class TestObservationIsRecorded:
    def test_the_moved_head_is_written_so_every_reader_sees_it(self, monkeypatch):
        # the preflight is another live observer: what it learns feeds the same
        # channel the sweep uses, so the block is not private to this request.
        seen: list = []
        monkeypatch.setattr(data, "prs", lambda: {5: _rec()})
        monkeypatch.setattr(data, "pr_to_clusters", lambda: {5: [1]})
        monkeypatch.setattr(executor, "_pr_live", lambda n: _OPEN_MOVED)
        monkeypatch.setattr(executor, "_reflect_state", lambda n, **k: None)
        monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
        monkeypatch.setattr(executor, "_record_observed_head",
                            lambda n, head: seen.append((n, head)))
        executor.submit_review(5, "request-changes", "fix it", token="tok", dry_run=False)
        assert seen == [(5, NEWHEAD)]
