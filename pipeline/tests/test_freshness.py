"""Freshness: the ONE implementation of 'is this fact still about the current
code?'. A section is current iff it was computed against the PR's current
head_sha (and optionally within a max-age window)."""
from pipeline.freshness import (
    UNINGESTED_PUSH,
    SECTION_SCHEMA_VERSION,
    currency_failure,
    is_current,
    stale_sections,
    upstream_head_moved,
)
from pipeline.model import Pr


def _pr(head="abc123", live=None, **sections) -> Pr:
    meta = {"title": "t", "state": "open", "head_sha": head,
            "checked_at": "2026-06-10T00:00:00+00:00"}
    if live is not None:
        meta["live_head_sha"] = live
    rec = {"pr": 1, "meta": meta}
    rec.update(sections)
    return Pr(None, rec)


def _section(sha="abc123", at="2026-06-10T00:00:00+00:00", **extra):
    return {"checked_at": at, "against_head_sha": sha, **extra}


class TestIsCurrent:
    def test_current_when_sha_matches(self):
        pr = _pr(security=_section(verdict="GREEN"))
        assert is_current(pr, "security")

    def test_stale_when_head_moved(self):
        pr = _pr(head="NEW999", security=_section(sha="abc123"))
        assert not is_current(pr, "security")

    def test_missing_section_is_not_current(self):
        assert not is_current(_pr(), "security")

    def test_max_age_window(self):
        pr = _pr(security=_section(at="2026-06-01T00:00:00+00:00"))
        assert is_current(pr, "security", max_age_days=30, today="2026-06-10")
        assert not is_current(pr, "security", max_age_days=7, today="2026-06-10")

    def test_meta_section_never_sha_bound(self):
        # meta defines head_sha; it's current by existence (age check still applies)
        pr = _pr()
        assert is_current(pr, "meta")

    def test_cluster_stamp_current_distinguishes_standalone(self):
        # a clustering pass left this PR standalone: stamped at the current head,
        # no `id` → is_current is True even though there's no cluster membership.
        pr = _pr(cluster=_section())
        assert is_current(pr, "cluster")

    def test_cluster_stamp_stale_when_head_moved(self):
        # head moved since the pass → the stamp is stale, so the PR is "not yet
        # clustered (at this head)" and re-enters consideration.
        pr = _pr(head="NEW999", cluster=_section(sha="abc123"))
        assert not is_current(pr, "cluster")

    def test_missing_cluster_is_not_current(self):
        # never clustered: no stamp at all.
        assert not is_current(_pr(), "cluster")


class TestSchemaVersion:
    # A logic change to a classifier doesn't move head_sha, so a versioned
    # section also carries the schema_version it was produced under; a mismatch
    # is stale even when the sha still matches.
    def test_summary_current_when_version_matches(self):
        pr = _pr(summary=_section(schema_version=SECTION_SCHEMA_VERSION["summary"]))
        assert is_current(pr, "summary")

    def test_summary_stale_when_version_missing(self):
        # produced before versioning existed → no schema_version field
        pr = _pr(summary=_section(one_liner="x"))
        assert not is_current(pr, "summary")

    def test_summary_stale_when_version_older(self):
        pr = _pr(summary=_section(schema_version=SECTION_SCHEMA_VERSION["summary"] - 1))
        assert not is_current(pr, "summary")

    def test_unversioned_section_ignores_schema_version(self):
        # security carries no schema_version requirement; sha alone governs it
        pr = _pr(security=_section(verdict="GREEN"))
        assert is_current(pr, "security")


class TestCurrencyFailure:
    """The reason-bearing complement of is_current: same checks, same parameters,
    None exactly when is_current is True."""

    def test_current_is_none(self):
        pr = _pr(security=_section(verdict="GREEN"))
        assert currency_failure(pr, "security") is None

    def test_missing_section(self):
        assert currency_failure(_pr(), "security") == "missing"

    def test_head_moved(self):
        pr = _pr(head="NEW999", security=_section(sha="abc123"))
        assert "earlier head" in currency_failure(pr, "security")

    def test_outside_age_window_names_age_and_window(self):
        pr = _pr(security=_section(at="2026-06-01T00:00:00+00:00"))
        why = currency_failure(pr, "security", max_age_days=7, today="2026-06-10")
        assert why == "9d old, outside the 7d window"

    def test_within_age_window_is_none(self):
        pr = _pr(security=_section(at="2026-06-01T00:00:00+00:00"))
        assert currency_failure(pr, "security", max_age_days=30, today="2026-06-10") is None


class TestStaleSections:
    def test_lists_only_present_but_stale(self):
        pr = _pr(head="NEW999",
                 signals=_section(sha="abc123"),
                 analysis=_section(sha="NEW999"))
        assert stale_sections(pr) == ["signals"]


class TestEffectiveHead:
    """A live sweep records the head GitHub reports (meta.live_head_sha) without
    re-ingesting the diff behind it. Freshness tokens against that observed head,
    so a push nobody has ingested yet reads stale for every consumer."""

    def test_uningested_push_makes_a_matching_fact_stale(self):
        pr = _pr(head="abc123", live="NEW999", analysis=_section(sha="abc123"))
        assert not is_current(pr, "analysis")

    def test_live_head_equal_to_ingested_head_stays_current(self):
        pr = _pr(head="abc123", live="abc123", analysis=_section(sha="abc123"))
        assert is_current(pr, "analysis")

    def test_absent_live_head_falls_back_to_the_ingested_head(self):
        pr = _pr(head="abc123", analysis=_section(sha="abc123"))
        assert is_current(pr, "analysis")

    def test_one_push_stales_every_sha_bound_fact_at_once(self):
        pr = _pr(head="abc123", live="NEW999",
                 analysis=_section(sha="abc123"), security=_section(sha="abc123"),
                 verify=_section(sha="abc123"))
        assert stale_sections(pr) == ["analysis", "security", "verify"]

    def test_reingest_clearing_the_observation_heals_the_read(self):
        # ingest rebuilds meta at the new head, dropping live_head_sha; a fact
        # re-run against that head is current again with nothing else cleared.
        pr = _pr(head="NEW999", analysis=_section(sha="NEW999"))
        assert is_current(pr, "analysis")
        assert stale_sections(pr) == []

    def test_meta_is_never_sha_bound_so_a_push_leaves_it_current(self):
        pr = _pr(head="abc123", live="NEW999")
        assert is_current(pr, "meta")


class TestUpstreamHeadMoved:
    def test_true_when_the_sweep_saw_a_newer_head(self):
        assert upstream_head_moved(_pr(head="abc123", live="NEW999"))

    def test_false_when_the_observation_agrees(self):
        assert not upstream_head_moved(_pr(head="abc123", live="abc123"))

    def test_false_when_never_observed(self):
        assert not upstream_head_moved(_pr(head="abc123"))


class TestStaleReasons:
    """The two stale shapes need different operator responses, so they read
    differently: re-run the phase vs re-ingest first."""

    def test_uningested_push_names_the_push(self):
        pr = _pr(head="abc123", live="NEW999", analysis=_section(sha="abc123"))
        assert currency_failure(pr, "analysis") == UNINGESTED_PUSH

    def test_fact_older_than_the_ingested_head_still_reads_earlier_head(self):
        pr = _pr(head="abc123", live="NEW999", analysis=_section(sha="OLD000"))
        assert "earlier head" in currency_failure(pr, "analysis")

    def test_age_window_reason_survives_an_uningested_push(self):
        # the push is the token failure and reports first; with no push, the age
        # window is what fails and keeps its own phrasing.
        pr = _pr(head="abc123", analysis=_section(at="2026-06-01T00:00:00+00:00"))
        assert currency_failure(pr, "analysis", max_age_days=7,
                                today="2026-06-10") == "9d old, outside the 7d window"


class TestVerifyFreshness:
    def _pr(self, verify) -> Pr:
        return Pr(None, {"pr": 1,
                         "meta": {"title": "t", "state": "open", "head_sha": "h1"},
                         "verify": verify})

    def test_current_against_head(self):
        assert is_current(self._pr({"outcome": "verified-fix",
                                    "against_head_sha": "h1",
                                    "checked_at": "2026-06-10T00:00:00+00:00"}),
                          "verify", today="2026-06-10") is True

    def test_stale_when_author_pushed(self):
        assert is_current(self._pr({"outcome": "verified-fix",
                                    "against_head_sha": "OLD",
                                    "checked_at": "2026-06-10T00:00:00+00:00"}),
                          "verify", today="2026-06-10") is False
