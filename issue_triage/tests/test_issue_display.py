from __future__ import annotations

from issue_triage import issue_display as idp
from issue_triage import issue_store

META = {"title": "Cancel is a no-op after a restart", "body": "See #12 for the reaper.",
        "state": "open", "updated_at": "T1"}
GOOD = {"bug": "A login interrupted by a restart can never be cancelled.",
        "fix": "Cancel the stored session and release its lease at startup."}


def _seed(tmp_path) -> issue_store.IssueStore:
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(5, META)
    st.edit_issue(5).route_to("needs-human", "A maintainer should decide.", gist="Stuck logins.")
    return st


def test_check_rejects_a_number_the_inputs_never_name(tmp_path):
    src = idp.inputs(_seed(tmp_path).load_issue(5))
    assert idp.check(GOOD, src) == GOOD
    assert idp.check({**GOOD, "fix": "Merge #12."}, src) == {**GOOD, "fix": "Merge #12."}
    assert idp.check({**GOOD, "fix": "Merge #999."}, src) == \
        "it cited #999, which nothing it was given names"
    assert idp.check({"bug": GOOD["bug"]}, src) == "the bug or fix was missing"


def test_write_stores_a_card_a_new_analysis_retires(tmp_path, monkeypatch):
    st = _seed(tmp_path)
    monkeypatch.setattr(idp.display_pass, "ask", lambda prompt: GOOD)
    sec = idp.write(st, 5, trigger="operator")
    assert isinstance(sec, dict) and sec["fix"] == GOOD["fix"]
    assert idp.current(st.load_issue(5)) == st.load_issue(5).section("display")
    assert [r.raw["stats"]["written"] for r in st.runs() if r.raw["phase"] == idp.PHASE] == [True]
    st.edit_issue(5).route_to("needs-human", "Logging alone is not enough.", gist="Stuck logins.")
    assert idp.current(st.load_issue(5)) is None


def test_write_stores_nothing_when_every_reply_is_rejected(tmp_path, monkeypatch):
    st = _seed(tmp_path)
    monkeypatch.setattr(idp.display_pass, "ask", lambda prompt: {**GOOD, "bug": "See #999."})
    assert idp.write(st, 5, trigger="operator") == "it cited #999, which nothing it was given names"
    assert st.load_issue(5).section("display") is None


def test_refresh_writes_only_stale_cards(tmp_path, monkeypatch):
    st = _seed(tmp_path)
    st.create_issue(6, META)
    asked: list[str] = []
    monkeypatch.setattr(idp.display_pass, "ask", lambda prompt: asked.append(prompt) or GOOD)
    assert idp.refresh(st, [5, 6], trigger="analyze") == 2
    assert idp.refresh(st, [5, 6], trigger="analyze") == 0
    assert len(asked) == 2


def test_refresh_ends_quietly_when_the_agent_is_unavailable(tmp_path):
    st = _seed(tmp_path)
    assert idp.refresh(st, [5], trigger="issue-fix") == 0
    assert st.load_issue(5).section("display") is None


def test_wanted_lists_open_analyzed_issues_without_a_current_card(tmp_path, monkeypatch):
    st = _seed(tmp_path)
    st.create_issue(6, META)
    st.create_issue(7, {**META, "state": "closed"})
    st.edit_issue(7).route_to("needs-human", "Closed upstream.", gist="Closed.")
    st.create_issue(8, META)
    st.edit_issue(8).route_to("needs-human", "Also stuck.", gist="Stuck too.")
    assert idp.wanted(st.all_issues()) == [8, 5]
    monkeypatch.setattr(idp.display_pass, "ask", lambda prompt: GOOD)
    idp.write(st, 8, trigger="operator")
    assert idp.wanted(st.all_issues()) == [5]


def test_refresh_with_force_rewrites_a_current_card(tmp_path, monkeypatch):
    st = _seed(tmp_path)
    asked: list[str] = []
    monkeypatch.setattr(idp.display_pass, "ask", lambda prompt: asked.append(prompt) or GOOD)
    idp.write(st, 5, trigger="operator")
    assert idp.refresh(st, [5], trigger="operator", force=True) == 1
    assert len(asked) == 2
