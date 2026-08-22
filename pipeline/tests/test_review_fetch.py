from pipeline import review_fetch

NODE = {"number": 11858, "headRefOid": "cb7342d3", "updatedAt": "2026-08-21T15:21:03Z",
        "reviews": {"nodes": [{"databaseId": 1, "author": {"login": "superagent-security", "__typename": "Bot"},
                               "state": "COMMENTED", "commit": {"oid": "cb7342d3"},
                               "body": "Superagent found 2 security concern(s).",
                               "submittedAt": "2026-08-21T15:21:03Z", "url": "r"}]},
        "reviewThreads": {"nodes": [{"isResolved": False, "isOutdated": False, "comments": {"nodes": [{
            "databaseId": 2, "author": {"login": "superagent-security"}, "body": "**P1:** x", "path": "m.ts",
            "line": 28, "originalLine": 28, "commit": {"oid": "cb7342d3"}, "originalCommit": {"oid": "cb7342d3"},
            "createdAt": "2026-08-21T15:21:03Z", "updatedAt": "2026-08-21T15:21:03Z", "url": "t"}]}}]},
        "comments": {"nodes": [{"databaseId": 3, "author": {"login": "greptile-apps", "__typename": "Bot"},
                                "body": "Confidence Score: 4/5", "createdAt": "2026-08-21T00:00:00Z",
                                "updatedAt": "2026-08-21T00:00:00Z", "url": "c"}]},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"contexts": {"nodes": [
            {"__typename": "CheckRun", "name": "Greptile Review", "status": "COMPLETED", "conclusion": "FAILURE",
             "title": "Confidence 4/5 — below your required 5/5", "summary": "s", "detailsUrl": "d", "url": "u",
             "checkSuite": {"app": {"slug": "greptile-apps"}}}]}}}}]}}


def test_feed_from_node():
    f = review_fetch.feed_from_node(11858, NODE)
    assert f.head_sha == "cb7342d3" and f.updated_at == "2026-08-21T15:21:03Z" and f.conversation
    assert f.reviews[0]["login"] == "superagent-security" and f.reviews[0]["commit"] == "cb7342d3"
    assert f.threads[0]["resolved"] is False and f.threads[0]["original_commit"] == "cb7342d3"
    assert f.comments[0]["body"] == "Confidence Score: 4/5"
    assert f.check_runs[0]["app"] == "greptile-apps" and f.check_runs[0]["conclusion"] == "failure"


def test_fetch_feeds_chunks_and_aliases(monkeypatch):
    calls: list[str] = []

    def fake(query, **kw):
        calls.append(query)
        numbers = [n for n in range(1, 12) if f"pullRequest(number: {n})" in query]
        return {"data": {"repository": {f"p{i}": dict(NODE, number=n) for i, n in enumerate(numbers)}}}
    monkeypatch.setattr(review_fetch, "gh_graphql", fake)
    monkeypatch.setattr(review_fetch.settings, "repo_owner", lambda: "o")
    monkeypatch.setattr(review_fetch.settings, "repo_name", lambda: "r")
    out = review_fetch.fetch_feeds(list(range(1, 12)))
    assert len(calls) == 2 and set(out) == set(range(1, 12))
    assert out[11].pr == 11


def test_fetch_feeds_tolerates_a_failed_chunk(monkeypatch):
    monkeypatch.setattr(review_fetch, "gh_graphql", lambda q, **kw: None)
    monkeypatch.setattr(review_fetch.settings, "repo_owner", lambda: "o")
    monkeypatch.setattr(review_fetch.settings, "repo_name", lambda: "r")
    assert review_fetch.fetch_feeds([1, 2]) == {}
