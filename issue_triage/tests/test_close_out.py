"""Closing an issue with no operator: which issues qualify, the evidence each
kind needs, what is recorded and written, and the replies read afterwards."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_triage import close_out, fixability, fixed_test, public_comments
from issue_triage.issue_store import IssueStore
from pipeline import gh, prove, settings

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
BASE = prove.PinnedBase(sha="b" * 40, tier=2, image="img", clone=Path("/clone"))


def _save(store: IssueStore, n: int, *, association: str = "NONE", state: str = "open",
          updated: str = "2026-10-27T00:00:00Z") -> None:
    store.save_issue({"issue": n, "meta": {"title": f"bug {n}", "state": state, "body": "b",
                                           "updated_at": updated, "author": "drive-by",
                                           "author_association": association}})


def _touch(store: IssueStore, n: int, *, updated: str, state: str | None = None) -> None:
    """Issue `n` as a later ingest finds it: updated, its sections kept."""
    rec = store.load_issue(n).rec
    rec["meta"].update({"updated_at": updated, **({"state": state} if state else {})})
    store.save_issue(rec)


def _triage(store: IssueStore, n: int, kind: str = "not-in-repo", verdict: str = "skip") -> None:
    store.edit_issue(n).record_fix_triage({"verdict": verdict, "kind": kind,
                                           "reason": "the module is in another product",
                                           "bar": fixability.BAR})


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "live")
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "all")
    monkeypatch.setattr(settings, "worker_id", lambda: "studio")
    s = IssueStore(tmp_path)
    _save(s, 1)
    _triage(s, 1)
    return s


@pytest.fixture
def writes(monkeypatch):
    from prospector_app.backend import executor

    calls: list[dict] = []
    status = {"value": "executed"}

    def auto_close_issue(n, **kw):
        calls.append({"n": n, **kw})
        return {"status": "dry-run" if kw["dry_run"] else status["value"], "detail": "d"}

    monkeypatch.setattr(executor, "auto_close_issue", auto_close_issue)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: "tok")
    monkeypatch.setattr(prove, "pinned", lambda st: BASE)
    return {"calls": calls, "status": status}


def _evidence(monkeypatch, ok: bool = True) -> list[str]:
    seen: list[str] = []

    def evidence(issue, kind, base):
        seen.append(kind)
        return {"ok": ok, "why": "agrees" if ok else "the second read finds a defect",
                "tests": ["test/x.test.ts"] if kind == "already-fixed" else [],
                "base_sha": base.sha}

    monkeypatch.setattr(close_out, "evidence", evidence)
    return seen


def _run(store: IssueStore, **kw) -> list[int]:
    return close_out.run(store, store.all_issues(), may_run=lambda: True, now=NOW, **kw)


# --- which issues qualify -------------------------------------------------------

@pytest.mark.parametrize("kind", ["not-in-repo", "external", "already-fixed"])
def test_a_current_skip_of_a_close_kind_qualifies(store, kind):
    _triage(store, 1, kind)
    assert close_out.candidate(store.load_issue(1), "live") == kind


@pytest.mark.parametrize(("verdict", "kind"), [
    ("skip", "intended"), ("skip", "feature"), ("skip", "too-big"), ("skip", "unclear"),
    ("ask", "defect"), ("go", "defect")])
def test_any_other_triage_does_not(store, verdict, kind):
    _triage(store, 1, kind, verdict)
    assert close_out.candidate(store.load_issue(1), "live") is None


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_a_maintainers_issue_never_qualifies(store, association, monkeypatch):
    _save(store, 2, association=association)
    _triage(store, 2)
    assert close_out.candidate(store.load_issue(2), "live") is None


def test_under_the_default_scope_nothing_qualifies(store, monkeypatch):
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "maintainers")
    assert close_out.candidate(store.load_issue(1), "live") is None


def test_a_closed_issue_or_a_stale_triage_does_not_qualify(store):
    _save(store, 2, state="closed")
    _triage(store, 2)
    assert close_out.candidate(store.load_issue(2), "live") is None
    _touch(store, 1, updated="2026-10-28T00:00:00Z")
    assert close_out.candidate(store.load_issue(1), "live") is None


def test_an_issue_an_operator_or_the_lane_took_up_does_not_qualify(store):
    store.edit_issue(1).record_fix_request({"action": "solve", "status": "queued",
                                            "source": "operator"})
    assert close_out.candidate(store.load_issue(1), "live") is None


# --- the evidence -------------------------------------------------------------

def test_a_not_in_repo_close_needs_a_second_read_that_agrees(store, monkeypatch):
    reads: list[str | None] = []

    def review(clone, *, title, body, model=None):
        reads.append(model)
        return {"verdict": "skip", "kind": "external", "reason": "a hosted service"}

    monkeypatch.setattr(fixability, "review", review)
    monkeypatch.setattr(settings, "issue_fix_review_model", lambda: "opus")
    assert close_out.evidence(store.load_issue(1), "not-in-repo", BASE)["ok"] is True
    assert reads == ["opus"]
    monkeypatch.setattr(fixability, "review", lambda clone, **kw: {
        "verdict": "go", "kind": "defect", "reason": "it is in src/x.ts"})
    held = close_out.evidence(store.load_issue(1), "not-in-repo", BASE)
    assert held["ok"] is False and "go / defect" in held["why"]


def test_a_second_read_that_cannot_finish_is_no_evidence(store, monkeypatch):
    def review(clone, **kw):
        raise ValueError("unparseable")

    monkeypatch.setattr(fixability, "review", review)
    assert close_out.evidence(store.load_issue(1), "external", BASE)["ok"] is False


@pytest.mark.parametrize(("status", "ok"), [("shown", True), ("unshown", False)])
def test_an_already_fixed_close_needs_a_test_shown_passing(store, monkeypatch, status, ok):
    monkeypatch.setattr(fixed_test, "prove_fixed", lambda n, base, **kw: {
        "status": status, "why": "w", "paths": ["t.test.ts"], "asserts": "a", "tail": ""})
    got = close_out.evidence(store.load_issue(1), "already-fixed", BASE)
    assert got["ok"] is ok and got["tests"] == ["t.test.ts"]


# --- closing ------------------------------------------------------------------

def test_a_close_with_evidence_comments_closes_and_records(store, writes, monkeypatch):
    _evidence(monkeypatch)
    assert _run(store) == [1]
    [call] = writes["calls"]
    assert call["kind"] == "not-in-repo" and call["reason"] == "not_planned"
    assert call["dry_run"] is False and call["token"] == "tok"
    assert call["marker"] == public_comments.marker(1, close_out.KEY)
    assert "does not appear to relate to this repository" in call["body"]
    issue = store.load_issue(1)
    closed = issue.fix_public["closed"]
    assert closed["kind"] == "not-in-repo" and closed["evidence"]["why"] == "agrees"
    assert "lease" not in issue.fix_public
    assert issue.fix_thread[-1]["text"].startswith("Closed this as not-in-repo")


def test_an_already_fixed_close_is_completed(store, writes, monkeypatch):
    _evidence(monkeypatch)
    _triage(store, 1, "already-fixed")
    _run(store)
    [call] = writes["calls"]
    assert call["reason"] == "completed" and "test/x.test.ts" in call["body"]


def test_an_issue_without_evidence_waits_for_a_new_triage(store, writes, monkeypatch):
    seen = _evidence(monkeypatch, ok=False)
    assert _run(store) == [1]
    assert writes["calls"] == [] and "closed" not in store.load_issue(1).fix_public
    assert "the second read finds a defect" in store.load_issue(1).fix_thread[-1]["text"]
    assert _run(store) == [] and len(seen) == 1
    _touch(store, 1, updated="2026-10-28T00:00:00Z")
    _triage(store, 1)
    assert close_out.candidate(store.load_issue(1), "live") == "not-in-repo"


def test_a_failed_write_retries_without_checking_again(store, writes, monkeypatch):
    seen = _evidence(monkeypatch)
    writes["status"]["value"] = "error"
    _run(store)
    assert "closed" not in store.load_issue(1).fix_public
    writes["status"]["value"] = "executed"
    _run(store)
    assert len(seen) == 1 and len(writes["calls"]) == 2
    assert store.load_issue(1).fix_public["closed"]["kind"] == "not-in-repo"


def test_a_live_pass_without_the_bots_token_closes_nothing(store, writes, monkeypatch):
    from prospector_app.backend import executor

    seen = _evidence(monkeypatch)
    monkeypatch.setattr(executor, "mint_bot_token", lambda: None)
    assert _run(store) == [] and seen == [] and writes["calls"] == []
    assert close_out.candidate(store.load_issue(1), "live") == "not-in-repo"


def test_a_forced_dry_run_in_live_mode_is_not_recorded_as_a_close(store, writes, monkeypatch):
    _evidence(monkeypatch)
    writes["status"]["value"] = "dry-run"
    _run(store)
    assert "closed" not in store.load_issue(1).fix_public
    assert store.load_issue(1).fix_public["close_check"]["fails"] == 1


def test_failures_not_the_issues_stop_after_max_fails(store, writes, monkeypatch):
    calls: list[int] = []

    def broken(issue, kind, base):
        calls.append(1)
        raise RuntimeError("docker is down")

    monkeypatch.setattr(close_out, "evidence", broken)
    for _ in range(close_out.MAX_FAILS + 2):
        _run(store)
    assert len(calls) == close_out.MAX_FAILS
    check = store.load_issue(1).fix_public["close_check"]
    assert check["fails"] == close_out.MAX_FAILS and "docker" in check["last_error"]
    assert "leaving it until it is judged again" in store.load_issue(1).fix_thread[-1]["text"]


def test_a_declined_check_waits_for_a_new_triage(store, writes, monkeypatch):
    from pipeline import headless_agent

    def declined(issue, kind, base):
        raise headless_agent.AgentDeclined("refused")

    monkeypatch.setattr(close_out, "evidence", declined)
    _run(store)
    assert close_out.candidate(store.load_issue(1), "live") is None
    assert writes["calls"] == []


def test_a_dry_close_does_not_use_up_the_live_cap(store, writes, monkeypatch):
    _evidence(monkeypatch)
    monkeypatch.setattr(close_out, "DAILY_CAP", 1)
    _save(store, 2, association="NONE")
    _triage(store, 2)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "dry-run")
    assert _run(store) == [2]
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "live")
    assert _run(store) == [2]
    assert _run(store) == []


def test_a_close_the_live_gate_blocks_is_not_tried_again(store, writes, monkeypatch):
    _evidence(monkeypatch)
    writes["status"]["value"] = "blocked"
    _run(store)
    assert close_out.candidate(store.load_issue(1), "live") is None


def test_an_issue_once_closed_is_never_closed_again(store, writes, monkeypatch):
    _evidence(monkeypatch)
    _run(store)
    _touch(store, 1, updated="2026-10-28T00:00:00Z")
    _triage(store, 1)
    assert close_out.candidate(store.load_issue(1), "live") is None


def test_a_dry_run_records_apart_and_writes_nothing(store, writes, monkeypatch):
    _evidence(monkeypatch)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "dry-run")
    _run(store)
    [call] = writes["calls"]
    assert call["dry_run"] is True and call["token"] is None
    pub = store.load_issue(1).fix_public
    assert "closed" not in pub and pub["dry"]["closed"]["kind"] == "not-in-repo"
    assert store.load_issue(1).fix_thread[-1]["text"] == "Dry run: would close this as not-in-repo."
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "live")
    assert close_out.candidate(store.load_issue(1), "live") == "not-in-repo"


def test_an_outsiders_issue_runs_dry_under_all_dry_run(store, writes, monkeypatch):
    _evidence(monkeypatch)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC_SCOPE", "all-dry-run")
    _run(store)
    assert writes["calls"][0]["dry_run"] is True


def test_off_closes_nothing(store, writes, monkeypatch):
    _evidence(monkeypatch)
    monkeypatch.setenv("TRIAGE_ISSUE_FIX_PUBLIC", "off")
    assert _run(store) == [] and writes["calls"] == []


def test_a_pass_closes_at_most_its_batch_and_a_day_its_cap(store, writes, monkeypatch):
    _evidence(monkeypatch)
    for n in range(2, 8):
        _save(store, n)
        _triage(store, n)
    assert _run(store) == [7, 6]
    monkeypatch.setattr(close_out, "DAILY_CAP", 3)
    assert _run(store) == [5]
    assert _run(store) == []


def test_a_pass_stops_when_the_capacity_closes(store, writes, monkeypatch):
    _evidence(monkeypatch)
    assert close_out.run(store, store.all_issues(), may_run=lambda: False, now=NOW) == []


# --- replies after the close --------------------------------------------------

def _closed(store: IssueStore) -> None:
    store.edit_issue(1).record_fix_public({"closed": {"kind": "not-in-repo",
                                                      "at": "2026-10-28T10:00:00+00:00"}})
    _touch(store, 1, state="closed", updated="2026-10-28T11:00:00Z")


def _replies(monkeypatch, rows: list[dict]) -> None:
    monkeypatch.setattr(gh, "gh_list", lambda url, paginate=False: rows)


def _reply(login: str, association: str, at: str = "2026-10-28T10:30:00Z") -> dict:
    return {"id": 1, "user": {"login": login}, "author_association": association,
            "body": "this is in the CLI, not a plugin", "created_at": at}


def test_the_authors_reply_disputes_the_close(store, monkeypatch):
    _closed(store)
    _replies(monkeypatch, [_reply("drive-by", "NONE")])
    assert close_out.watch(store) == 1
    issue = store.load_issue(1)
    disputed = issue.fix_public["closed"]["disputed"]
    assert disputed["by"] == "drive-by" and "CLI" in disputed["reply"]
    assert issue.disposition == "close-disputed"
    assert "needs an operator" in issue.fix_thread[-1]["text"]
    assert store.load_issue(1).state == "closed"


def test_replies_are_read_once_per_update(store, monkeypatch):
    _closed(store)
    reads: list[str] = []
    monkeypatch.setattr(gh, "gh_list", lambda url, paginate=False: reads.append(url) or [])
    assert close_out.watch(store) == 0
    assert close_out.watch(store) == 0
    assert len(reads) == 1
    assert store.load_issue(1).disposition != "close-disputed"


def test_a_close_the_store_has_not_caught_up_with_is_not_a_reopen(store, monkeypatch):
    _closed(store)
    _touch(store, 1, state="open", updated="2026-10-28T11:30:00Z")
    monkeypatch.setattr(gh, "gh_json", lambda path: {"state": "closed"})
    assert close_out.watch(store) == 0
    assert "reopened" not in store.load_issue(1).fix_public["closed"]


def test_a_maintainers_reply_is_noted_and_not_a_dispute(store, monkeypatch):
    _closed(store)
    _replies(monkeypatch, [_reply("lead", "OWNER")])
    close_out.watch(store)
    issue = store.load_issue(1)
    assert "disputed" not in issue.fix_public["closed"]
    assert issue.fix_thread[-1]["text"] == "A maintainer replied after Prospector closed this."


def test_a_reopened_issue_is_noted_and_left_alone(store, monkeypatch):
    _closed(store)
    _touch(store, 1, state="open", updated="2026-10-28T11:30:00Z")
    _replies(monkeypatch, [])
    monkeypatch.setattr(gh, "gh_json", lambda path: {"state": "open"})
    assert close_out.watch(store) == 1
    issue = store.load_issue(1)
    assert issue.fix_public["closed"]["reopened"]
    assert issue.disposition != "close-disputed"
    _triage(store, 1)
    assert close_out.candidate(store.load_issue(1), "live") is None
