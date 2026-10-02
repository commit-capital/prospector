"""threat_view: what the banner and the Threats view say about flagged PRs,
blocked actors, and credentials to rotate."""
from __future__ import annotations

from pipeline.model import Pr
from prospector_app.backend import threat_view

HEAD = "a" * 40


def _pr(n: int, *, verdict: str | None = None, state: str = "open",
        signatures: list[str] | None = None, author: str = "mallory",
        head: str = HEAD) -> Pr:
    rec: dict = {"pr": n,
                 "meta": {"title": f"PR {n}", "author": author, "state": state,
                          "head_sha": HEAD, "url": f"https://github.com/o/r/pull/{n}"}}
    if verdict is not None:
        rec["threat"] = {"verdict": verdict, "signatures": signatures or [], "detail": {},
                         "checked_at": "2026-10-01T00:00:00+00:00", "against_head_sha": head}
    return Pr(None, rec)


def _secret(n: int, *, status: str = "open", fixture: bool = False) -> dict:
    return {"id": f"rotate-secret:{n}", "kind": "rotate-secret", "pr": n, "status": status,
            "summary": f"Potential secret leaked in PR #{n}", "fixture": fixture}


def test_the_summary_lists_open_malicious_prs_with_their_first_detection():
    prs = [_pr(11987, verdict="malicious", signatures=["obfuscated-payload"]),
           _pr(12035, verdict="malicious", state="closed"),
           _pr(3, verdict="clear")]
    incidents = [{"pr": 11987, "noticed": "2026-09-30"}, {"pr": 12035, "noticed": "2026-09-28"}]
    out = threat_view.summarize(prs, incidents, [])
    assert [m["pr"] for m in out["malicious"]] == [11987]
    first = out["malicious"][0]
    assert first["signatures"] == ["obfuscated-payload"]
    assert first["noticed"] == "2026-09-30"
    assert first["author"] == "mallory"


def test_the_summary_counts_open_suspicious_prs():
    prs = [_pr(1, verdict="suspicious"), _pr(2, verdict="suspicious", state="merged"),
           _pr(3, verdict="clear")]
    assert threat_view.summarize(prs, [], [])["suspicious"] == 1


def test_the_summary_counts_open_live_looking_secrets_only():
    items = [_secret(1), _secret(2, status="dismissed"), _secret(3, fixture=True),
             {"id": "salvage-fix:4", "kind": "salvage-fix", "pr": 4, "status": "open"}]
    assert threat_view.summarize([], [], items)["secrets"] == 1


def test_a_quiet_store_reads_as_nothing_to_raise():
    out = threat_view.summarize([_pr(1, verdict="clear"), _pr(2)], [], [])
    assert out == {"malicious": [], "suspicious": 0, "secrets": 0}


def test_the_detail_lists_flagged_prs_incidents_actors_and_secrets():
    prs = [_pr(11987, verdict="malicious", signatures=["obfuscated-payload"]),
           _pr(12035, verdict="malicious", state="closed"),
           _pr(5, verdict="suspicious", signatures=["eol-churn"], author="bob"),
           _pr(6, verdict="clear", author="alice")]
    registry = {"actors": {"mallory": {"reason": "Malicious PR(s): obfuscated-payload",
                                       "added": "2026-09-28", "incidents": [11987, 12035]}},
                "incidents": [{"pr": 12035, "author": "mallory", "head_sha": "x",
                               "signatures": ["blocked-actor"], "noticed": "2026-09-28"},
                              {"pr": 11987, "author": "mallory", "head_sha": HEAD,
                               "signatures": ["obfuscated-payload"], "noticed": "2026-09-30"}]}
    out = threat_view.detail(prs, registry, [_secret(5), _secret(7, status="done")])
    assert [(f["pr"], f["verdict"]) for f in out["flagged"]] == [(11987, "malicious"),
                                                                 (5, "suspicious")]
    assert [(i["pr"], i["state"]) for i in out["incidents"]] == [(11987, "open"),
                                                                 (12035, "closed")]
    assert out["actors"] == [{"login": "mallory", "reason": "Malicious PR(s): obfuscated-payload",
                              "added": "2026-09-28", "incidents": [11987, 12035],
                              "open_prs": [11987]}]
    assert [s["pr"] for s in out["secrets"]] == [5]


def test_an_unmarked_secret_whose_evidence_reads_as_a_fixture_is_not_counted():
    item = {"id": "rotate-secret:9", "kind": "rotate-secret", "pr": 9, "status": "open",
            "evidence": "tests/fixtures/keys.ts: const key = 'sk_live_dummy'"}
    assert threat_view.summarize([], [], [item])["secrets"] == 0


def test_the_threats_route_serves_the_detail(monkeypatch):
    from fastapi.testclient import TestClient

    from prospector_app.backend import app as appmod
    from prospector_app.backend import data

    class _Store:
        def load_threats(self) -> dict:
            return {"actors": {}, "incidents": [{"pr": 11987, "author": "mallory",
                                                 "signatures": ["x"], "noticed": "2026-09-30"}]}

    monkeypatch.setattr(data, "snapshot_loading", lambda: False)
    monkeypatch.setattr(data, "prs", lambda: {11987: _pr(11987, verdict="malicious")})
    monkeypatch.setattr(data, "store", lambda: _Store())
    monkeypatch.setattr(data, "action_items", lambda: [])
    r = TestClient(appmod.app).get("/api/threats")
    assert r.status_code == 200
    body = r.json()
    assert [f["pr"] for f in body["flagged"]] == [11987]
    assert body["incidents"][0]["state"] == "open" and body["loading"] is False


def test_nothing_is_summarized_while_the_snapshot_loads(monkeypatch):
    import pytest

    from prospector_app.backend import data
    monkeypatch.setattr(data, "snapshot_loading", lambda: True)
    monkeypatch.setattr(data, "prs", lambda: pytest.fail("held a request on the cold load"))
    assert threat_view.summary() is None


def test_the_threats_route_answers_loading_while_the_snapshot_loads(monkeypatch):
    import pytest
    from fastapi.testclient import TestClient

    from prospector_app.backend import app as appmod
    from prospector_app.backend import data
    monkeypatch.setattr(data, "snapshot_loading", lambda: True)
    monkeypatch.setattr(data, "prs", lambda: pytest.fail("held a request on the cold load"))
    body = TestClient(appmod.app).get("/api/threats").json()
    assert body["loading"] is True and body["flagged"] == []


def test_the_detail_marks_fixture_secrets_and_lists_live_ones_first():
    fixture = {"id": "rotate-secret:3", "kind": "rotate-secret", "pr": 3, "status": "open",
               "evidence": "tests/fixtures/keys.ts: const key = 'dummy'"}
    live = {"id": "rotate-secret:9", "kind": "rotate-secret", "pr": 9, "status": "open",
            "evidence": ".env.prod: SECRET=abc123def456"}
    out = threat_view.detail([], {}, [fixture, live])
    assert [(s["pr"], s["fixture"]) for s in out["secrets"]] == [(9, False), (3, True)]
