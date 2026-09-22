"""action_items: structured operator/first-party worklist items that the
triage discovers (rotate a leaked secret, salvage a fix from a rejected PR,
notify upstream) — distinct from per-PR merge/close dispositions. Stored in
a durable store-level collection (store/action_items.json), surfaced in the
app. Mirrors the threats.json registry pattern."""
from pipeline import actions
from pipeline.store import Store, ValidationError

NOW = "2026-06-13"


def test_make_item_has_stable_id():
    a = actions.make_item("rotate-secret", pr=3994,
                          summary="Rotate leaked OBSIDIAN_API_KEY", created=NOW)
    b = actions.make_item("rotate-secret", pr=3994, summary="(different text)", created=NOW)
    assert a["id"] == b["id"] == "rotate-secret:3994"
    assert a["status"] == "open"


def test_rejects_unknown_kind():
    import pytest
    with pytest.raises(ValueError, match="kind"):
        actions.make_item("nuke-from-orbit", pr=1, summary="x", created=NOW)


class TestUpsert:
    def test_add_then_idempotent(self):
        reg = actions.empty_registry()
        it = actions.make_item("rotate-secret", pr=3994, summary="rotate key", created=NOW,
                               evidence="OBSIDIAN_API_KEY=…")
        actions.upsert(reg, it)
        actions.upsert(reg, actions.make_item("rotate-secret", pr=3994, summary="rotate key", created="2026-07-01"))
        assert len(reg["items"]) == 1
        # original created date preserved; not duplicated
        assert reg["items"][0]["created"] == NOW

    def test_upsert_preserves_done_status(self):
        reg = actions.empty_registry()
        actions.upsert(reg, actions.make_item("rotate-secret", pr=3994, summary="x", created=NOW))
        actions.set_status(reg, "rotate-secret:3994", "done")
        # a re-scan re-emits the same item; it must NOT reopen a done item
        actions.upsert(reg, actions.make_item("rotate-secret", pr=3994, summary="x", created=NOW))
        assert reg["items"][0]["status"] == "done"

    def test_distinct_kinds_same_pr_coexist(self):
        reg = actions.empty_registry()
        actions.upsert(reg, actions.make_item("rotate-secret", pr=10, summary="a", created=NOW))
        actions.upsert(reg, actions.make_item("salvage-fix", pr=10, summary="b", created=NOW))
        assert {i["id"] for i in reg["items"]} == {"rotate-secret:10", "salvage-fix:10"}


class TestLikelyFixture:
    def test_marker_text_reads_as_fixture(self):
        assert actions.likely_fixture("src/client.py: key = 'sk-ant-LEAKMARKER0123456789'")
        assert actions.likely_fixture("keys.txt: a pretend private key for the demo")

    def test_test_paths_read_as_fixture(self):
        assert actions.likely_fixture("tests/test_auth.py: AKIA1234567890ABCDEF")
        assert actions.likely_fixture("pkg/fixtures/creds.json: ghp_" + "a" * 36)
        assert actions.likely_fixture("src/auth.spec.ts: xoxb-1234567890-abcdefghij")

    def test_live_looking_leak_is_not_a_fixture(self):
        assert not actions.likely_fixture("src/config.py: AWS_KEY = 'AKIA1234567890ABCDEF'")
        assert not actions.likely_fixture("")

    def test_fixture_flag_travels_through_make_item_and_upsert(self):
        reg = actions.empty_registry()
        actions.upsert(reg, actions.make_item(
            "rotate-secret", pr=7, summary="x", created=NOW, fixture=True))
        assert reg["items"][0]["fixture"] is True
        # A re-scan that reads the evidence as live flips the mark in place.
        actions.upsert(reg, actions.make_item(
            "rotate-secret", pr=7, summary="x", created=NOW, fixture=False))
        assert reg["items"][0]["fixture"] is False
        # An item made without the flag carries no fixture key at all.
        assert "fixture" not in actions.make_item("review", pr=8, summary="y", created=NOW)


class TestStore:
    def test_round_trip(self, tmp_path):
        s = Store(tmp_path)
        assert s.load_action_items() == {"items": []}
        reg = actions.empty_registry()
        actions.upsert(reg, actions.make_item("rotate-secret", pr=3994, summary="x", created=NOW))
        s.save_action_items(reg)
        assert s.load_action_items()["items"][0]["id"] == "rotate-secret:3994"

    def test_save_validates_shape(self, tmp_path):
        s = Store(tmp_path)
        import pytest
        with pytest.raises(ValidationError):
            s.save_action_items({"items": "nope"})
