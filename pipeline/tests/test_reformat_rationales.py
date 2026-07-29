"""reformat_rationales — the display-polish pass. The safety-critical part is the
faithfulness gate: a committed body must be the analyst's words verbatim, with only
markdown added. These tests pin that the gate accepts pure formatting and rejects any
word change, and that the summary check blocks invented PR refs."""
from pipeline import reformat_rationales as R

SRC = ("Ten PRs fix the same loopback bug. PR 5102 is the winner: it clears the bar "
       "(Greptile 5/5, CI passing). The others close as dups of #5102.")


def test_gate_accepts_pure_markdown():
    # only paragraph breaks, bold, and backticks added — same words, same order
    body = ("Ten PRs fix the same loopback bug.\n\n**PR 5102 is the winner**: it clears "
            "the bar (`Greptile 5/5`, CI passing).\n\nThe others close as dups of #5102.")
    assert R.gate_body(SRC, body)


def test_gate_rejects_a_reworded_body():
    body = SRC.replace("the winner", "clearly the best choice")
    assert not R.gate_body(SRC, body)


def test_gate_rejects_a_dropped_word():
    body = SRC.replace("the same loopback bug", "the loopback bug")
    assert not R.gate_body(SRC, body)


def test_gate_rejects_added_content():
    assert not R.gate_body(SRC, SRC + " This is an editorial aside.")


def test_summary_accepts_grounded_reference():
    assert R.check_summary(SRC, "Merge #5102; close the other nine as duplicates.")


def test_summary_rejects_invented_pr():
    assert not R.check_summary(SRC, "Merge #9999; it supersedes the cluster.")


def test_summary_rejects_empty_and_overlong():
    assert not R.check_summary(SRC, "   ")
    assert not R.check_summary(SRC, " ".join(["word"] * 50))


def test_parse_reply_extracts_json_block():
    reply = 'Here you go:\n```json\n{"summary": "S", "body": "B"}\n```'
    assert R._parse_reply(reply) == ("S", "B")


def test_parse_reply_rejects_non_json_and_wrong_shape():
    assert R._parse_reply("just prose, no json") is None
    assert R._parse_reply('```json\n{"summary": "S"}\n```') is None  # missing body


def test_backend_dispatch_follows_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert R.have_api() is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert R.have_api() is True


def test_api_call_returns_none_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert R._ask_api("anything", R.HAIKU) is None


def test_raw_still_salvages_a_sound_summary(monkeypatch):
    # body always drifts (reworded), but the summary is grounded
    monkeypatch.setattr(R, "_ask",
                        lambda s, m: ("Merge #5102; close the rest as dups.", s.replace("winner", "best one")))
    res = R.reformat_one(SRC)
    assert res["tier"] == "raw"
    assert res["body"] == SRC  # original text kept intact
    assert res["summary"] == "Merge #5102; close the rest as dups."  # TL;DR salvaged


def test_raw_summary_is_none_when_summary_also_unsound(monkeypatch):
    # body drifts AND the summary invents a PR number -> nothing to salvage
    monkeypatch.setattr(R, "_ask",
                        lambda s, m: ("Superseded by #9999.", s.replace("winner", "best one")))
    res = R.reformat_one(SRC)
    assert res["tier"] == "raw" and res["summary"] is None
