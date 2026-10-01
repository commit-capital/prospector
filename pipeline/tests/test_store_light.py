from __future__ import annotations

import pytest

from pipeline import describe_pr
from pipeline import reviewers
from pipeline import storekit
from pipeline.store import LIGHT_CLIP_CHARS, Store, light_pr
from pipeline.testsupport import greptile_entry, reviews_section

NOW = "2026-06-10T00:00:00+00:00"
FINDING_BODY = "\n  The PR description is missing the Testing section.\n\nDetails follow " + "d" * 500


def _record(n: int = 1) -> dict:
    greptile = {**greptile_entry(3, "h1"), "summary": "s" * 1000,
                "findings": [{"path": "a.ts", "line": 1, "severity": None,
                              "title": "Missing section", "body": FINDING_BODY,
                              "resolved": False, "outdated": False, "commit": "h1",
                              "url": "u"},
                             {"path": "b.ts", "line": 2, "severity": None, "title": "Code",
                              "body": "a bug\nmore", "resolved": True, "outdated": False,
                              "commit": "h1", "url": "u"}]}
    return {"pr": n,
            "meta": {"title": f"t{n}", "author": "a", "state": "open", "draft": False,
                     "head_sha": "h1", "checked_at": NOW, "body": "the description"},
            "reviews": reviews_section("h1", NOW, greptile=greptile),
            "fix_request": {"status": "awaiting-review", "action": "resolve", "checked_at": NOW,
                            "against_head_sha": "h1",
                            "result": {"merge_diff": "m" * 5000, "conflict_paths": ["a.ts"]}}}


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path)
    s.save_pr(_record(1))
    s.save_pr(_record(2))
    return s


def test_light_copy_cuts_the_long_text(store):
    prs, stamps, high = store.prs_light()
    light = prs[1]
    entry = light.review_entry("greptile")
    assert len(entry["summary"]) == LIGHT_CLIP_CHARS
    assert entry["findings"][0]["body"] == "\n  The PR description is missing the Testing section."
    assert entry["findings"][1]["body"] == "a bug"
    assert len(light.fix_request["result"]["merge_diff"]) == LIGHT_CLIP_CHARS
    assert light.body is None
    assert set(stamps) == {1, 2} and high == max(stamps.values())


def test_light_copy_answers_what_the_gates_ask(store):
    whole = store.load_pr(1)
    light = store.prs_light()[0][1]
    for f_whole, f_light in zip(whole.review_entry("greptile")["findings"],
                                light.review_entry("greptile")["findings"], strict=True):
        assert describe_pr.is_description_nit(f_light) == describe_pr.is_description_nit(f_whole)
    assert reviewers.digests(light) == reviewers.digests(whole)
    assert light.fix_request["result"]["conflict_paths"] == ["a.ts"]


def test_a_light_copy_cannot_be_saved(store):
    light = store.prs_light()[0][1]
    with pytest.raises(storekit.ClippedWriteError):
        light.clear_cluster()
    carried = store.load_pr(2)
    with pytest.raises(storekit.ClippedWriteError):
        carried.set_reviews(dict(light.reviews or {}), head_sha="h1")
    assert store.load_pr(2).review_entry("greptile")["summary"] == "s" * 1000


def test_unclip_restores_the_whole_record(store):
    light = store.prs_light()[0]
    texts = store.pr_long_text()
    whole, _ = store.prs_since(None)
    for n in (1, 2):
        assert store.unclip_pr(light[n], texts[n][1]).raw == whole[n].raw
        assert not storekit.holds_clipped(store.unclip_pr(light[n], texts[n][1]).raw)


def test_light_pr_shares_sections_it_does_not_cut():
    rec = _record()
    out = light_pr(rec)
    assert out["meta"] is rec["meta"]
    assert rec["reviews"]["greptile"]["summary"] == "s" * 1000
