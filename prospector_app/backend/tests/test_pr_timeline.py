"""The PR timeline: GitHub's history with Prospector's own writes tagged from
the activity log, and one Prospector summary after each commit."""
from pipeline import model
from pipeline import storekit
from pipeline.testsupport import reviews_section
from prospector_app.backend import pr_history

OLD, HEAD = "aaa111", "bbb222"
NOW = "2026-06-10T00:00:00+00:00"


def _it(kind, at, actor=None, url=None, sha=None):
    return pr_history._item(kind, at, actor, None, url, sha=sha)


def _ev(kind, at, **kw):
    return {"kind": kind, "at": at, "verb": kw.pop("verb", kind), "operator": "Brandon Burr", **kw}


def test_event_url_match_tags_that_entry():
    items = [_it("comment", "2026-01-01T00:00:00Z", "test-bot", url="https://x/c1"),
             _it("comment", "2026-01-01T00:00:30Z", "test-bot", url="https://x/c2")]
    out = pr_history.tag_prospector(items, [_ev("review-retrigger", "2026-01-01T00:00:31Z",
                                                event_url="https://x/c1", verb="re-triggered Greptile")])
    assert out[0]["via"] and out[0]["via"]["verb"] == "re-triggered Greptile"
    assert out[0]["via"]["operator"] == "Brandon Burr"
    assert out[1]["via"] is None
    assert len(out) == 2


def test_close_tags_its_comment_and_the_close_by_the_bot_nearby():
    items = [_it("comment", "2026-01-01T00:00:00Z", "alice"),
             _it("comment", "2026-01-01T00:00:01Z", "test-bot[bot]"),
             _it("closed", "2026-01-01T00:00:02Z", "test-bot")]
    out = pr_history.tag_prospector(items, [_ev("close", "2026-01-01T00:00:05Z", reason="duplicate")])
    assert [it["via"] is not None for it in out] == [False, True, True]
    assert out[2]["via"] and out[2]["via"]["reason"] == "duplicate"


def test_no_match_outside_the_window_or_by_another_author():
    items = [_it("comment", "2026-01-01T00:00:00Z", "alice"),
             _it("comment", "2026-01-01T01:00:00Z", "test-bot")]
    out = pr_history.tag_prospector(items, [_ev("comment", "2026-01-01T00:00:10Z")])
    assert all(it["via"] is None for it in out if it["kind"] == "comment")
    assert out[1]["kind"] == "prospector_action"


def test_push_matches_its_head_commit_by_sha():
    items = [_it("commit", "2026-01-01T00:00:00Z", "Michel", sha=OLD),
             _it("commit", "2026-01-01T00:00:00Z", "Michel", sha=HEAD)]
    out = pr_history.tag_prospector(items, [_ev("resubmit", "2026-01-02T00:00:00Z",
                                                to_sha=HEAD, verb="pushed a fix")])
    assert out[0]["via"] is None and out[1]["via"] and out[1]["via"]["verb"] == "pushed a fix"


def test_event_github_does_not_show_gets_its_own_row_in_order():
    items = [_it("comment", "2026-01-01T00:00:00Z", "alice"),
             _it("comment", "2026-01-03T00:00:00Z", "alice")]
    out = pr_history.tag_prospector(items, [_ev("pr-edit", "2026-01-02T00:00:00Z",
                                                verb="rewrote the description")])
    assert [it["kind"] for it in out] == ["comment", "prospector_action", "comment"]
    assert out[1]["actor"] == "test-bot"


def test_one_entry_is_tagged_once():
    items = [_it("comment", "2026-01-01T00:00:00Z", "test-bot")]
    out = pr_history.tag_prospector(items, [_ev("comment", "2026-01-01T00:00:01Z"),
                                            _ev("comment", "2026-01-01T00:00:02Z")])
    assert [it["kind"] for it in out] == ["comment", "prospector_action"]


def _pr(**over):
    rec = {
        "pr": 7,
        "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                 "head_sha": HEAD, "checked_at": NOW},
        "signals": {"ci": "passing", "mergeable": True, "has_tests": True,
                    "checked_at": NOW, "against_head_sha": HEAD},
        "reviews": reviews_section(HEAD, NOW),
    }
    rec.update(over)
    return model.Pr(None, rec)


def _run(phase, head, **stats):
    return storekit.PhaseRun(phase, NOW, NOW, {"phase": phase, "pr": 7, "stats": {"head_sha": head, **stats}})


def _history():
    return [_it("commit", "2026-01-01T00:00:00Z", "Michel", sha=OLD),
            _it("comment", "2026-01-01T01:00:00Z", "alice"),
            _it("commit", "2026-01-02T00:00:00Z", "Michel", sha=HEAD),
            _it("merged", "2026-01-03T00:00:00Z", "test-bot")]


def test_summary_rows_sit_after_each_commit_stretch():
    rec = _pr(security={"verdict": "YELLOW", "findings": [{"severity": "high"}],
                        "checked_at": NOW, "against_head_sha": OLD})
    out = pr_history.commit_summaries(_history(), rec, [])
    assert [it["kind"] for it in out] == ["commit", "comment", "prospector", "commit", "prospector", "merged"]
    old, head = out[2], out[4]
    assert old["kind"] == "prospector" and head["kind"] == "prospector"
    assert old["sha"] == OLD and not old["head"]
    assert {"label": "Security review YELLOW · 1 finding(s)", "tone": "warn"} in old["facts"]
    assert "security review" not in old["unrecorded"] and "CI" in old["unrecorded"]
    assert head["head"] and {"label": "CI passing", "tone": "pass"} in head["facts"]
    assert head["unrecorded"] == []


def test_ledger_rows_naming_a_head_land_on_that_commit():
    runs = [_run("fix:single", OLD, status="pushed", action="rebase"),
            _run("design-review:run", OLD, findings={"high": 2, "low": 0}),
            _run("security:review-one", None, verdict="GREEN")]
    out = pr_history.commit_summaries(_history(), _pr(), runs)
    old = out[2]
    assert old["kind"] == "prospector"
    assert [f["label"] for f in old["facts"]] == ["Fix (rebase): pushed", "Design review: 2 high"]


def test_head_summary_states_core_checks_and_only_failing_others():
    rec = _pr(signals={"ci": "failing", "mergeable": False, "has_tests": True,
                       "checked_at": NOW, "against_head_sha": HEAD})
    head = pr_history.commit_summaries(_history(), rec, [])[3]
    assert head["kind"] == "prospector"
    labels = [f["label"] for f in head["facts"]]
    assert "CI failing" in labels and "Merge conflicts" in labels
    assert not any("tests" in label.lower() for label in labels)


def test_an_older_commit_with_nothing_recorded_gets_no_row():
    out = pr_history.commit_summaries(_history(), _pr(), [])
    assert [it["kind"] for it in out] == ["commit", "comment", "commit", "prospector", "merged"]


def test_no_summaries_without_a_store_record():
    assert pr_history.commit_summaries(_history(), None, []) == _history()
