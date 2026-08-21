"""The capabilities `reviewers` descriptor drives the frontend's per-reviewer UI.
It follows the configured review policy."""
from pipeline import review_policy
from prospector_app.backend import caps


def test_reviewers_descriptor_explicit_greptile(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    monkeypatch.delenv("TRIAGE_REVIEW_THRESHOLD", raising=False)
    review_policy.reset()
    caps.refresh()
    d = {x["id"]: x for x in caps.capabilities()["reviewers"]}
    assert d["greptile"]["active"] is True and d["greptile"]["retrigger"] is True
    assert d["greptile"]["threshold"] == 5 and d["greptile"]["score_max"] == 5
    assert d["coderabbit"]["active"] is False and d["coderabbit"]["kind"] == "review"
    assert d["superagent"]["kind"] == "scanner" and d["superagent"]["retrigger"] is False
    assert "review" not in caps.capabilities()


def test_reviewers_descriptor_none(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    review_policy.reset()
    caps.refresh()
    assert all(x["active"] is False for x in caps.capabilities()["reviewers"])


def test_capabilities_route_exposes_reviewers(monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    review_policy.reset()
    caps.refresh()
    r = TestClient(appmod.app).get("/api/capabilities")
    assert r.status_code == 200
    ids = [x["id"] for x in r.json()["reviewers"]]
    assert ids == ["greptile", "coderabbit", "superagent", "socket"]
