import pytest

from pipeline import display_pass as dp
from pipeline.store import Store

GOOD = {"problem": "Deleting an agent's instructions file asked for no confirmation.",
        "fix": "Require an explicit --yes flag before the CLI deletes it.",
        "why": ["Small, tested change to one command."],
        "how": ["The command stops before deleting unless --yes is passed."]}


def _src(rationale: str = "Duplicate of #42; the same guard already landed.") -> dict[str, str]:
    return {"verdict": "close-dup", "title": "fix(cli): confirm deletes", "body": "",
            "issues": "#7 delete is destructive", "summary": "", "rationale": rationale,
            "asks": "(none)", "blockers": "(none)"}


def _seed(tmp_path, monkeypatch) -> Store:
    monkeypatch.setattr(dp, "_issue_lines", lambda pr: "(none)")
    st = Store(tmp_path)
    st.create_pr(1, {"title": "fix(cli): confirm deletes", "state": "open", "head_sha": "h1"})
    st.edit_pr(1).route_to("close-wontfix", "The wrong approach; #42 has the better one.")
    st.edit_pr(1).set_summary({"one_liner": "Require --yes before deleting"})
    return st


def test_check_accepts_a_faithful_card():
    assert dp.check(GOOD, _src(), "close-dup") == GOOD


@pytest.mark.parametrize("field,value", [
    ("problem", "This fixes #999, a crash."),
    ("why", ["Same change as #43."]),
])
def test_check_rejects_invented_refs(field, value):
    assert dp.check({**GOOD, field: value}, _src(), "close-dup") is None


def test_check_allows_refs_the_inputs_name():
    assert dp.check({**GOOD, "why": ["Same guard as #42 for #7."]}, _src(), "close-dup")


@pytest.mark.parametrize("why,disposition", [
    (["This is ready to merge."], "close-dup"),
    (["Should be merged as is."], "close-dup"),
    (["It is a duplicate."], "merge"),
])
def test_check_rejects_a_changed_verdict(why, disposition):
    assert dp.check({**GOOD, "why": why}, _src("Clean and tested."), disposition) is None


def test_check_keeps_a_verdict_phrase_the_rationale_uses():
    src = _src("Not a duplicate of #42: it touches a different command.")
    assert dp.check({**GOOD, "why": ["Not a duplicate of #42."]}, src, "merge")


@pytest.mark.parametrize("reply", [None, "text", {**GOOD, "why": []}, {**GOOD, "fix": ""},
                                   {**GOOD, "how": ["word " * 30]}])
def test_check_rejects_malformed_replies(reply):
    assert dp.check(reply, _src(), "close-dup") is None


def test_problem_section_prefers_the_problem_heading():
    body = "## Summary\n\n### What happened?\n\nIt deleted the file.\n\n### Steps\n\n1. run"
    assert dp.problem_section(body) == "It deleted the file."
    assert dp.problem_section("no headings here") == "no headings here"


def test_write_stores_a_current_section(tmp_path, monkeypatch):
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_ask", lambda prompt: GOOD)
    sec = dp.write(st, 1, trigger="operator")
    assert sec is not None and sec["problem"] == GOOD["problem"]
    pr = st.load_pr(1)
    assert pr.disposition == "close-wontfix"
    assert dp.current(pr) == pr.section("display")
    assert [r.raw["stats"]["written"] for r in st.runs(phase=dp.PHASE)] == [True]


def test_write_stores_nothing_when_every_reply_is_rejected(tmp_path, monkeypatch):
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_ask", lambda prompt: {**GOOD, "fix": "Fixes #999."})
    assert dp.write(st, 1, trigger="operator") is None
    assert st.load_pr(1).section("display") is None
    assert [r.raw["stats"]["written"] for r in st.runs(phase=dp.PHASE)] == [False]


def test_a_new_verdict_or_head_retires_the_card(tmp_path, monkeypatch):
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(dp, "_ask", lambda prompt: GOOD)
    dp.write(st, 1, trigger="operator")
    st.edit_pr(1).route_to("close-stale", "Abandoned.")
    assert dp.current(st.load_pr(1)) is None
    st.edit_pr(1).route_to("close-wontfix", "The wrong approach; #42 has the better one.")
    assert dp.current(st.load_pr(1)) is not None
    rec = st.load_pr(1).rec
    rec["meta"]["head_sha"] = "h2"
    st.save_pr(rec)
    assert dp.current(st.load_pr(1)) is None


def test_missing_section_reads_as_none(tmp_path, monkeypatch):
    st = _seed(tmp_path, monkeypatch)
    assert dp.current(st.load_pr(1)) is None
    assert dp.wanted(st.all_prs()) == [1]
