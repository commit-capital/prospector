from copy import deepcopy

import pytest

from pipeline import ci_signal, gh, live_prs, review_fetch


def _page(nodes: list[dict], cursor: str | None = None) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}}


def _run(name: str) -> dict:
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
            "conclusion": "SUCCESS", "checkSuite": {"app": {"slug": "github-actions"}}}


def _ref() -> dict:
    return {"branchProtectionRule": {"requiredStatusCheckContexts": ["required-build"]},
            "rules": _page([])}


@pytest.mark.parametrize("ref", [None, {}, {"branchProtectionRule": None, "rules": None},
                               {"branchProtectionRule": None, "rules": _page([], "next")}])
def test_incomplete_requirements_are_unknown(monkeypatch, ref):
    monkeypatch.setattr(gh, "_graphql", lambda *a: ({"data": {"repository": {"ref": ref}}}, ""))
    assert gh.required_checks("main", {}) is None


@pytest.mark.parametrize("payload", [None, {"data": {"repository": {"ref": _ref()}},
                                         "errors": [{"message": "Resource not accessible"}]}])
def test_failed_requirements_are_unknown_and_cached(monkeypatch, payload):
    calls = []

    def query(*args):
        calls.append(args)
        return payload, "unavailable"

    monkeypatch.setattr(gh, "_graphql", query)
    known = {}
    assert gh.required_checks("main", known) is None
    assert gh.required_checks("main", known) is None
    assert len(calls) == 1


def test_unprotected_branch_has_no_requirements(monkeypatch):
    ref = {"branchProtectionRule": None, "rules": _page([])}
    monkeypatch.setattr(gh, "_graphql", lambda *a: ({"data": {"repository": {"ref": ref}}}, ""))
    assert gh.required_checks("main", {}) == []
    assert gh.required_checks(None, {}) is None


@pytest.mark.parametrize("reader", ["live", "feed"])
@pytest.mark.parametrize("last_page", ["success", "failure", "unavailable", "partial", "missing", "repeated"])
def test_both_readers_finish_check_pages_at_the_pinned_head(monkeypatch, reader, last_page):
    node = {"headRefOid": "pinned-head", "baseRefName": "main", "state": "OPEN",
            "mergeable": "MERGEABLE", "additions": 1, "deletions": 0, "changedFiles": 1,
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {
                "contexts": _page([_run(f"job-{i}") for i in range(100)], "page-2")}}}]}}
    calls = []

    def query(query, variables, timeout, waits):
        calls.append((query, variables))
        if "pullRequest(" in query:
            payload = {"data": {"repository": {"p0": deepcopy(node), "p1": deepcopy(node)}}}
        elif "ref(qualifiedName:" in query:
            payload = {"data": {"repository": {"ref": _ref()}}}
        else:
            assert variables["head"] == "pinned-head"
            if variables["cursor"] == "page-2":
                page = _page([_run("required-build")], "page-3")
            else:
                assert variables["cursor"] == "page-3"
                if last_page == "unavailable":
                    return None, "unavailable"
                status = "FAILURE" if last_page == "failure" else "SUCCESS"
                page = _page([{"__typename": "StatusContext", "context": "lint", "state": status}],
                             "page-3" if last_page == "repeated" else None)
                if last_page == "missing":
                    page = None
            payload = {"data": {"repository": {"object": {"statusCheckRollup": {"contexts": page}}}}}
            if variables["cursor"] == "page-3" and last_page == "partial":
                payload["errors"] = [{"message": "unavailable"}]
        return payload, ""

    monkeypatch.setattr(gh, "_graphql", query)
    if reader == "live":
        facts, _ = live_prs.fetch([5, 6])
        verdict = facts[5]["ci"]
        assert facts[5]["unreported"] == []
    else:
        feed = review_fetch.fetch_feeds([5, 6])[5]
        verdict = ci_signal.verdict(feed.check_runs, feed.statuses, required=feed.required)
    assert verdict == {"success": "passing", "failure": "failing"}.get(last_page, "unknown")
    assert sum("ref(qualifiedName:" in q for q, _ in calls) == (last_page in ("success", "failure"))


def test_failed_requirements_replace_passing_during_ingest_and_live_sweep(tmp_path, monkeypatch):
    from pipeline import ingest
    from pipeline.store import Store
    from prospector_app.backend import data, freshness_live

    store = Store(tmp_path)
    raw = {"number": 5, "title": "x", "user": {"login": "a"}, "state": "open",
           "head": {"sha": "h"}, "base": {"ref": "main"}}
    ingest.upsert_pr(store, raw, ci_override="passing")
    ci = ci_signal.verdict([{"app": "github-actions", "name": "build",
                             "status": "completed", "conclusion": "success"}], [], required=None)
    monkeypatch.setattr(data, "store", lambda: store)
    freshness_live.persist_live({5: {"head": "h", "ci": ci}}, {5: store.load_pr(5)})
    assert store.load_pr(5).ci == "unknown"
    ingest.upsert_pr(store, raw, ci_override="passing")
    ingest.upsert_pr(store, raw, ci_override=ci)
    assert store.load_pr(5).ci == "unknown"
