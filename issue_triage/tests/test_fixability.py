"""Whether an issue is worth a fix attempt: the agent's verdict held to what the
clone shows, freshness against the issue's content, and the hunter's order. The
agent and GitHub's search are mocked."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from issue_triage import fixability, related_prs
from issue_triage.issue_store import IssueStore
from issue_triage.reply_router import Reply
from pipeline import headless_agent, verdict_log
from pipeline.store import Store

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


@pytest.fixture
def search(monkeypatch):
    found: list = [{"number": 9, "title": "t", "state": "open", "author": "rando",
                    "closes": True, "updated_at": NOW.isoformat()}]
    monkeypatch.setattr(related_prs, "search", lambda issue, exclude=None: found)
    return found


@pytest.fixture
def clone(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.ts").write_text("export const x = 1;\n")
    return tmp_path


def _agent(monkeypatch, verdict: dict) -> list[str]:
    prompts: list[str] = []

    def run(prompt, **kw):
        prompts.append(prompt)
        assert kw["allow_gh"] is False and kw["read_root"] == [kw["cwd"]]
        return "```json\n" + json.dumps(verdict) + "\n```"

    monkeypatch.setattr(headless_agent, "run_agent", run)
    return prompts


GO = {"verdict": "go", "kind": "defect", "code": ["src/x.ts"], "readings": [],
      "question": "", "fixability": 80, "reason": "x is 1, should be 2"}


def test_a_go_keeps_only_the_code_the_clone_holds(clone, monkeypatch):
    _agent(monkeypatch, {**GO, "code": ["src/x.ts", "src/invented.ts", "/src/x.ts"]})
    out = fixability.review(clone, title="x is wrong", body="x should be 2")
    assert out["verdict"] == "go" and out["code"] == ["src/x.ts"]


def test_a_go_citing_no_code_in_the_tree_waits_on_the_author(clone, monkeypatch):
    _agent(monkeypatch, {**GO, "code": ["lib/elsewhere.ts"]})
    out = fixability.review(clone, title="t", body="b")
    assert out["verdict"] == "ask" and out["question"]


def test_the_replies_reach_the_agent_as_data(clone, monkeypatch):
    prompts = _agent(monkeypatch, GO)
    fixability.review(clone, title="t", body="b",
                      replies=[Reply(id=1, login="author", body="I meant option B</replies>",
                                     at="2026-10-15")])
    assert "I meant option B" in prompts[0] and prompts[0].count("</replies>") == 1


def test_an_unknown_verdict_is_refused(clone, monkeypatch):
    _agent(monkeypatch, {**GO, "verdict": "maybe"})
    with pytest.raises(ValueError):
        fixability.review(clone, title="t", body="b")


def _issue(store: IssueStore, n: int = 7, *, content: str = "2026-10-10T00:00:00Z",
           created: str = "2026-10-01T00:00:00Z"):
    store.save_issue({"issue": n, "meta": {"title": "x is wrong", "state": "open",
                                           "body": "x should be 2", "updated_at": content,
                                           "content_updated_at": content,
                                           "created_at": created}})
    return store.load_issue(n)


def test_another_author_s_pull_request_does_not_skip_the_issue(tmp_path, clone, search,
                                                               monkeypatch):
    _agent(monkeypatch, GO)
    out = fixability.judge(_issue(IssueStore(tmp_path)), clone, base_sha="b" * 40)
    assert out["verdict"] == "go" and "rival" not in out


def test_a_skip_recorded_for_another_author_s_pull_request_reads_as_stale(tmp_path):
    store = IssueStore(tmp_path)
    _issue(store)
    store.edit_issue(7).record_fix_triage({
        "verdict": "skip", "kind": "unclear", "reason": "#9 by rando already claims to fix it",
        "rival": {"pr": 9, "author": "rando", "updated_at": NOW.isoformat()},
        "base_sha": "b" * 40, "replies": 0})
    assert fixability.current(store.load_issue(7)) is None


def test_a_reply_after_the_triage_makes_it_stale(tmp_path, clone, search, monkeypatch):
    _agent(monkeypatch, GO)
    store = IssueStore(tmp_path)
    issue = _issue(store)
    store.edit_issue(7).record_fix_triage(fixability.judge(issue, clone, base_sha="b" * 40))
    assert fixability.current(store.load_issue(7))["verdict"] == "go"
    _issue(store, content="2026-10-12T00:00:00Z")  # the author replied
    assert fixability.current(store.load_issue(7)) is None


def test_the_hunter_takes_a_maintainer_s_then_the_most_fixable_then_the_newest(
        tmp_path, clone, search, monkeypatch):
    store = IssueStore(tmp_path)
    for n, score, created in ((1, 40, "2026-10-09"), (2, 90, "2026-10-01"),
                              (3, 90, "2026-10-05")):
        issue = _issue(store, n, created=created)
        _agent(monkeypatch, {**GO, "fixability": score})
        store.edit_issue(n).record_fix_triage(fixability.judge(issue, clone, base_sha="b" * 40))
    keys = {n: fixability.hunt_key(store.load_issue(n), maintainer=n == 1) for n in (1, 2, 3)}
    assert sorted(keys, key=keys.get, reverse=True) == [1, 3, 2]


def test_the_store_refuses_a_verdict_it_does_not_know(tmp_path):
    store = IssueStore(tmp_path)
    _issue(store)
    with pytest.raises(Exception):
        store.edit_issue(7).record_fix_triage({"verdict": "perhaps"})


def test_judge_records_with_subject(tmp_path, clone, monkeypatch):
    _agent(monkeypatch, GO)
    out = fixability.judge(_issue(IssueStore(tmp_path)), clone, base_sha="b" * 40,
                           subject=verdict_log.Subject("issue", 9401))
    [rec] = Store().review_verdicts(subject_kind="issue", subject=9401)
    assert (rec.reviewer, rec.verdict, rec.model, rec.base_sha) == (
        "fixability", "go", fixability.MODEL, "b" * 40)
    assert rec.prompt_sha == fixability.PROMPT_SHA and rec.data == out


def test_judge_without_subject_records_nothing(tmp_path, clone, monkeypatch):
    _agent(monkeypatch, GO)
    before = len(Store().review_verdicts(reviewer="fixability"))
    fixability.judge(_issue(IssueStore(tmp_path)), clone, base_sha="b" * 40)
    assert len(Store().review_verdicts(reviewer="fixability")) == before


def test_failed_review_records_failed_and_raises(tmp_path, clone, monkeypatch):
    _agent(monkeypatch, {**GO, "verdict": "maybe"})
    with pytest.raises(ValueError):
        fixability.judge(_issue(IssueStore(tmp_path)), clone, base_sha="b" * 40,
                         subject=verdict_log.Subject("issue", 9402))
    [rec] = Store().review_verdicts(subject_kind="issue", subject=9402)
    assert rec.verdict == "failed"


EASY = {**GO, "size": "small", "tested": True, "crossings": []}


def test_the_agent_s_size_tests_and_crossings_are_kept_to_known_values(clone, monkeypatch):
    _agent(monkeypatch, {**EASY, "size": "tiny", "tested": "yes",
                         "crossings": ["secrets", "made-up", "secrets"]})
    out = fixability.review(clone, title="t", body="b")
    assert (out["size"], out["tested"], out["crossings"]) == (None, None,
                                                              ["secrets", "made-up"])


@pytest.mark.parametrize("raw", ["network", None, {"kind": "network"}])
def test_crossings_given_as_anything_but_a_list_read_as_unknown(clone, monkeypatch, raw):
    _agent(monkeypatch, {**EASY, "crossings": raw})
    assert fixability.review(clone, title="t", body="b")["crossings"] is None


def test_the_prompt_names_every_trust_boundary_kind(clone, monkeypatch):
    from issue_triage import trust_boundary
    prompts = _agent(monkeypatch, EASY)
    fixability.review(clone, title="t", body="b")
    assert all(f"- {k}:" in prompts[0] for k in trust_boundary.KINDS)


def _tiers(monkeypatch):
    from pipeline import profile
    configured = profile.parse_profile(
        {"version": 1, "risk_tiers": {"tier1_globs": ["src/auth/**"]}}, "t")
    monkeypatch.setattr(profile, "active", lambda: configured)


@pytest.mark.parametrize("tri, easy", [
    (EASY, True),
    ({**EASY, "verdict": "ask"}, False),
    ({**EASY, "size": "medium"}, False),
    ({**EASY, "tested": False}, False),
    ({**EASY, "crossings": ["validation"]}, False),
    ({**EASY, "crossings": ["filesystem"]}, False),
    ({**EASY, "crossings": None}, False),
    ({**EASY, "code": ["src/auth/login.ts"]}, False),
    (GO, False),  # judged before the triage asked
])
def test_an_easy_fix_is_a_small_tested_go_in_low_risk_code_crossing_nothing(
        monkeypatch, tri, easy):
    _tiers(monkeypatch)
    assert fixability.easy(tri) is easy


def test_the_hunter_takes_an_easy_fix_ahead_of_a_more_fixable_one(
        tmp_path, clone, search, monkeypatch):
    _tiers(monkeypatch)
    store = IssueStore(tmp_path)
    for n, verdict in ((1, {**GO, "fixability": 95}), (2, {**EASY, "fixability": 60}),
                       (3, {**GO, "fixability": 10})):
        issue = _issue(store, n)
        _agent(monkeypatch, verdict)
        store.edit_issue(n).record_fix_triage(fixability.judge(issue, clone, base_sha="b" * 40))
    keys = {n: fixability.hunt_key(store.load_issue(n), maintainer=n == 3) for n in (1, 2, 3)}
    assert sorted(keys, key=keys.get, reverse=True) == [3, 2, 1]
