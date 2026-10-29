"""A head whose diff is the same patch as an earlier head's keeps the facts
judged from that diff: merging the base into a branch moves the head without
changing the change under review."""
from __future__ import annotations

from pathlib import Path

from pipeline import diff_cache, freshness, review_policy, reviewers, threat_scan
from pipeline.model import Pr
from pipeline.store import Store

_TEST = "server/src/__tests__/cleanup-removal-service.test.ts"


def _diff(index: str, start: int, added: str = "companySkills", context: str = "issues") -> str:
    return (f"diff --git a/{_TEST} b/{_TEST}\n"
            f"index {index} 100644\n"
            f"--- a/{_TEST}\n+++ b/{_TEST}\n"
            f"@@ -{start},6 +{start},7 @@ describe(\"cleanup removal services\", () => {{\n"
            f"     expect(removed?.id).toBe(companyId);\n"
            f"     await expect(select({context})).resolves.toHaveLength(0);\n"
            f"+    await expect(select({added})).resolves.toHaveLength(0);\n"
            f"     await expect(select(documents)).resolves.toHaveLength(0);\n")


# The #13490 shape: the base grew above the hunk, so the blob hashes and line
# numbers changed and nothing else did.
BEFORE = _diff("68bf1ce2c17b..2ecc283286a0", 227)
AFTER_BASE_MERGE = _diff("2bf989846d2..3b10dc6b34d", 248)


class TestPatchId:
    def test_a_base_merge_keeps_the_patch(self):
        assert diff_cache.patch_id(BEFORE) == diff_cache.patch_id(AFTER_BASE_MERGE)

    def test_a_changed_line_is_another_patch(self):
        assert diff_cache.patch_id(BEFORE) != diff_cache.patch_id(_diff("aa..bb", 227, added="agents"))

    def test_changed_context_is_another_patch(self):
        assert diff_cache.patch_id(BEFORE) != diff_cache.patch_id(_diff("aa..bb", 227, context="projects"))

    def test_a_capped_diff_has_none(self):
        assert diff_cache.patch_id(BEFORE + "x" * diff_cache.MAX_DIFF_BYTES) is None

    def test_a_binary_change_has_none(self):
        binary = ("diff --git a/fixture.zip b/fixture.zip\nindex 1a..2b 100644\n"
                  "Binary files a/fixture.zip and b/fixture.zip differ\n")
        assert diff_cache.patch_id(BEFORE + binary) is None


def _seed(store: Store, diffs: Path, head: str, diff: str) -> None:
    (diffs / f"{head}.diff").write_text(diff)
    rec = store.load_pr(1)
    sections = {k: v for k, v in (rec.raw if rec else {}).items() if k not in ("pr", "meta")}
    store.save_pr({"pr": 1, **sections,
                   "meta": {"title": "t", "author": "alice", "state": "open", "draft": False,
                            "head_sha": head, "checked_at": "2026-10-28T00:00:00+00:00"}})


def _scan(store: Store, diffs: Path) -> dict:
    threat_scan.scan(store, store.all_prs(), diffs, fetch=False)
    rec = store.load_pr(1)
    assert rec is not None
    return rec.section("patch") or {}


class TestScanRecordsThePatch:
    def test_base_merges_chain_and_a_real_change_resets(self, tmp_path):
        store, diffs = Store(tmp_path), tmp_path / "diffs"
        diffs.mkdir()
        _seed(store, diffs, "h1", BEFORE)
        assert _scan(store, diffs)["same_as"] == []
        _seed(store, diffs, "h2", AFTER_BASE_MERGE)
        assert _scan(store, diffs)["same_as"] == ["h1"]
        _seed(store, diffs, "h3", _diff("cc..dd", 260))
        assert _scan(store, diffs)["same_as"] == ["h1", "h2"]
        _seed(store, diffs, "h4", _diff("cc..dd", 260, added="agents"))
        assert _scan(store, diffs)["same_as"] == []

    def test_a_record_stamped_before_patches_starts_its_chain_at_its_next_head(self, tmp_path):
        store, diffs = Store(tmp_path), tmp_path / "diffs"
        diffs.mkdir()
        _seed(store, diffs, "h1", BEFORE)
        _scan(store, diffs)
        pr = store.edit_pr(1)
        del pr.rec["patch"]
        pr.set_threat(pr.section("threat"))
        assert _scan(store, diffs) == {}
        _seed(store, diffs, "h2", AFTER_BASE_MERGE)
        assert _scan(store, diffs)["same_as"] == []

    def test_a_capped_head_records_no_patch(self, tmp_path):
        store, diffs = Store(tmp_path), tmp_path / "diffs"
        diffs.mkdir()
        _seed(store, diffs, "h1", BEFORE)
        _scan(store, diffs)
        _seed(store, diffs, "h2", AFTER_BASE_MERGE + "x" * diff_cache.MAX_DIFF_BYTES)
        assert _scan(store, diffs)["against_head_sha"] == "h1"


def _pr(head: str = "h2", live: str | None = None, same_as: tuple[str, ...] = ("h1",),
        **sections: dict) -> Pr:
    meta = {"title": "t", "state": "open", "head_sha": head}
    if live:
        meta["live_head_sha"] = live
    patch = {"id": "p", "same_as": list(same_as), "against_head_sha": head,
             "checked_at": "2026-10-28T00:00:00+00:00"}
    return Pr(None, {"pr": 1, "meta": meta, "patch": patch, **sections})


def _at(sha: str, **extra: object) -> dict:
    return {"against_head_sha": sha, "checked_at": "2026-10-28T00:00:00+00:00", **extra}


class TestCarriedFacts:
    def test_facts_from_the_diff_hold_at_the_same_patch(self):
        pr = _pr(analysis=_at("h1", disposition="merge"), security=_at("h1", verdict="GREEN"),
                 summary=_at("h1", schema_version=2), cluster=_at("h1"), verify=_at("h1"))
        assert all(freshness.is_current(pr, s)
                   for s in ("analysis", "security", "summary", "cluster", "verify"))
        assert freshness.stale_sections(pr) == []

    def test_facts_of_the_exact_commit_do_not_carry(self):
        pr = _pr(signals=_at("h1"), threat=_at("h1", verdict="clear"))
        assert not freshness.is_current(pr, "signals")
        assert not freshness.is_current(pr, "threat")

    def test_another_patch_reads_stale(self):
        pr = _pr(same_as=(), analysis=_at("h1", disposition="merge"))
        assert not freshness.is_current(pr, "analysis")

    def test_a_patch_stamped_at_an_older_head_carries_nothing(self):
        pr = _pr(head="h3", analysis=_at("h1", disposition="merge"))
        pr.rec["patch"]["against_head_sha"] = "h2"
        assert not freshness.is_current(pr, "analysis")

    def test_a_finding_anchored_to_a_line_keeps_the_fact_at_its_own_head(self):
        finding = {"title": "t", "location": "a.ts, hunk @@ -1,2 +1,3 @@", "severity": "yellow"}
        pr = _pr(security=_at("h1", verdict="YELLOW", findings=[finding]))
        assert not freshness.is_current(pr, "security")
        assert freshness.is_current(_pr(head="h1", security=_at("h1", verdict="YELLOW",
                                                                  findings=[finding])), "security")

    def test_an_uningested_push_reads_stale(self):
        pr = _pr(live="h3", analysis=_at("h1", disposition="merge"))
        assert not freshness.is_current(pr, "analysis")

    def test_the_design_review_holds_at_the_same_patch(self):
        entry = {"kind": reviewers.REVIEW, "reviewed_sha": "h1", "findings": [],
                 "extra": {"rubric": reviewers.DESIGN_RUBRIC}}
        assert review_policy.own_bar(_pr(reviews={"design": entry}),
                                     reviewers.DESIGN).status == reviewers.PASS
        assert review_policy.own_bar(_pr(same_as=(), reviews={"design": entry}),
                                     reviewers.DESIGN).status == reviewers.STALE

    def test_a_design_review_with_an_open_line_finding_holds_only_at_its_own_head(self):
        finding = {"path": "a.ts", "line": 12, "severity": "minor", "title": "t"}
        entry = {"kind": reviewers.REVIEW, "reviewed_sha": "h1", "findings": [finding],
                 "extra": {"rubric": reviewers.DESIGN_RUBRIC}}
        assert review_policy.own_bar(_pr(reviews={"design": entry}),
                                     reviewers.DESIGN).status == reviewers.STALE
        resolved = {**entry, "findings": [{**finding, "resolved": True}]}
        assert review_policy.own_bar(_pr(reviews={"design": resolved}),
                                     reviewers.DESIGN).status == reviewers.PASS
