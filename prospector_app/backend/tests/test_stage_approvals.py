"""POST /api/prs/{n}/approve: an operator approves a YELLOW security verdict or
an escalated verify outcome past what holds the PR, and the pipeline moves on;
a RED verdict is never approvable."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from pipeline import gates
from pipeline import store as S
from pipeline.storekit import now as _now
from pipeline.testsupport import reviews_section, threat_section
from prospector_app.backend import activity, data, pr_checks
from prospector_app.backend import app as appmod

HEAD = "abc123def456" + "0" * 28


def _save(st: S.Store, *, verdict: str = "YELLOW", outcome: str | None = None,
          head: str = HEAD) -> None:
    rec: dict = {
        "pr": 5,
        "meta": {"title": "PR 5", "author": "al", "author_association": "CONTRIBUTOR",
                 "state": "open", "head_sha": HEAD, "updated_at": _now()},
        "signals": {"ci": "passing", "mergeable": True, "checked_at": _now(),
                    "against_head_sha": HEAD},
        "threat": threat_section(HEAD),
        "reviews": reviews_section(HEAD),
        "security": {"verdict": verdict, "against_head_sha": head, "checked_at": _now(),
                     "findings": [{"severity": "yellow", "title": "logs a token"}]}}
    if outcome is not None:
        rec["verify"] = {"outcome": outcome, "against_head_sha": HEAD, "against_base_sha": "b" * 40,
                         "tier": 1, "checked_at": _now(),
                         "signals": {}, "findings": []}
    st.save_pr(rec)
    data.refresh()


@pytest.fixture
def st(tmp_path, monkeypatch) -> S.Store:
    store = S.Store(tmp_path / "store")
    monkeypatch.setattr(data, "_store", store)
    monkeypatch.setattr(activity, "operator",
                        lambda: {"name": "Alex Example", "email": None, "slug": "alex"})
    return store


@pytest.fixture
def logged(monkeypatch) -> list[dict]:
    events: list[dict] = []
    monkeypatch.setattr(activity, "record", lambda kind, **f: events.append({"kind": kind, **f}))
    return events


@pytest.fixture
def client() -> TestClient:
    return TestClient(appmod.app, raise_server_exceptions=False)


def _approve(client: TestClient, stage: str, head: str = HEAD, reason: str = "token is a test fixture"):
    return client.post("/api/prs/5/approve", json={"stage": stage, "head_sha": head, "reason": reason})


def _row(st: S.Store, key: str) -> dict:
    pr = st.load_pr(5)
    assert pr is not None
    return next(c for c in pr_checks.checks_for_record(pr)["checks"] if c["key"] == key)


def test_an_approved_yellow_clears_security_for_the_pipeline(st, client, logged):
    _save(st)
    assert _row(st, "security")["approve_head"] == HEAD
    assert _approve(client, "security").status_code == 200
    pr = st.load_pr(5)
    assert pr is not None and pr.security_override is not None
    assert pr.security_override["by"] == "Alex Example"
    assert gates.security_cleared(pr) and gates.security_disposition(pr) is None
    assert gates.security_merge_block(pr) is None
    row = _row(st, "security")
    assert row["status"] == "pass" and row["approval"]["reason"] == "token is a test fixture"
    assert row["approve_head"] is None and "approved by Alex Example" in row["detail"]
    assert logged[-1]["kind"] == "security-approve" and not activity.is_landed(logged[-1])


@pytest.mark.parametrize("verdict, words", [("RED", "never approvable"), ("GREEN", "no longer holds")])
def test_only_a_yellow_verdict_is_approvable(st, client, verdict, words):
    _save(st, verdict=verdict)
    assert _row(st, "security")["approve_head"] is None
    r = _approve(client, "security")
    assert r.status_code == 409 and words in r.json()["detail"]
    pr = st.load_pr(5)
    assert pr is not None and pr.security_override is None


def test_a_verdict_of_an_earlier_head_is_not_approvable(st, client):
    _save(st, head="e" * 40)
    assert _approve(client, "security", head="e" * 40).status_code == 409


def test_an_approved_escalate_clears_verify(st, client, logged):
    _save(st, verdict="GREEN", outcome="escalate")
    assert _row(st, "verify")["status"] == "fail" and _row(st, "verify")["approve_head"] == HEAD
    assert _approve(client, "verify", reason="the lane failed on infra").status_code == 200
    pr = st.load_pr(5)
    assert pr is not None and pr.verify_override is not None
    assert gates.verify_disposition(pr) is None
    assert _row(st, "verify")["status"] == "pass"
    assert logged[-1]["kind"] == "verify-approve"


def test_a_concluded_verify_outcome_is_not_approvable(st, client):
    _save(st, verdict="GREEN", outcome="not-verified")
    assert _approve(client, "verify").status_code == 409


def test_an_unknown_stage_is_refused(st, client):
    _save(st)
    r = _approve(client, "ci")
    assert r.status_code == 409 and "not a stage" in r.json()["detail"]
