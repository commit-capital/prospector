"""Wire-structs: the transient shapes drivers pass between each other and hand to
the JS workflows as JSON. The golden checks pin to_dict() to the exact bytes the
summarize workflow already reads, so the dataclass can't silently drift the wire
format."""
import json
from pathlib import Path

from pipeline import cluster_driver as cd
from pipeline.store import Store
from pipeline.wire import DiffManifestItem, SummaryEntry, VerdictItem


NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, head="h1", title="t"):
    store.save_pr({"pr": n, "meta": {"title": title, "author": "a", "state": "open",
                   "draft": False, "head_sha": head, "checked_at": NOW}})
    rec = store.load_pr(n)
    assert rec is not None
    return rec


class TestDiffManifestItem:
    def test_for_pr_projects_the_four_fields(self, tmp_path):
        rec = _pr(Store(tmp_path), 7, head="abc", title="fix lock leak")
        item = DiffManifestItem.for_pr(7, rec, Path("/diffs"))
        assert item.pr == 7
        assert item.head_sha == "abc"
        assert item.title == "fix lock leak"
        assert item.diff_path == "/diffs/abc.diff"

    def test_to_dict_matches_the_legacy_wire_shape(self, tmp_path):
        """to_dict() must reproduce the bare dict the drivers used to build, key
        order included — summarize.js reads {pr, head_sha, title, diff_path}."""
        rec = _pr(Store(tmp_path), 7, head="abc", title="fix lock leak")
        diffs = Path("/diffs")
        item = DiffManifestItem.for_pr(7, rec, diffs)
        legacy = {"pr": 7, "head_sha": rec.head_sha, "title": rec.title,
                  "diff_path": str(diffs / f"{rec.head_sha}.diff")}
        assert item.to_dict() == legacy
        # key order is load-bearing across the JSON boundary
        assert json.dumps(item.to_dict()) == json.dumps(legacy)
        assert list(item.to_dict().keys()) == ["pr", "head_sha", "title", "diff_path"]

    def test_frozen(self, tmp_path):
        rec = _pr(Store(tmp_path), 7)
        item = DiffManifestItem.for_pr(7, rec, Path("/diffs"))
        try:
            item.pr = 8  # type: ignore[misc]
            assert False, "DiffManifestItem should be frozen"
        except AttributeError:
            pass


_SUMMARY = {"one_liner": "fixes lock leak", "mechanism": "clears field on close",
            "subsystem": "execution-locks", "identifiers": ["executionRunId"],
            "paths": ["server/src/locks.ts"], "primary_change": "drop the stale lock",
            "secondary_changes": ["tidy imports"]}


class TestSummaryEntry:
    def test_to_dict_matches_the_legacy_wire_shape(self, tmp_path):
        rec = _pr(Store(tmp_path), 7, title="t7")
        entry = SummaryEntry.from_pr(7, rec, _SUMMARY)
        legacy = {
            "pr": 7, "title": rec.title, "one_liner": _SUMMARY["one_liner"],
            "mechanism": _SUMMARY["mechanism"], "identifiers": _SUMMARY["identifiers"],
            "paths": _SUMMARY["paths"],
            "primary_change": _SUMMARY["primary_change"] or _SUMMARY["one_liner"],
            "secondary_changes": _SUMMARY["secondary_changes"],
        }
        assert entry.to_dict() == legacy
        assert json.dumps(entry.to_dict()) == json.dumps(legacy)

    def test_primary_change_falls_back_to_one_liner(self, tmp_path):
        rec = _pr(Store(tmp_path), 7)
        s = {k: v for k, v in _SUMMARY.items() if k != "primary_change"}
        assert SummaryEntry.from_pr(7, rec, s).primary_change == _SUMMARY["one_liner"]

    def test_missing_list_fields_default_empty(self, tmp_path):
        rec = _pr(Store(tmp_path), 7)
        entry = SummaryEntry.from_pr(7, rec, {"one_liner": "x"})
        assert entry.identifiers == [] and entry.paths == [] and entry.secondary_changes == []

    def test_driver_summary_entry_is_unchanged(self, tmp_path):
        """cluster_driver.summary_entry now delegates to SummaryEntry — its output
        (the cluster-unit JSON the cluster/assign workflows read) must not move."""
        rec = _pr(Store(tmp_path), 7, title="t7")
        assert cd.summary_entry(7, rec, _SUMMARY) == SummaryEntry.from_pr(7, rec, _SUMMARY).to_dict()


class TestVerdictItem:
    def test_from_dict_selects_fields_from_the_workflow_superset(self):
        """The security workflow writes {...base, verdict, findings} where base
        carries lenses_ok/lenses_total and no tier — from_dict picks out the
        fields it owns (a plain VerdictItem(**d) would raise on the extras)."""
        d = {"pr": 12, "head_sha": "abc", "lenses_ok": 3, "lenses_total": 3,
             "verdict": "RED",
             "findings": [{"severity": "red", "category": "authz", "title": "t",
                           "detail": "d", "location": "x.ts", "lens": "security"}]}
        item = VerdictItem.from_dict(d)
        assert item.pr == 12 and item.head_sha == "abc" and item.verdict == "RED"
        assert item.tier == "adversarial"  # defaulted — workflow omits it
        # extra/open finding keys (category, lens) survive verbatim
        assert item.findings[0]["category"] == "authz" and item.findings[0].get("lens") == "security"

    def test_from_dict_defaults_tier_and_tolerates_missing_optionals(self):
        item = VerdictItem.from_dict({"pr": 1, "verdict": "GREEN"})
        assert item.tier == "adversarial" and item.head_sha is None and item.findings == []

    def test_to_dict_round_trips(self):
        d = {"pr": 1, "head_sha": "h", "verdict": "GREEN", "findings": [], "tier": "adversarial"}
        assert VerdictItem.from_dict(d).to_dict() == d


class TestVerifyWire:
    def test_blind_item_defaults_the_optional_fields(self):
        from pipeline.wire import BlindItem

        b = BlindItem.from_dict({"pr": 7, "head_sha": "h7", "has_test": False,
                                 "faithful": False, "reasoning": "no test"})
        assert b.test_cmd is None and b.repro_command is None
        assert b.requires_live_agent is False and b.expected_red_signature is None
        assert b.expected_repro_signature is None

    def test_blind_item_non_bool_faithful_is_not_faithful(self):
        # A malformed `faithful` (e.g. the string "false" an agent might emit)
        # must never read as faithful — bool("false") is True, which would flip
        # an unfaithful verdict to faithful and disarm the escalate anchor. It
        # fails toward not-faithful (→ escalate/human), never toward verified.
        from pipeline.wire import BlindItem

        for bad in ("false", "true", "no", "", 0, 1, None, {}):
            b = BlindItem.from_dict({"pr": 7, "head_sha": "h7", "has_test": True,
                                     "faithful": bad, "reasoning": "r"})
            assert b.faithful is False, bad
        # a real bool is preserved exactly
        assert BlindItem.from_dict(
            {"pr": 7, "faithful": True, "has_test": True}).faithful is True
        assert BlindItem.from_dict(
            {"pr": 7, "faithful": False, "has_test": True}).faithful is False

    def test_blind_item_non_bool_flags_are_not_true(self):
        # the same unsafe coercion applied to every other boolean field
        from pipeline.wire import BlindItem

        b = BlindItem.from_dict({"pr": 7, "faithful": True,
                                 "has_test": "false", "requires_live_agent": "false",
                                 "from_linked_issue": "false"})
        assert b.has_test is False
        assert b.requires_live_agent is False
        assert b.from_linked_issue is False

    def test_blind_item_to_signal_is_the_stored_shape(self):
        from pipeline.wire import BlindItem

        b = BlindItem.from_dict({
            "pr": 7, "head_sha": "h7", "has_test": True, "faithful": True,
            "test_cmd": "pnpm -s test a.test.ts", "confidence": "high",
            "claimed_symptom": "throws", "expected_red_signature": "TypeError",
            "repro_command": "node -e \"require('./x')\"",
            "expected_repro_signature": "Cannot find module './x'",
            "reasoning": "matches"})
        sig = b.to_signal()
        assert sig["test_cmd"] == "pnpm -s test a.test.ts"
        assert sig["expected_red_signature"] == "TypeError"
        assert sig["expected_repro_signature"] == "Cannot find module './x'"
        assert "pr" not in sig and "head_sha" not in sig

    def test_blind_item_repro_rejected_is_driver_set_never_agent_supplied(self):
        # repro_rejected records the DRIVER's pre-run rejection of the agent's
        # repro_command: from_dict never reads it from agent output, and
        # to_signal carries it into the stored blind_adequacy shape.
        import dataclasses

        from pipeline.wire import BlindItem

        b = BlindItem.from_dict({"pr": 7, "has_test": True, "faithful": True,
                                 "repro_rejected": "agent-supplied text"})
        assert b.repro_rejected is None
        assert b.to_signal()["repro_rejected"] is None
        vetted = dataclasses.replace(b, repro_command=None,
                                     expected_repro_signature=None,
                                     repro_rejected="targets 'src/x.test.ts'")
        sig = vetted.to_signal()
        assert sig["repro_rejected"] == "targets 'src/x.test.ts'"
        assert sig["repro_command"] is None

    def test_judge_item_carries_no_outcome(self):
        from pipeline.wire import JudgeItem

        j = JudgeItem.from_dict({
            "pr": 7, "red_reason_match": {"matches": True, "confidence": "high",
                                          "reasoning": "same assertion"},
            "findings": [], "outcome": "verified-fix"})
        assert not hasattr(j, "outcome")
        assert j.red_reason_match["matches"] is True

    def test_judge_item_defaults_repro_reason_match_when_no_repro_ran(self):
        from pipeline.wire import JudgeItem

        j = JudgeItem.from_dict({
            "pr": 7, "red_reason_match": {"matches": True, "confidence": "high",
                                          "reasoning": "same assertion"},
            "findings": []})
        assert j.repro_reason_match == {}

    def test_judge_item_carries_the_repro_reason_match(self):
        from pipeline.wire import JudgeItem

        j = JudgeItem.from_dict({
            "pr": 7, "red_reason_match": {"matches": True, "confidence": "high",
                                          "reasoning": "same assertion"},
            "repro_reason_match": {"applicable": True, "matches": False,
                                   "confidence": "high", "reasoning": "timed out"},
            "findings": []})
        assert j.repro_reason_match == {"applicable": True, "matches": False,
                                        "confidence": "high", "reasoning": "timed out"}
