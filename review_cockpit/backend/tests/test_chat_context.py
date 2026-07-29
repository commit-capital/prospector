"""The agent's injected context must not leak the pipeline's verdict (so its
'do you agree?' judgment is unbiased). It gets neutral member signals; it reads
the actual disposition/rationale from the store itself, afterwards."""
import asyncio
from unittest import mock

from review_cockpit.backend import chat

_DETAIL = {
    "cluster_id": 12,
    "root_problem": "Parser drops trailing commas",
    "outcome": "merge-ready",                      # verdict — must NOT leak
    "state": "ready",                              # verdict-ish — must NOT leak
    "rationale": "PIPELINE_SECRET_RATIONALE picks #101",  # must NOT leak
    "buckets": {"merge": [{"number": 101}], "request-changes": [{"number": 102}]},
    "prs": [
        {"number": 101, "title": "Fix parser", "author": "alice",
         "disposition": "merge",                   # must NOT leak
         "signals": {"greptile": 5, "ci": "passing", "conflicts": False,
                   "has_tests": True, "additions": 40, "deletions": 3,
                   "changed_files": 2}},
        {"number": 102, "title": "Alt fix", "author": "bob",
         "disposition": "request-changes",
         "signals": {"greptile": 4, "ci": "passing", "conflicts": True,
                   "has_tests": False, "additions": 120, "deletions": 10,
                   "changed_files": 5}},
    ],
}


def _ctx():
    with mock.patch.object(chat.service, "cluster_detail", return_value=_DETAIL):
        return chat._cluster_context(12)


def test_neutral_facts_present():
    out = _ctx()
    assert "Parser drops trailing commas" in out
    assert "#101" in out and "#102" in out
    assert "alice" in out and "bob" in out
    assert "greptile 5" in out
    assert "greptile 4" in out


def test_verdict_does_not_leak():
    out = _ctx()
    assert "PIPELINE_SECRET_RATIONALE" not in out
    assert "merge-ready" not in out
    assert "disposition" not in out.lower()
    assert "request-changes" not in out


# The per-PR path must also stay verdict-free (spec item 1 covers both). pr_detail
# carries the disposition/rationale; assert _pr_context never renders them.
_PR_DETAIL = {
    "title": "Fix the parser", "author": "alice", "clusters": [12, 15],
    "safety": "GREEN", "drift_state": "current",
    "disposition": "merge",                                     # must NOT leak
    "proposed_action": {"rationale": "PR_VERDICT_RATIONALE"},   # must NOT leak
}


def test_pr_context_does_not_leak_disposition(temp_store):
    with mock.patch.object(chat.service, "pr_detail", return_value=_PR_DETAIL), \
         mock.patch.object(chat.data, "safety_by_pr", return_value={}), \
         mock.patch.object(chat.service, "get_diff", return_value={"diff": ""}):
        out = chat._pr_context(101, None, None)
    assert "Fix the parser" in out and "alice" in out   # neutral facts present
    assert "12,15" in out                                # straddler's clusters render
    assert "PR_VERDICT_RATIONALE" not in out             # rationale withheld
    assert "disposition" not in out.lower()


# An issue subject (`?issue=N`) gets the same treatment as PRs/clusters: neutral
# facts (report, signals, dedup members, candidate PRs) with the pipeline's
# recorded disposition/rationale withheld.
_ISSUE_DETAIL = {
    "number": 2928, "title": "Crash when saving a board", "author": "carol",
    "state": "open", "labels": ["bug"], "comments": 4, "reactions": 7,
    "subsystem": "persistence", "repro_grade": "B", "pain": 8,
    "cluster": 31, "cluster_size": 3, "duplicates": [2801, 2660],
    "canonical": 2928, "is_dup": False,
    "disposition": "close-dup",                                # must NOT leak
    "analysis": {"rationale": "ISSUE_SECRET_RATIONALE",        # must NOT leak
                 "gist": "ISSUE_SECRET_GIST"},
    "linked_prs": [{"pr": 101, "how": "explicit", "title": "Fix save crash",
                    "state": "merged", "in_store": True}],
    "body": "1. open a board\n2. hit save\n3. crash",
}


def test_issue_context_neutral_facts_present(temp_store):
    with mock.patch.object(chat.issues, "get_issue", return_value=_ISSUE_DETAIL):
        out = chat._issue_context(2928)
    assert "Issue #2928" in out and "Crash when saving a board" in out
    assert "carol" in out
    assert "repro grade B" in out and "pain 8" in out
    assert "#2801" in out and "#2660" in out       # dedup-cluster members
    assert "#101" in out and "merged" in out       # candidate PR with its state
    assert "hit save" in out                       # the report body rides along


def test_issue_context_verdict_does_not_leak(temp_store):
    with mock.patch.object(chat.issues, "get_issue", return_value=_ISSUE_DETAIL):
        out = chat._issue_context(2928)
    assert "ISSUE_SECRET_RATIONALE" not in out
    assert "ISSUE_SECRET_GIST" not in out
    assert "close-dup" not in out


def test_issue_context_handles_unknown_issue(temp_store):
    with mock.patch.object(chat.issues, "get_issue", return_value=None):
        out = chat._issue_context(999)
    assert "Issue #999" in out  # no crash on an issue missing from the store


# The executed-action record (the activity log) DOES ride along in the subject
# context — unlike the pipeline's verdict it's ground truth of what actually
# happened, and withholding it made the agent answer "what happened to X?" from
# the pipeline's proposal instead.
def _insert_activity(url: str, rows: list[dict]) -> None:
    from pipeline import schema
    from pipeline import storekit
    eng = storekit.get_engine(url)
    with eng.begin() as conn:
        for r in rows:
            conn.execute(schema.activity.insert().values(**schema.activity_row(r)))


def test_pr_context_shows_executed_actions(temp_store):
    _insert_activity(temp_store, [
        {"at": "2026-07-17T10:00:00", "kind": "close", "action": "CLOSE_STALE",
         "status": "executed", "reason": "stale", "identity": "test-bot",
         "operator": "Casey", "pr": 101, "dry_run": False},
        {"at": "2026-07-17T11:00:00", "kind": "close", "status": "dry-run",
         "identity": "test-bot", "operator": "Casey", "pr": 101, "dry_run": True},
    ])
    with mock.patch.object(chat.service, "pr_detail", return_value=_PR_DETAIL), \
         mock.patch.object(chat.data, "safety_by_pr", return_value={}), \
         mock.patch.object(chat.service, "get_diff", return_value={"diff": ""}):
        out = chat._pr_context(101, None, None)
    assert "Actions already executed" in out
    assert "close (stale) as test-bot, operator Casey" in out
    assert out.count("2026-07-17") == 1   # the dry-run preview is excluded


def test_issue_context_shows_executed_actions(temp_store):
    _insert_activity(temp_store, [
        {"at": "2026-07-17T10:00:00", "kind": "issue-close", "action": "CLOSE_ISSUE",
         "status": "closed", "reason": "not planned", "identity": "test-bot",
         "operator": "Casey", "issue": 2928, "dry_run": False},
    ])
    with mock.patch.object(chat.issues, "get_issue", return_value=_ISSUE_DETAIL):
        out = chat._issue_context(2928)
    assert "Actions already executed" in out
    assert "issue-close (not planned) as test-bot, operator Casey" in out


def test_issue_subject_gets_its_own_thread_key():
    assert chat._thread_key(None, None, None, 2928) == "issue-2928"
    assert chat._thread_key("sess-x", None, None, 2928) == "sess-x"


# #355: the agent pane should see whatever PRs the operator is currently
# filtered/viewing to in PR Explorer, without re-listing them in the question.
_ROWS = {
    101: {"title": "Fix parser", "author": "alice", "disposition": "merge",
          "signals": {"greptile": 5, "ci": "passing", "conflicts": False,
                    "has_tests": True, "additions": 40, "deletions": 3, "changed_files": 2}},
    102: {"title": "Alt fix", "author": "bob", "disposition": "request-changes",
          "signals": {"greptile": 4, "ci": "passing", "conflicts": True,
                    "has_tests": False, "additions": 120, "deletions": 10, "changed_files": 5}},
}


def test_visible_prs_context_neutral_facts():
    with mock.patch.object(chat.service, "pr_row", side_effect=lambda n: _ROWS.get(n)):
        out = chat._visible_prs_context([101, 102])
    assert "2 PR(s)" in out
    assert "#101" in out and "#102" in out
    assert "alice" in out and "bob" in out
    assert "greptile 5" in out and "greptile 4" in out
    assert "request-changes" not in out  # disposition *values* don't leak (same convention as cluster context)


def test_visible_prs_context_notes_truncation_honestly():
    with mock.patch.object(chat.service, "pr_row", side_effect=lambda n: _ROWS.get(n)):
        out = chat._visible_prs_context([101], total=500)
    assert "500 PR(s)" in out          # the true match count, not just what was sent
    assert "+499 more not shown" in out


def test_visible_prs_context_skips_pr_missing_from_the_snapshot():
    with mock.patch.object(chat.service, "pr_row", return_value=None):
        out = chat._visible_prs_context([999])
    assert "#999" not in out  # no crash, no fabricated line for an unknown PR


# #507: a PR flyout open on top of the filtered Explorer list isn't a signal
# that the operator abandoned that list — the visible-PR context must ride
# alongside a pr/cluster subject's own context too, not just general questions.
def test_visible_prs_context_rides_alongside_a_pr_subject(temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    monkeypatch.setattr(chat, "_pr_context", lambda *a, **k: "SUBJECT-CONTEXT-PR-101")
    monkeypatch.setattr(chat.service, "pr_row", lambda n: _ROWS.get(n))

    captured: dict[str, list[str]] = {}

    class FakeProc:
        pid = 1
        def __init__(self):
            self.returncode = 0
            self.stdout = self._gen()
        async def _gen(self):
            yield b'{"type":"result","session_id":"sess-A"}\n'
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        async for _ in chat.stream_chat("review these", pr=101, prs=[101, 102], prs_total=2):
            pass
    asyncio.run(drive())

    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "SUBJECT-CONTEXT-PR-101" in prompt  # the flyout PR's own full context…
    assert "#102" in prompt                    # …AND the broader filtered list


def test_visible_prs_context_omitted_when_nothing_is_currently_filtered(temp_store, tmp_path, monkeypatch):
    monkeypatch.setattr(chat, "SESSION_DIR", tmp_path / "cache" / "chat")
    monkeypatch.setattr(chat, "_op_slug", lambda: "tester")
    monkeypatch.setattr(chat, "_bot_token", lambda: None)
    monkeypatch.setattr(chat, "system_prompt", lambda: "SYS")
    monkeypatch.setattr(chat, "_pr_context", lambda *a, **k: "SUBJECT-CONTEXT-PR-101")

    captured: dict[str, list[str]] = {}

    class FakeProc:
        pid = 1
        def __init__(self):
            self.returncode = 0
            self.stdout = self._gen()
        async def _gen(self):
            yield b'{"type":"result","session_id":"sess-A"}\n'
        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        async for _ in chat.stream_chat("is this safe?", pr=101):
            pass
    asyncio.run(drive())

    prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
    assert "SUBJECT-CONTEXT-PR-101" in prompt
    assert "currently looking at" not in prompt  # no visible-PRs block was sent
