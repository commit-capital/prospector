"""gates.py — the ONE clean/merge policy. Replaces v1's three divergent
implementations (checks._tier0_is_clean, verdict.gate_decision, app
merge_concerns)."""
import pytest

from pipeline import gates, profile, settings
from pipeline.model import Cluster, Pr
from pipeline.store import Store


HEAD = "abc123"
NOW = "2026-06-10T00:00:00+00:00"


def _pr(**over) -> Pr:
    rec = {
        "pr": 1,
        "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                 "head_sha": HEAD, "checked_at": NOW},
        "signals": {"greptile": 5, "ci": "passing", "mergeable": True, "has_tests": True,
                    "checked_at": NOW, "against_head_sha": HEAD},
        "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": HEAD},
    }
    rec.update(over)
    return Pr(None, rec)


def _merge_analysis(**over):
    return dict({"disposition": "merge", "rationale": "r",
                 "checked_at": NOW, "against_head_sha": HEAD}, **over)


def _green(**over):
    return dict({"verdict": "GREEN", "findings": [], "override": None,
                 "checked_at": NOW, "against_head_sha": HEAD}, **over)


class TestSecurityDisposition:
    """The ONE security→disposition rule, applied at both the security commit and
    the ANALYZE merge-bar so a 'merge' pick can never coexist with a non-GREEN
    verdict."""

    def test_green_does_not_override(self):
        assert gates.security_disposition(_pr(security=_green())) is None

    def test_yellow_routes_to_request_changes(self):
        rec = _pr(security=_green(verdict="YELLOW",
                                  findings=[{"title": "unbounded retry loop"}]))
        disp, rationale = gates.security_disposition(rec)
        assert disp == "request-changes"
        assert "YELLOW" in rationale and "unbounded retry loop" in rationale

    def test_red_routes_to_needs_human(self):
        rec = _pr(security=_green(verdict="RED",
                                  findings=[{"title": "drops ownership check"}]))
        disp, rationale = gates.security_disposition(rec)
        assert disp == "needs-human"
        assert "RED" in rationale and "drops ownership check" in rationale

    def test_logged_override_does_not_route(self):
        rec = _pr(security=_green(verdict="YELLOW", override="accepted by human"))
        assert gates.security_disposition(rec) is None

    def test_stale_verdict_does_not_route(self):
        rec = _pr(security=_green(verdict="RED", against_head_sha="OLD"))
        assert gates.security_disposition(rec) is None

    def test_missing_verdict_is_none(self):
        assert gates.security_disposition(_pr()) is None


class TestPRClean:
    def test_clean_pr(self):
        ok, reasons = gates.pr_clean(_pr(), today="2026-06-10")
        assert ok and reasons == []

    def test_greptile_must_be_5(self):
        rec = _pr()
        rec.raw["signals"]["greptile"] = 4
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("greptile" in r for r in reasons)

    def test_none_provider_ignores_review(self, monkeypatch):
        # No provider configured: a PR with no review score is still clean.
        monkeypatch.setattr(settings, "REVIEW_PROVIDER", "none")
        rec = _pr()
        del rec.raw["signals"]["greptile"]
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert ok and reasons == []

    def test_ci_must_pass(self):
        rec = _pr()
        rec.raw["signals"]["ci"] = "failing"
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("ci" in r for r in reasons)

    def test_must_be_mergeable(self):
        rec = _pr()
        rec.raw["signals"]["mergeable"] = False
        rec.raw["drift"]["state"] = "conflicts"
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok

    def test_stale_signals_not_clean(self):
        rec = _pr()
        rec.raw["signals"]["against_head_sha"] = "OLD"
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("stale" in r for r in reasons)

    def test_closed_pr_not_clean(self):
        rec = _pr()
        rec.raw["meta"]["state"] = "merged"
        ok, _ = gates.pr_clean(rec, today="2026-06-10")
        assert not ok

    def test_secret_leak_not_clean(self):
        # A committed credential (MEDIUM → verdict "suspicious", not "malicious")
        # is never merged as-is, even though every other gate is green.
        rec = _pr(threat={"verdict": "suspicious", "signatures": ["secret-leak"]})
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("secret-leak" in r for r in reasons)

    def test_secret_leak_blocks_regardless_of_freshness(self):
        # Mirrors the malicious rule: a stale threat section still blocks; clearing
        # requires a re-scan, never silent staleness exemption.
        rec = _pr(threat={"verdict": "suspicious", "signatures": ["secret-leak"],
                          "against_head_sha": "OLD"})
        ok, reasons = gates.pr_clean(rec, today="2026-06-10")
        assert not ok and any("secret-leak" in r for r in reasons)


class TestSecurityEligible:
    def test_clean_merge_candidate_is_eligible(self):
        assert gates.security_eligible(_pr(analysis=_merge_analysis()), today="2026-06-10")

    def test_non_merge_disposition_not_eligible(self):
        rec = _pr(analysis=_merge_analysis(disposition="close-stale"))
        assert not gates.security_eligible(rec, today="2026-06-10")

    def test_dirty_pr_not_eligible(self):
        rec = _pr(analysis=_merge_analysis())
        rec.raw["signals"]["greptile"] = 3
        assert not gates.security_eligible(rec, today="2026-06-10")

    def test_stale_analysis_not_eligible(self):
        rec = _pr(analysis=_merge_analysis(against_head_sha="OLD"))
        assert not gates.security_eligible(rec, today="2026-06-10")


class TestMergeAllowed:
    def test_green_and_clean_allows(self):
        rec = _pr(analysis=_merge_analysis(), security=_green(), verify=_verified())
        ok, reason = gates.merge_allowed(rec, today="2026-06-10")
        assert ok

    def test_no_security_review_blocks(self):
        ok, reason = gates.merge_allowed(_pr(analysis=_merge_analysis()), today="2026-06-10")
        assert not ok and "security review missing" in reason

    def test_stale_security_blocks_and_says_head_moved(self):
        rec = _pr(analysis=_merge_analysis(), security=_green(against_head_sha="OLD"))
        ok, reason = gates.merge_allowed(rec, today="2026-06-10")
        assert not ok and "earlier head" in reason

    def test_old_security_reason_names_age_and_window(self):
        rec = _pr(analysis=_merge_analysis(),
                  security=_green(checked_at="2026-05-30T00:00:00+00:00"))
        ok, reason = gates.merge_allowed(rec, today="2026-06-10")
        assert not ok
        assert "11d old, outside the 7d window" in reason and "re-run SECURITY" in reason


class TestMergeEligibility:
    """The app's human-initiated merge gate: mergeable iff every check we
    actually ran passed. ANALYZE/SECURITY absence does not block."""

    def test_clean_unanalyzed_pr_is_eligible(self):
        # No analysis, no security review — a clean Easy-Lane PR still merges.
        ok, reason = gates.merge_eligibility(_pr(), today="2026-06-10")
        assert ok, reason

    def test_clean_unanalyzed_in_cluster_is_eligible(self):
        # Cluster membership is irrelevant for a clean PR (operator's choice).
        ok, _ = gates.merge_eligibility(_pr(cluster={"cluster_id": 7}), today="2026-06-10")
        assert ok

    def test_green_security_is_eligible(self):
        ok, _ = gates.merge_eligibility(_pr(security=_green()), today="2026-06-10")
        assert ok

    def test_greptile_below_5_blocks(self):
        rec = _pr()
        rec.raw["signals"]["greptile"] = 4
        ok, reason = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok and "greptile" in reason

    def test_ci_failing_blocks(self):
        rec = _pr()
        rec.raw["signals"]["ci"] = "failing"
        ok, reason = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok and "ci" in reason

    def test_stale_head_blocks(self):
        rec = _pr()
        rec.raw["signals"]["against_head_sha"] = "OLD"
        ok, reason = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok and "stale" in reason

    def test_conflicts_block(self):
        rec = _pr()
        rec.raw["signals"]["mergeable"] = False
        rec.raw["drift"]["state"] = "conflicts"
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok

    def test_malicious_blocks(self):
        rec = _pr(threat={"verdict": "malicious", "signatures": ["x"]})
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok

    def test_secret_leak_blocks(self):
        rec = _pr(threat={"verdict": "suspicious", "signatures": ["secret-leak"]})
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok

    def test_security_yellow_blocks(self):
        rec = _pr(security=_green(verdict="YELLOW"))
        ok, reason = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok and "security" in reason

    def test_security_red_blocks(self):
        rec = _pr(security=_green(verdict="RED"))
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10")
        assert not ok

    def test_security_yellow_with_override_is_eligible(self):
        rec = _pr(security=_green(verdict="YELLOW", override="logged: cold path"))
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10")
        assert ok

    def test_security_yellow_with_operator_reason_is_eligible(self):
        rec = _pr(security=_green(verdict="YELLOW"))
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10",
                                        override_reason="mirrors master's stdout ignore list")
        assert ok

    def test_blank_operator_reason_does_not_clear_yellow(self):
        rec = _pr(security=_green(verdict="YELLOW"))
        ok, _ = gates.merge_eligibility(rec, today="2026-06-10", override_reason="   ")
        assert not ok

    def test_operator_reason_never_clears_red(self):
        rec = _pr(security=_green(verdict="RED"))
        ok, reason = gates.merge_eligibility(rec, today="2026-06-10",
                                             override_reason="i am very sure")
        assert not ok and "RED" in reason

    def test_codeowners_path_blocks(self):
        # An otherwise-clean PR touching a code-owner-gated path is a hard block.
        ok, reason = gates.merge_eligibility(_pr(), today="2026-06-10",
                                             changed_paths=[".github/workflows/ci.yml"])
        assert not ok and "code owner" in reason

    def test_non_codeowners_path_is_eligible(self):
        ok, _ = gates.merge_eligibility(_pr(), today="2026-06-10",
                                        changed_paths=["src/app.ts"])
        assert ok

    def test_old_security_review_blocks(self):
        rec = _pr(analysis=_merge_analysis(),
                  security=_green(checked_at="2026-05-01T00:00:00+00:00"))
        ok, reason = gates.merge_allowed(rec, today="2026-06-10")
        assert not ok and "security" in reason

    def test_yellow_without_override_blocks(self):
        rec = _pr(analysis=_merge_analysis(), security=_green(verdict="YELLOW"))
        ok, reason = gates.merge_allowed(rec, today="2026-06-10")
        assert not ok and "YELLOW" in reason

    def test_yellow_with_override_allows(self):
        rec = _pr(analysis=_merge_analysis(),
                  security=_green(verdict="YELLOW",
                                  override={"reason": "cosmetic", "by": "tester", "date": "2026-06-10"}),
                  verify=_verified())
        ok, _ = gates.merge_allowed(rec, today="2026-06-10")
        assert ok

    def test_non_merge_disposition_blocks(self):
        rec = _pr(analysis=_merge_analysis(disposition="needs-human"), security=_green())
        ok, reason = gates.merge_allowed(rec, today="2026-06-10")
        assert not ok


class TestBlockedOnSecurity:
    """True iff a clean merge PR is blocked solely because its security review is
    missing/stale/>7d — i.e. re-running SECURITY is exactly what unblocks merge.
    Mirrors the security-currency branch of merge_allowed (no changed_paths, the
    way suggest.py calls it)."""

    def test_missing_security_is_blocked_on_security(self):
        rec = _pr(analysis=_merge_analysis())
        assert gates.blocked_on_security(rec, today="2026-06-10")

    def test_stale_head_security_is_blocked_on_security(self):
        rec = _pr(analysis=_merge_analysis(), security=_green(against_head_sha="OLD"))
        assert gates.blocked_on_security(rec, today="2026-06-10")

    def test_old_security_is_blocked_on_security(self):
        rec = _pr(analysis=_merge_analysis(),
                  security=_green(checked_at="2026-05-01T00:00:00+00:00"))
        assert gates.blocked_on_security(rec, today="2026-06-10")

    def test_current_green_is_not_blocked(self):
        rec = _pr(analysis=_merge_analysis(), security=_green())
        assert not gates.blocked_on_security(rec, today="2026-06-10")

    def test_not_clean_is_not_blocked_on_security(self):
        # Greptile < 5 is the blocker, not security — re-running SECURITY won't help.
        rec = _pr(analysis=_merge_analysis())
        rec.raw["signals"]["greptile"] = 4
        assert not gates.blocked_on_security(rec, today="2026-06-10")

    def test_non_merge_disposition_is_not_blocked_on_security(self):
        rec = _pr(analysis=_merge_analysis(disposition="request-changes"))
        assert not gates.blocked_on_security(rec, today="2026-06-10")

    def test_stale_analysis_is_not_blocked_on_security(self):
        # The blocker is a stale ANALYZE, routed to re-cluster — not SECURITY.
        rec = _pr(analysis=_merge_analysis(against_head_sha="OLD"))
        assert not gates.blocked_on_security(rec, today="2026-06-10")


class TestClusterState:
    def _cluster(self, prs, outcome="merge-ready"):
        rec = {"id": 1, "root_problem": "x", "prs": [r.n for r in prs],
               "outcome": outcome, "checked_at": NOW}
        return Cluster(None, rec)

    def test_needs_analysis_when_no_outcome(self):
        pr = _pr()
        c = self._cluster([pr], outcome=None)
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "needs-analysis"

    def test_outcome_passthrough_states(self):
        pr = _pr(analysis=_merge_analysis(disposition="needs-human"))
        for outcome in ("awaiting-authors", "needs-first-party-work", "blocked-on-decision"):
            c = self._cluster([pr], outcome=outcome)
            assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == outcome

    def test_merge_ready_without_security_is_security_pending(self):
        pr = _pr(analysis=_merge_analysis())
        c = self._cluster([pr])
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "security-pending"

    def test_merge_ready_with_green_security_but_no_verify_is_security_pending(self):
        # Security alone no longer clears the bar — verified-fix is also required.
        pr = _pr(analysis=_merge_analysis(), security=_green())
        c = self._cluster([pr])
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "security-pending"

    def test_merge_ready_with_green_and_verified_is_ready(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(), verify=_verified())
        c = self._cluster([pr])
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "ready"

    def test_close_out_is_ready_without_security(self):
        pr = _pr(analysis=_merge_analysis(disposition="close-stale"))
        c = self._cluster([pr], outcome="close-out")
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "ready"

    def test_stale_analysis_returns_to_needs_analysis(self):
        pr = _pr(analysis=_merge_analysis(against_head_sha="OLD"))
        c = self._cluster([pr])
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "needs-analysis"

    def test_merge_ready_with_a_needs_human_pick_is_blocked_on_decision(self):
        # The plan says merge, the pick is routed needs-human: a person has to make
        # the call, so the cluster is a judgment call, not ready.
        pr = _pr(analysis=_merge_analysis(disposition="needs-human"),
                 security=_green(verdict="RED", findings=[{"title": "authz bypass"}]))
        c = self._cluster([pr])
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "blocked-on-decision"

    def test_merge_ready_with_a_request_changes_pick_is_awaiting_authors(self):
        # The plan says merge, the pick is routed request-changes: the asks are on
        # the author.
        pr = _pr(analysis=_merge_analysis(disposition="request-changes"),
                 security=_green(verdict="YELLOW", findings=[{"title": "weak check"}]))
        c = self._cluster([pr])
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") == "awaiting-authors"

    def test_a_needs_human_member_outranks_a_request_changes_member(self):
        # Most-blocking wins, matching DISPOSITION_PRECEDENCE.
        human = _pr(analysis=_merge_analysis(disposition="needs-human"))
        author = _pr(analysis=_merge_analysis(disposition="request-changes"))
        author.raw["pr"] = 2
        c = self._cluster([human, author])
        state = gates.cluster_state(c, {1: human, 2: author}, today="2026-06-10")
        assert state == "blocked-on-decision"

    def test_a_surviving_merge_pick_still_gates_on_security(self):
        # One pick demoted, another still routed merge → the merge bar decides.
        demoted = _pr(analysis=_merge_analysis(disposition="needs-human"))
        survivor = _pr(analysis=_merge_analysis(), security=_green(), verify=_verified())
        survivor.raw["pr"] = 2
        c = self._cluster([demoted, survivor])
        state = gates.cluster_state(c, {1: demoted, 2: survivor}, today="2026-06-10")
        assert state == "ready"

    def test_merge_ready_with_only_closes_left_is_ready(self):
        # The merge landed upstream; the duplicate it superseded is still open and
        # still needs closing — an action waiting on the operator, not on anyone else.
        merged = _pr(analysis=_merge_analysis())
        merged.raw["meta"]["state"] = "merged"
        dup = _pr(analysis=_merge_analysis(disposition="close-dup"))
        dup.raw["pr"] = 2
        c = self._cluster([merged, dup])
        assert gates.cluster_state(c, {1: merged, 2: dup}, today="2026-06-10") == "ready"

    def test_surfaces_all_draft_cluster(self):
        # An all-draft cluster has an active member (the draft) → not "done".
        pr = _pr(meta={"title": "t", "author": "a", "state": "open", "draft": True,
                       "head_sha": HEAD, "checked_at": NOW})
        c = self._cluster([pr], outcome=None)
        assert gates.cluster_state(c, {1: pr}, today="2026-06-10") != "done"


def test_reconcile_disposition_most_blocking_wins():
    proposals = [
        {"pr": 1, "disposition": "merge", "cluster_id": 10},
        {"pr": 1, "disposition": "close-dup", "cluster_id": 20},
    ]
    assert gates.reconcile_disposition(proposals)["disposition"] == "close-dup"


def test_reconcile_disposition_full_order():
    order = ["needs-human", "close-dup", "close-fixed", "close-stale",
             "request-changes", "merge"]
    for i, hi in enumerate(order):
        for lo in order[i + 1:]:
            picked = gates.reconcile_disposition(
                [{"disposition": lo, "cluster_id": 1},
                 {"disposition": hi, "cluster_id": 2}])
            assert picked["disposition"] == hi, f"{hi} should beat {lo}"


def test_reconcile_disposition_ties_break_to_lower_cluster_id():
    picked = gates.reconcile_disposition(
        [{"disposition": "merge", "cluster_id": 30, "rationale": "b"},
         {"disposition": "merge", "cluster_id": 10, "rationale": "a"}])
    assert picked["cluster_id"] == 10 and picked["rationale"] == "a"


def test_reconcile_disposition_none_when_empty():
    assert gates.reconcile_disposition([]) is None
    assert gates.reconcile_disposition([{"disposition": "bogus", "cluster_id": 1}]) is None


class TestIsDependabotBump:
    def test_lockfile_and_manifest_bump(self):
        assert gates.is_dependabot_bump(
            "dependabot[bot]", ["pnpm-lock.yaml", "package.json"])

    def test_workflow_bump(self):
        assert gates.is_dependabot_bump(
            "dependabot[bot]", [".github/workflows/pr.yml"])

    def test_nested_manifest(self):
        assert gates.is_dependabot_bump(
            "dependabot[bot]", ["frontend/package-lock.json"])

    def test_non_dependabot_author_never_matches(self):
        assert not gates.is_dependabot_bump("alice", ["pnpm-lock.yaml"])
        assert not gates.is_dependabot_bump("renovate[bot]", ["pnpm-lock.yaml"])

    def test_profile_automation_bots(self, monkeypatch):
        # The exempt-author list is profile policy data, not a hard-coded literal.
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(automation_bots=("renovate[bot]",)))
        assert gates.is_dependabot_bump("renovate[bot]", ["package.json"])
        assert not gates.is_dependabot_bump("dependabot[bot]", ["package.json"])

    def test_profile_dependency_manifests(self, monkeypatch):
        # A profile manifest list replaces the generic set (basename match).
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(dependency_manifests=("deps.lock",)))
        assert gates.is_dependabot_bump("dependabot[bot]", ["deps.lock"])
        assert gates.is_dependabot_bump("dependabot[bot]", ["sub/deps.lock"])
        assert not gates.is_dependabot_bump("dependabot[bot]", ["package.json"])

    def test_profile_manifest_glob_matches_full_path(self, monkeypatch):
        # A manifest entry with a "/" matches the whole path as a glob.
        monkeypatch.setattr(profile, "active",
                            lambda: profile.RepoProfile(dependency_manifests=("deps/*.lock",)))
        assert gates.deps_touched(["deps/a.lock"]) is True
        assert gates.deps_touched(["other/a.lock"]) is False

    def test_generic_manifests_cover_other_ecosystems(self):
        # The generic default is cross-ecosystem, not a pnpm-only list.
        assert gates.is_dependabot_bump("dependabot[bot]", ["Cargo.toml", "go.sum"])

    def test_workflow_passes_exemption_but_is_not_a_manifest(self):
        # Workflow files are automation surface: dependabot regenerates them, so
        # they pass the bump exemption, but they never count as a dependency
        # manifest for the VERIFY refusal.
        assert gates.is_dependabot_bump("dependabot[bot]", [".github/workflows/ci.yml"])
        assert gates.deps_touched([".github/workflows/ci.yml"]) is False

    def test_touching_source_code_fails_the_shape_guard(self):
        # the security guard: a dependabot PR that edits anything beyond a bump
        # does NOT inherit the exemption.
        assert not gates.is_dependabot_bump(
            "dependabot[bot]", ["pnpm-lock.yaml", "src/app.ts"])
        assert not gates.is_dependabot_bump(
            "dependabot[bot]", [".github/workflows/pr.yml", "cli/esbuild.config.mjs"])

    def test_empty_or_unknown_paths_fail_closed(self):
        assert not gates.is_dependabot_bump("dependabot[bot]", [])
        assert not gates.is_dependabot_bump("dependabot[bot]", None)


class TestSecurityOverridable:
    """security_overridable marks the ONE state where an operator reason unblocks
    merge_eligibility: a current YELLOW verdict with no logged override."""

    def test_yellow_without_override_is_overridable(self):
        rec = _pr(security=_green(verdict="YELLOW"))
        assert gates.security_overridable(rec, today="2026-06-10")

    def test_green_gate_passes_so_not_overridable(self):
        assert not gates.security_overridable(_pr(security=_green()), today="2026-06-10")

    def test_red_is_not_overridable(self):
        rec = _pr(security=_green(verdict="RED"))
        assert not gates.security_overridable(rec, today="2026-06-10")

    def test_logged_override_already_clears_so_not_overridable(self):
        rec = _pr(security=_green(verdict="YELLOW",
                                  override={"reason": "cosmetic", "by": "b", "at": NOW}))
        assert not gates.security_overridable(rec, today="2026-06-10")

    def test_unclean_yellow_is_not_overridable(self):
        # CI failing blocks regardless of any reason, so a reason doesn't help.
        rec = _pr(security=_green(verdict="YELLOW"))
        rec.raw["signals"]["ci"] = "failing"
        assert not gates.security_overridable(rec, today="2026-06-10")

    def test_codeowners_yellow_is_not_overridable(self):
        rec = _pr(security=_green(verdict="YELLOW"))
        assert not gates.security_overridable(
            rec, today="2026-06-10", changed_paths=[".github/workflows/ci.yml"])


class TestDepsTouched:
    """Never install PR-controlled dependencies: a PR touching a dependency
    manifest or lockfile is refused before a sandbox boots."""

    def test_root_manifest(self):
        assert gates.deps_touched(["package.json"]) is True

    def test_nested_manifest(self):
        assert gates.deps_touched(["packages/core/package.json"]) is True

    def test_lockfile_and_workspace(self):
        assert gates.deps_touched(["pnpm-lock.yaml"]) is True
        assert gates.deps_touched(["pnpm-workspace.yaml"]) is True

    def test_cross_ecosystem_manifest(self):
        assert gates.deps_touched(["poetry.lock"]) is True

    def test_nested_lockfile(self):
        assert gates.deps_touched(["packages/x/pnpm-lock.yaml"]) is True

    def test_workflow_file_is_not_deps(self):
        assert gates.deps_touched([".github/workflows/ci.yml"]) is False

    def test_ordinary_source_is_not_deps(self):
        assert gates.deps_touched(["src/a.ts", "src/a.test.ts"]) is False

    def test_lookalike_filename_is_not_deps(self):
        assert gates.deps_touched(["docs/package.json.md"]) is False


def _blind(**over) -> dict:
    return dict({"has_test": True, "test_cmd": "pnpm -s test x.test.ts",
                 "faithful": True, "requires_live_agent": False,
                 "expected_red_signature": "AssertionError: expected 2",
                 "repro_command": None}, **over)


def _host(**over) -> dict:
    # A confirmed-clean run by default: the first red->green is clean AND the
    # confirm re-run agrees. Tests that exercise a flaky/incomplete confirm
    # override the *_exit_confirm fields.
    return dict({"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                 "red_exit_confirm": 20, "green_exit_confirm": 0}, **over)


def _judge(**over) -> dict:
    return dict({"red_reason_match": dict(
        {"matches": True, "confidence": "high"}, **over)})


def _regress(**over) -> dict:
    return dict({"ran": True, "exit_first": 0, "exit_confirm": None,
                 "confirmed": False, "flake": False, "excluded_count": 9,
                 "new_failures": []}, **over)


def _authored(**over) -> dict:
    # A validated, confirmed-clean authored-test run by default.
    return dict({"attempted": True, "can_author": True,
                 "test_cmd": "npx vitest run x.test.ts",
                 "red_exit": 20, "green_exit": 0,
                 "red_exit_confirm": 20, "green_exit_confirm": 0}, **over)


class TestVerifyOutcome:
    """The ONE VERIFY outcome policy. The judgment agent emits no outcome —
    it is computed here from the blind verdict, the host-observed exit codes,
    and the judge's red-reason rating."""

    def test_all_signals_agree_is_verified_fix(self):
        assert gates.verify_outcome(
            _blind(), _host(), _judge(), regress=_regress()) == "verified-fix"

    def test_live_agent_requirement_short_circuits(self):
        # Tier 2 is out of scope; nothing else is evaluated.
        assert gates.verify_outcome(
            _blind(requires_live_agent=True, test_cmd=None), _host(), _judge(),
            regress=None
        ) == "unverifiable-needs-live-agent"

    def test_no_test_command_is_unverifiable(self):
        assert gates.verify_outcome(
            _blind(has_test=False, test_cmd=None), _host(), _judge(), regress=None
        ) == "unverifiable-no-test"

    def test_patch_conflict_is_needs_rebase(self):
        assert gates.verify_outcome(
            _blind(), _host(apply_exit=30), _judge(), regress=None) == "needs-rebase"

    def test_no_test_hunks_in_the_diff_is_unverifiable(self):
        # #551: has_test claimed a PR-authored test, but the diff carried no test
        # hunk for red to apply — no legitimate red was ever possible.
        null = {"apply_exit": 0, "red_exit": None, "green_exit": None,
                "no_test_hunks": True}
        assert gates.verify_outcome(
            _blind(), null, _judge(), regress=None) == "unverifiable-no-test"
        assert gates.verify_outcome(_blind(), null, None, regress=None) == "unverifiable-no-test"

    def test_red_that_passed_on_main_is_not_verified(self):
        # The author's test does not reproduce anything on pinned main.
        assert gates.verify_outcome(
            _blind(), _host(red_exit=0), _judge(), regress=None) == "not-verified"

    def test_green_that_failed_is_not_verified(self):
        assert gates.verify_outcome(
            _blind(), _host(green_exit=20), _judge(), regress=None) == "not-verified"

    def test_blind_disagreement_escalates_and_agent_cannot_resolve(self):
        # THE rule: the blind verdict says the test is unfaithful, yet it goes
        # clean red->green. A judge insisting the reason matches cannot override.
        assert gates.verify_outcome(
            _blind(faithful=False), _host(), _judge(matches=True, confidence="high"),
            regress=_regress()
        ) == "escalate"

    def test_red_for_the_wrong_reason_is_not_verified(self):
        assert gates.verify_outcome(
            _blind(), _host(), _judge(matches=False), regress=_regress()) == "not-verified"

    def test_low_confidence_red_reason_escalates(self):
        assert gates.verify_outcome(
            _blind(), _host(), _judge(confidence="low"), regress=_regress()) == "escalate"

    def test_a_flaky_red_escalates(self):
        # First run clean (red 20, green 0), but the confirm red PASSED — the
        # reproduction is nondeterministic, so it is not a reliable verified-fix.
        assert gates.verify_outcome(
            _blind(), _host(red_exit_confirm=0), _judge(), regress=_regress()
        ) == "escalate"

    def test_a_flaky_green_escalates(self):
        # First run clean, but the confirm green FAILED — nondeterministic.
        assert gates.verify_outcome(
            _blind(), _host(green_exit_confirm=20), _judge(), regress=_regress()
        ) == "escalate"

    def test_a_clean_red_green_without_a_confirm_run_holds(self):
        # A clean first run with no confirm re-run is an incomplete run — hold
        # (fail-closed) rather than verify unconfirmed.
        host = {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
                "red_exit_confirm": None, "green_exit_confirm": None}
        assert gates.verify_outcome(_blind(), host, _judge(), regress=_regress()) is None

    def test_a_non_sentinel_confirm_holds(self):
        # The confirm re-run died (OOM/timeout) — its exit carries no meaning, so
        # hold rather than read it as agreement or disagreement.
        assert gates.verify_outcome(
            _blind(), _host(red_exit_confirm=137), _judge(), regress=_regress()) is None

    def test_non_sentinel_exit_holds(self):
        # 137 (killed) must never read as a red. None == hold, write nothing.
        assert gates.verify_outcome(
            _blind(), _host(red_exit=137), _judge(), regress=None) is None
        assert gates.verify_outcome(
            _blind(), _host(green_exit=1), _judge(), regress=None) is None
        assert gates.verify_outcome(
            _blind(), _host(apply_exit=137), _judge(), regress=None) is None

    def test_missing_judge_holds(self):
        assert gates.verify_outcome(_blind(), _host(), None, regress=_regress()) is None

    def test_blind_disagreement_escalates_with_missing_judge(self):
        # THE rule again, minus a judge: the blind-unfaithful check runs
        # before judge inspection and wins regardless.
        assert gates.verify_outcome(
            _blind(faithful=False), _host(), None, regress=_regress()
        ) == "escalate"

    def test_judge_with_no_usable_rating_holds(self):
        # A judge dict with no red_reason_match, or an empty one, carries no
        # actual bool for `matches`: the run holds.
        assert gates.verify_outcome(_blind(), _host(), {}, regress=_regress()) is None
        assert gates.verify_outcome(
            _blind(), _host(), {"red_reason_match": {}}, regress=_regress()
        ) is None

    def test_repro_reason_mismatch_does_not_prevent_verified_fix(self):
        # Signal 4 (the agent's independent repro) is corroborating evidence,
        # not a gate: a repro that failed for the wrong reason (a timeout, an
        # import error) does not itself block verified-fix.
        judge = dict(_judge(), repro_reason_match={
            "applicable": True, "matches": False, "confidence": "high"})
        assert gates.verify_outcome(
            _blind(), _host(), judge, regress=_regress()) == "verified-fix"

    def test_repro_reason_match_true_does_not_change_the_outcome(self):
        judge = dict(_judge(), repro_reason_match={
            "applicable": True, "matches": True, "confidence": "high"})
        assert gates.verify_outcome(
            _blind(), _host(), judge, regress=_regress()) == "verified-fix"

    def test_every_outcome_is_in_the_vocabulary(self):
        for blind, host, judge in (
            (_blind(), _host(), _judge()),
            (_blind(requires_live_agent=True), _host(), _judge()),
            (_blind(test_cmd=None), _host(), _judge()),
            (_blind(), _host(apply_exit=30), _judge()),
            (_blind(), _host(red_exit=0), _judge()),
            (_blind(faithful=False), _host(), _judge()),
            (_blind(), _host(red_exit=None, green_exit=None, no_test_hunks=True), _judge()),
        ):
            assert gates.verify_outcome(
                blind, host, judge, regress=_regress()) in gates.VERIFY_OUTCOMES

    def test_confirmed_regress_blocks_a_would_be_verified_fix(self):
        assert gates.verify_outcome(
            _blind(), _host(), _judge(),
            regress=_regress(exit_first=20, exit_confirm=20, confirmed=True,
                             new_failures=["src/x.test.ts"])) == "regressed"

    def test_a_flake_does_not_block(self):
        assert gates.verify_outcome(
            _blind(), _host(), _judge(),
            regress=_regress(exit_first=20, exit_confirm=0, flake=True)
        ) == "verified-fix"

    def test_escalate_outranks_regressed(self):
        # A blind-unfaithful clean red->green stays the human's, even regressed.
        assert gates.verify_outcome(
            _blind(faithful=False), _host(), _judge(),
            regress=_regress(exit_first=20, exit_confirm=20, confirmed=True)
        ) == "escalate"

    def test_clean_red_green_with_no_regress_leg_holds(self):
        # Fail-closed: a clean red->green that never ran the suite is an
        # incomplete run, and an incomplete run re-runs rather than passing.
        assert gates.verify_outcome(_blind(), _host(), _judge(), regress=None) is None
        assert gates.verify_run_errored(_blind(), _host(), regress=None) is True

    def test_a_non_sentinel_regress_exit_holds(self):
        r = _regress(exit_first=137)
        assert gates.verify_outcome(_blind(), _host(), _judge(), regress=r) is None
        assert gates.verify_run_errored(_blind(), _host(), regress=r) is True

    def test_an_unconfirmed_first_failure_holds(self):
        # exit_first 20 with no confirming run recorded is an interrupted
        # confirmation, not a verdict.
        r = _regress(exit_first=20, exit_confirm=None)
        assert gates.verify_outcome(_blind(), _host(), _judge(), regress=r) is None
        assert gates.verify_run_errored(_blind(), _host(), regress=r) is True

    def test_a_skipped_regress_on_a_dirty_red_green_is_not_an_error(self):
        host = _host(red_exit=0)
        skipped = {"ran": False, "skipped_reason": "red-green-not-clean"}
        assert gates.verify_run_errored(_blind(), host, regress=skipped) is False
        assert gates.verify_outcome(_blind(), host, _judge(), regress=skipped) == "not-verified"

    def test_a_no_suite_config_skip_is_a_complete_run(self):
        # The pin declared the repository has no full-suite contract: the
        # driver's deliberate skip settles a clean confirmed red->green as
        # verified-fix rather than holding forever on a suite that cannot run.
        skipped = {"ran": False, "skipped_reason": "no-suite-config"}
        assert gates.verify_run_errored(_blind(), _host(), regress=skipped) is False
        assert gates.verify_outcome(_blind(), _host(), _judge(),
                                    regress=skipped) == "verified-fix"

    def test_any_other_skip_reason_on_a_clean_run_holds(self):
        skipped = {"ran": False, "skipped_reason": "mystery"}
        assert gates.verify_run_errored(_blind(), _host(), regress=skipped) is True
        assert gates.verify_outcome(_blind(), _host(), _judge(), regress=skipped) is None

    def test_regressed_never_reaches_the_unverifiable_paths(self):
        assert gates.verify_outcome(
            _blind(has_test=False, test_cmd=None), _host(), _judge(),
            regress=None) == "unverifiable-no-test"


def _dirty_host(**over) -> dict:
    # A contained dirty green by default: red fails on the target AND a
    # contaminating test, green (both runs) fails only on the contaminant, and
    # the contaminant is not a test the diff introduces.
    base: dict = {
        "green_exit": 20, "green_exit_confirm": 20,
        "red_failing": ["a.test.ts > suite > target", "a.test.ts > suite > contam"],
        "green_failing": ["a.test.ts > suite > contam"],
        "green_failing_confirm": ["a.test.ts > suite > contam"],
        "failing_in_diff": []}
    base.update(over)
    return _host(**base)


class TestDirtyGreenContainment:
    """The dirty-green contamination exemption (#3718, #3368): a green that
    fails only on tests that already failed red — none of them introduced by
    the diff — is accepted as if it passed. Every fact missing, malformed, or
    outside the containment fails closed to today's behavior."""

    def test_contained_dirty_green_is_verified_fix(self):
        assert gates.verify_outcome(
            _blind(), _dirty_host(), _judge(), regress=_regress()) == "verified-fix"

    def test_contained_first_run_with_clean_confirm_is_verified_fix(self):
        # The contaminant flaked away on the confirm green — a fully clean
        # confirm agrees with a contained first run.
        host = _dirty_host(green_exit_confirm=0)
        del host["green_failing_confirm"]
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "verified-fix"

    def test_the_judge_still_gates_a_contained_green(self):
        assert gates.verify_outcome(
            _blind(), _dirty_host(), _judge(matches=False),
            regress=_regress()) == "not-verified"

    def test_blind_unfaithful_still_escalates_a_contained_green(self):
        assert gates.verify_outcome(
            _blind(faithful=False), _dirty_host(), _judge(),
            regress=_regress()) == "escalate"

    def test_a_confirmed_regress_still_blocks_a_contained_green(self):
        assert gates.verify_outcome(
            _blind(), _dirty_host(), _judge(),
            regress=_regress(exit_first=20, exit_confirm=20, confirmed=True)
        ) == "regressed"

    def test_identical_red_and_green_failing_sets_are_not_contained(self):
        # Nothing flipped: the green set must be a PROPER subset of red's.
        host = _dirty_host(
            green_failing=["a.test.ts > suite > target", "a.test.ts > suite > contam"],
            green_failing_confirm=["a.test.ts > suite > target",
                                   "a.test.ts > suite > contam"])
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_a_test_failing_only_in_green_is_not_contained(self):
        host = _dirty_host(green_failing=["a.test.ts > suite > brand-new-failure"])
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_a_diff_introduced_test_failing_in_green_is_not_contained(self):
        # The PR's own (added or retitled) test still fails with the fix applied.
        host = _dirty_host(failing_in_diff=["a.test.ts > suite > contam"])
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_an_unparsable_green_is_not_contained(self):
        host = _dirty_host(green_failing=None)
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_an_unparsable_red_is_not_contained(self):
        host = _dirty_host(red_failing=None)
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_a_missing_in_diff_fact_is_not_contained(self):
        # The diff could not be read at run time, so the diff-membership check
        # never happened — fail closed.
        host = _dirty_host(failing_in_diff=None)
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_an_empty_green_failing_set_is_not_contained(self):
        # Exit 20 with a report of zero failures is self-contradictory output.
        host = _dirty_host(green_failing=[])
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "not-verified"

    def test_a_record_without_parsed_sets_stays_not_verified(self):
        # The exact shape of the pre-existing #3718/#3368 records: both legs
        # exited 20 with no parsed facts. Nothing changes for them until re-run.
        assert gates.verify_outcome(
            _blind(), _host(green_exit=20), _judge(), regress=None) == "not-verified"

    def test_a_contained_run_without_a_confirm_holds(self):
        host = _dirty_host(red_exit_confirm=None, green_exit_confirm=None)
        assert gates.verify_outcome(_blind(), host, _judge(), regress=_regress()) is None
        assert gates.verify_run_errored(_blind(), host, regress=_regress()) is True

    def test_a_confirm_green_outside_the_red_set_escalates(self):
        # The confirm green failed on a test red never failed on — the runs
        # disagree, exactly like a flaky green.
        host = _dirty_host(
            green_failing_confirm=["a.test.ts > suite > brand-new-failure"])
        assert gates.verify_outcome(
            _blind(), host, _judge(), regress=_regress()) == "escalate"

    def test_a_contained_run_without_a_regress_leg_holds(self):
        assert gates.verify_outcome(_blind(), _dirty_host(), _judge(), regress=None) is None
        assert gates.verify_run_errored(_blind(), _dirty_host(), regress=None) is True

    def test_green_accepted_requires_a_red_that_failed(self):
        # Containment is meaningless without a host-observed red failure.
        host = _dirty_host(red_exit=0)
        assert gates.green_accepted(host) is False

    def test_green_accepted_on_a_clean_green_needs_no_facts(self):
        assert gates.green_accepted(_host()) is True

    def test_contained_green_failures_names_the_contamination(self):
        assert gates.contained_green_failures(_dirty_host()) == [
            "a.test.ts > suite > contam"]

    def test_contained_green_failures_is_empty_for_a_clean_green(self):
        assert gates.contained_green_failures(_host()) == []

    def test_contained_green_failures_is_empty_when_not_contained(self):
        host = _dirty_host(green_failing=["a.test.ts > suite > brand-new-failure"])
        assert gates.contained_green_failures(host) == []

    def test_a_contained_verified_fix_is_partial_evidence(self):
        # merge_allowed refuses to auto-recommend it; merge_eligibility stays
        # open but names the contamination (same posture as the repro gaps).
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(signals={"red_green": _dirty_host()}))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "also failed" in why
        ok, reason = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "incomplete" in reason
        ok, reason = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True and "incomplete" in reason

    def test_a_clean_verified_fix_is_still_complete_evidence(self):
        pr = _pr(verify=_verified(signals={"red_green": _host()}))
        assert gates.verify_signals_incomplete(pr) is None


class TestVerifyRunErrored:
    """The ONE definition of the VERIFY hold. verify_outcome returns None on it,
    so the PR is not settled and a re-queue runs it again."""

    def test_a_killed_container_errored(self):
        assert gates.verify_run_errored(_blind(), _host(red_exit=137), regress=None) is True
        assert gates.verify_run_errored(_blind(), _host(green_exit=1), regress=None) is True
        assert gates.verify_run_errored(_blind(), _host(apply_exit=137), regress=None) is True

    def test_a_timeout_errored(self):
        assert gates.verify_run_errored(_blind(), _host(red_exit=124), regress=None) is True

    def test_an_unrecorded_phase_errored(self):
        assert gates.verify_run_errored(
            _blind(), {"apply_exit": 0, "red_exit": 20, "green_exit": None},
            regress=None) is True

    def test_a_clean_red_green_did_not_error(self):
        assert gates.verify_run_errored(_blind(), _host(), regress=_regress()) is False

    def test_a_red_that_passed_on_main_did_not_error(self):
        # 0 is a sentinel: an honest "the test passed on main", not an error.
        assert gates.verify_run_errored(_blind(), _host(red_exit=0), regress=None) is False

    def test_a_patch_conflict_did_not_error(self):
        # 30 is host-authoritative needs-rebase — a real result.
        assert gates.verify_run_errored(_blind(), _host(apply_exit=30), regress=None) is False

    def test_a_blind_verdict_that_asked_for_no_run_did_not_error(self):
        # No test to run and no live agent available: the outcome follows from the
        # blind verdict alone, so the absent exit codes are not a failed run.
        null = {"apply_exit": None, "red_exit": None, "green_exit": None}
        assert gates.verify_run_errored(
            _blind(has_test=False, test_cmd=None), null, regress=None) is False
        assert gates.verify_run_errored(
            _blind(requires_live_agent=True), null, regress=None) is False

    def test_no_test_hunks_in_the_diff_did_not_error(self):
        # #551: apply-check ran, but red/green never did — by policy, not failure.
        host = {"apply_exit": 0, "red_exit": None, "green_exit": None,
                "no_test_hunks": True}
        assert gates.verify_run_errored(_blind(), host, regress=None) is False

    def test_it_agrees_with_verify_outcome_on_every_errored_run_hold(self):
        # The two must never disagree: whenever the host facts error, no outcome
        # follows however the judge rates the output.
        for host in (_host(red_exit=137), _host(green_exit=1), _host(apply_exit=137),
                     _host(red_exit=124)):
            assert gates.verify_run_errored(_blind(), host, regress=None) is True
            for judge in (_judge(), _judge(matches=False), _judge(confidence="low"), None):
                assert gates.verify_outcome(_blind(), host, judge, regress=None) is None
            # An errored run outranks even the escalate rule.
            assert gates.verify_outcome(
                _blind(faithful=False), host, _judge(), regress=None) is None


class TestVacuousNameFilter:
    """A red that exited 0 while the test_cmd carries a name filter is the
    signature of a filter that matched no test names (#7524: the it.each
    template title never equals a rendered name, so vitest skipped all 11
    tests and exited 0 on both red and green). Diagnostic only — the outcome
    stays whatever verify_outcome computes."""

    def test_the_7524_shape_is_flagged(self):
        blind = _blind(test_cmd='npx vitest run x.test.ts -t "release preserves status"')
        assert gates.vacuous_name_filter(blind, _host(red_exit=0)) == "-t"

    def test_every_filter_spelling_is_recognized(self):
        for cmd, flag in (
                ('npx vitest run f.ts -t "name"', "-t"),
                ("npx vitest run f.ts --testNamePattern=name", "--testNamePattern"),
                ('npx vitest run f.ts --testNamePattern "name"', "--testNamePattern"),
                ("node --test --test-name-pattern name", "--test-name-pattern"),
                ('npx mocha f.js --grep "name"', "--grep"),
                ('npx mocha f.js -g "name"', "-g")):
            got = gates.vacuous_name_filter(_blind(test_cmd=cmd), _host(red_exit=0))
            assert got == flag, cmd

    def test_a_whole_file_run_is_not_flagged(self):
        for cmd in ("pnpm -s test x.test.ts",             # -s is not -t
                    "npx vitest run src/x-t.test.ts",     # -t inside a path
                    "npx vitest run --timeout=5000 x.test.ts"):
            assert gates.vacuous_name_filter(
                _blind(test_cmd=cmd), _host(red_exit=0)) is None, cmd

    def test_a_genuine_red_is_not_flagged(self):
        blind = _blind(test_cmd='npx vitest run f.ts -t "name"')
        assert gates.vacuous_name_filter(blind, _host(red_exit=20)) is None

    def test_an_errored_red_is_not_flagged(self):
        blind = _blind(test_cmd='npx vitest run f.ts -t "name"')
        assert gates.vacuous_name_filter(blind, _host(red_exit=137)) is None

    def test_a_null_test_cmd_is_not_flagged(self):
        assert gates.vacuous_name_filter(
            _blind(test_cmd=None), _host(red_exit=0)) is None

    def test_the_outcome_is_unchanged_by_the_flag(self):
        # The flag is a finding, never a verdict: a filtered vacuous red is
        # not-verified exactly like an unfiltered one.
        blind = _blind(test_cmd='npx vitest run f.ts -t "name"')
        assert gates.verify_outcome(
            blind, _host(red_exit=0), _judge(), regress=None) == "not-verified"


def _repro(**over) -> dict:
    # A repro that ran and exited as failing — the exit that reads as a
    # successful reproduction of the bug on unfixed main.
    return dict({"ran": True, "exit_code": 20, "output_tail": ""}, **over)


class TestVacuousPathFilter:
    """A repro that exited as failing while its command mixes --config/--root
    with a path filter that repeats the rebased root is the signature of a
    filter that matched no files (#9041: `--config server/vitest.config.ts`
    rebased vitest's root to server/, so the filter `server/src/...` matched
    nothing, vitest exited nonzero on "No test files found", and the host
    exit read as a reproduction). Diagnostic only, from trusted inputs alone —
    the pre-committed repro_command and the host-observed exit."""

    R9041 = ('npx vitest run --config server/vitest.config.ts '
             'server/src/__tests__/config-file.test.ts -t "field-specific error" '
             '--testTimeout=10000')

    def test_the_9041_shape_is_flagged(self):
        blind = _blind(repro_command=self.R9041)
        assert gates.vacuous_path_filter(blind, _repro()) == \
            "server/src/__tests__/config-file.test.ts"

    def test_every_rebase_spelling_is_recognized(self):
        for cmd in (
                "npx vitest run --config server/vitest.config.ts server/src/x.test.ts",
                "npx vitest run --config=server/vitest.config.ts server/src/x.test.ts",
                "npx vitest run --root server server/src/x.test.ts",
                "npx vitest run --root=server server/src/x.test.ts",
                "npx vitest run --config ./server/vitest.config.ts server/src/x.test.ts"):
            got = gates.vacuous_path_filter(_blind(repro_command=cmd), _repro())
            assert got == "server/src/x.test.ts", cmd

    def test_a_filter_relative_to_the_rebased_root_is_not_flagged(self):
        cmd = "npx vitest run --config server/vitest.config.ts src/x.test.ts"
        assert gates.vacuous_path_filter(_blind(repro_command=cmd), _repro()) is None

    def test_a_repo_root_config_rebases_nothing(self):
        cmd = "npx vitest run --config vitest.config.ts server/src/x.test.ts"
        assert gates.vacuous_path_filter(_blind(repro_command=cmd), _repro()) is None

    def test_a_command_without_a_rebase_flag_is_not_flagged(self):
        cmd = "npx vitest run server/src/x.test.ts"
        assert gates.vacuous_path_filter(_blind(repro_command=cmd), _repro()) is None

    def test_a_name_filter_value_is_not_a_path_filter(self):
        # The -t value is the flag's argument, not a path filter — even when it
        # happens to start with the rebased root's name.
        cmd = 'npx vitest run --config server/vitest.config.ts -t "server/thing"'
        assert gates.vacuous_path_filter(_blind(repro_command=cmd), _repro()) is None

    def test_a_passing_repro_is_not_flagged(self):
        blind = _blind(repro_command=self.R9041)
        assert gates.vacuous_path_filter(blind, _repro(exit_code=0)) is None

    def test_an_errored_repro_is_not_flagged(self):
        blind = _blind(repro_command=self.R9041)
        assert gates.vacuous_path_filter(blind, _repro(exit_code=137)) is None

    def test_a_repro_that_never_ran_is_not_flagged(self):
        blind = _blind(repro_command=self.R9041)
        assert gates.vacuous_path_filter(
            blind, _repro(ran=False, exit_code=None)) is None

    def test_a_null_repro_command_is_not_flagged(self):
        assert gates.vacuous_path_filter(_blind(), _repro()) is None

    def test_an_unparseable_command_is_not_flagged(self):
        blind = _blind(repro_command='npx vitest run --config server/v.ts "unbalanced')
        assert gates.vacuous_path_filter(blind, _repro()) is None


# A PR diff that adds a test file (with its test titles in added lines) and
# modifies a source file — the shape whose tests/titles a structurally vacuous
# repro targets.
_DIFF_ADDS_TEST = (
    "diff --git a/server/src/__tests__/config-file.test.ts "
    "b/server/src/__tests__/config-file.test.ts\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/server/src/__tests__/config-file.test.ts\n"
    "@@ -0,0 +1,2 @@\n"
    '+describe("config file", () => {\n'
    '+  it("surfaces a field-specific error", () => {});\n'
    "diff --git a/server/src/config-file.ts b/server/src/config-file.ts\n"
    "--- a/server/src/config-file.ts\n"
    "+++ b/server/src/config-file.ts\n"
    "@@ -1 +1,2 @@\n"
    "+export const y = 2;\n"
)


class TestReproTargetsPrTest:
    """A repro_command that names a test the diff itself introduces is
    structurally vacuous: the repro phase runs the pinned base with no patch
    applied, so the target cannot exist there (#9041: the command's path was a
    file the diff adds; #7936: the -t value was a test title the diff adds).
    The check reads the pre-committed command and the PR's diff, and a match
    only ever skips a run."""

    def test_the_9041_shape_a_path_the_diff_adds_is_flagged(self):
        cmd = ('npx vitest run --config server/vitest.config.ts '
               'server/src/__tests__/config-file.test.ts -t "field-specific error"')
        assert gates.repro_targets_pr_test(cmd, _DIFF_ADDS_TEST) == \
            "server/src/__tests__/config-file.test.ts"

    def test_the_7936_shape_a_workspace_rooted_path_still_matches(self):
        # The command's path is written relative to a workspace package; it is
        # the same file the diff touches, matched as a "/"-bounded suffix.
        cmd = ("pnpm --filter @x/server exec vitest run "
               "src/__tests__/config-file.test.ts")
        assert gates.repro_targets_pr_test(cmd, _DIFF_ADDS_TEST) == \
            "src/__tests__/config-file.test.ts"

    def test_a_name_filter_naming_an_added_test_title_is_flagged(self):
        cmd = 'npx vitest run --testTimeout=5000 -t "field-specific error"'
        assert gates.repro_targets_pr_test(cmd, _DIFF_ADDS_TEST) == \
            "field-specific error"

    def test_a_name_filter_naming_a_preexisting_test_is_not_flagged(self):
        cmd = 'npx vitest run server/src/other.test.ts -t "loads defaults"'
        assert gates.repro_targets_pr_test(cmd, _DIFF_ADDS_TEST) is None

    def test_a_repro_distinct_from_the_prs_test_is_not_flagged(self):
        for cmd in ("node scripts/repro.mjs",
                    'npx vitest run src/other.test.ts -t "existing behavior"'):
            assert gates.repro_targets_pr_test(cmd, _DIFF_ADDS_TEST) is None, cmd

    def test_a_changed_non_test_path_is_not_flagged(self):
        # Referencing a source file the diff modifies is fine — the base's
        # version exists and exercising it is exactly what a repro does.
        cmd = "node server/src/config-file.ts"
        assert gates.repro_targets_pr_test(cmd, _DIFF_ADDS_TEST) is None

    def test_a_title_added_only_in_a_source_hunk_is_not_flagged(self):
        # The phrase appears in an added line of a NON-test file; only added
        # test lines count as introduced test titles.
        diff = ("diff --git a/server/src/config-file.ts b/server/src/config-file.ts\n"
                "--- a/server/src/config-file.ts\n"
                "+++ b/server/src/config-file.ts\n"
                "@@ -1 +1,2 @@\n"
                "+// handles the field-specific error case\n")
        cmd = 'npx vitest run -t "field-specific error"'
        assert gates.repro_targets_pr_test(cmd, diff) is None

    def test_an_unparseable_command_is_not_flagged(self):
        assert gates.repro_targets_pr_test(
            'npx vitest run "unbalanced', _DIFF_ADDS_TEST) is None


class TestAuthoredLaneOutcome:
    """The no-test lane: an agent-authored test is corroborating evidence only.
    Every non-clean lane result settles to unverifiable-no-test — the lane
    never holds and never escalates — and a clean confirmed run with a
    matching red reason is agent-verified."""

    NO_TEST = {"has_test": False, "test_cmd": None, "faithful": False,
               "requires_live_agent": False}
    LANE_HOST = {"apply_exit": 0, "red_exit": None, "green_exit": None}

    def test_clean_confirmed_run_is_agent_verified(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(), regress=_regress(),
            authored=_authored()) == "agent-verified"

    def test_no_authored_section_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, None, regress=None,
            authored=None) == "unverifiable-no-test"

    def test_validation_skip_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, None, regress=None,
            authored=_authored(test_cmd=None, skipped_reason="path-not-a-test-path")
        ) == "unverifiable-no-test"

    def test_red_that_passed_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(), regress=None,
            authored=_authored(red_exit=0)) == "unverifiable-no-test"

    def test_green_that_failed_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(), regress=None,
            authored=_authored(green_exit=20)) == "unverifiable-no-test"

    def test_infra_error_settles_not_holds(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, None, regress=None,
            authored=_authored(red_exit=137, green_exit=None)
        ) == "unverifiable-no-test"

    def test_flaky_confirm_settles_not_escalates(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(), regress=None,
            authored=_authored(red_exit_confirm=0)) == "unverifiable-no-test"

    def test_wrong_reason_red_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(matches=False), regress=None,
            authored=_authored()) == "unverifiable-no-test"

    def test_low_confidence_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(confidence="low"), regress=None,
            authored=_authored()) == "unverifiable-no-test"

    def test_missing_judge_stays_unverifiable(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, None, regress=None,
            authored=_authored()) == "unverifiable-no-test"

    def test_confirmed_regression_is_regressed(self):
        assert gates.verify_outcome(
            self.NO_TEST, self.LANE_HOST, _judge(),
            regress=_regress(exit_first=20, exit_confirm=20, confirmed=True),
            authored=_authored()) == "regressed"

    def test_pr_patch_conflict_is_needs_rebase(self):
        assert gates.verify_outcome(
            self.NO_TEST, {"apply_exit": 30}, None, regress=None,
            authored=_authored()) == "needs-rebase"

    def test_live_agent_still_short_circuits(self):
        blind = dict(self.NO_TEST, requires_live_agent=True)
        assert gates.verify_outcome(
            blind, self.LANE_HOST, _judge(), regress=None,
            authored=_authored()) == "unverifiable-needs-live-agent"

    def test_agent_verified_is_a_valid_outcome(self):
        assert "agent-verified" in gates.VERIFY_OUTCOMES

    def test_agent_verified_forces_no_disposition(self):
        assert gates.verify_disposition(
            _pr(verify=_verified(outcome="agent-verified"))) is None


def _verified(**over) -> dict:
    return dict({"outcome": "verified-fix", "signals": {}, "findings": [], "tier": 0,
                 "against_base_sha": "base1", "checked_at": NOW,
                 "against_head_sha": HEAD}, **over)


class TestVerifyEligible:
    """VERIFY runs on GATE-clean merge candidates only — the same wave rule as
    SECURITY — plus a parseable diff, which is what makes deps_touched safe."""

    def test_clean_merge_candidate_is_eligible(self):
        pr = _pr(analysis=_merge_analysis())
        assert gates.verify_eligible(pr, ["src/a.ts"], today="2026-06-10") is True

    def test_diffless_pr_is_never_eligible(self):
        # An unparseable diff must not read as "touches no dependency files".
        pr = _pr(analysis=_merge_analysis())
        assert gates.verify_eligible(pr, [], today="2026-06-10") is False

    def test_non_merge_disposition_is_not_eligible(self):
        pr = _pr(analysis=_merge_analysis(disposition="close-stale"))
        assert gates.verify_eligible(pr, ["src/a.ts"], today="2026-06-10") is False

    def test_dirty_pr_is_not_eligible(self):
        pr = _pr(analysis=_merge_analysis(),
                 signals={"greptile": 4, "ci": "passing", "mergeable": True,
                          "checked_at": NOW, "against_head_sha": HEAD})
        assert gates.verify_eligible(pr, ["src/a.ts"], today="2026-06-10") is False

    def test_deps_touching_pr_is_still_eligible_so_the_driver_can_record_it(self):
        # verify_eligible does not filter deps-touching PRs; deps_touched is the
        # separate check the driver uses to route them.
        pr = _pr(analysis=_merge_analysis())
        assert gates.verify_eligible(pr, ["package.json"], today="2026-06-10") is True


class TestVerifyMergeBar:
    """verified-fix is the fifth bar element in merge_allowed; merge_eligibility
    blocks only on a verify that RAN, mirroring how it treats SECURITY."""

    def test_merge_allowed_requires_a_verify(self):
        pr = _pr(analysis=_merge_analysis(), security=_green())
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "VERIFY" in why

    def test_merge_allowed_with_verified_fix(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(), verify=_verified())
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is True

    def test_merge_allowed_rejects_unverifiable(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(outcome="unverifiable-no-test"))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "unverifiable-no-test" in why

    def test_merge_allowed_rejects_a_pending_blind_only_verify(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(outcome=None))
        ok, _ = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False

    def test_merge_allowed_rejects_stale_verify(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(against_head_sha="OLD"))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "VERIFY" in why

    def test_merge_eligibility_never_run_verify_does_not_block(self):
        ok, _ = gates.merge_eligibility(_pr(), today="2026-06-10")
        assert ok is True

    def test_merge_eligibility_blocks_a_verify_that_ran_and_failed(self):
        pr = _pr(verify=_verified(outcome="not-verified"))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is False and "not-verified" in why

    def test_merge_eligibility_passes_verified_fix(self):
        ok, _ = gates.merge_eligibility(_pr(verify=_verified()), today="2026-06-10")
        assert ok is True

    def test_agent_verified_is_eligible_with_provenance_reason(self):
        pr = _pr(verify=_verified(outcome="agent-verified"))
        ok, reason = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True
        assert "agent-authored" in reason

    def test_agent_verified_does_not_satisfy_merge_allowed(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(outcome="agent-verified"))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "agent-verified" in why

    def test_escalate_blocks_without_an_override(self):
        pr = _pr(verify=_verified(outcome="escalate"))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is False and "escalate" in why

    def test_escalate_with_an_override_reason_is_eligible(self):
        # escalate = "a human must decide"; an operator reason clears it, exactly
        # like a YELLOW security verdict.
        pr = _pr(verify=_verified(outcome="escalate"))
        ok, _ = gates.merge_eligibility(pr, today="2026-06-10",
                                        override_reason="read the red output; the test is fine")
        assert ok is True

    def test_a_logged_verify_override_clears_escalate(self):
        pr = _pr(verify=_verified(outcome="escalate",
                                  override={"reason": "checked by hand", "by": "op"}))
        ok, _ = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True

    def test_not_verified_is_never_overridable(self):
        # only escalate is a judgment call; not-verified/regressed are "the PR
        # must change" and stay hard blocks even with a reason.
        for outcome in ("not-verified", "regressed", "needs-rebase"):
            pr = _pr(verify=_verified(outcome=outcome))
            ok, _ = gates.merge_eligibility(pr, today="2026-06-10",
                                            override_reason="please just merge it")
            assert ok is False, outcome

    def test_verify_overridable_marks_only_a_reason_clearable_escalate(self):
        assert gates.verify_overridable(
            _pr(verify=_verified(outcome="escalate")), today="2026-06-10")
        # a verified-fix needs no override
        assert not gates.verify_overridable(
            _pr(verify=_verified()), today="2026-06-10")
        # a not-verified is not reason-clearable
        assert not gates.verify_overridable(
            _pr(verify=_verified(outcome="not-verified")), today="2026-06-10")
        # an already-logged override is not "still overridable"
        assert not gates.verify_overridable(
            _pr(verify=_verified(outcome="escalate",
                                 override={"reason": "x", "by": "op"})), today="2026-06-10")

    def test_security_overridable_does_not_fire_on_a_verify_escalate(self):
        # disambiguation: an escalate-only PR (GREEN security) is verify-
        # overridable, NOT security-overridable — the reason must be logged to
        # the right section.
        pr = _pr(security=_green(), verify=_verified(outcome="escalate"))
        assert gates.verify_overridable(pr, today="2026-06-10")
        assert not gates.security_overridable(pr, today="2026-06-10")

    def test_merge_eligibility_ignores_a_blind_only_verify(self):
        # Between commit-blind and commit-dir the section is current but carries no
        # verdict. No verification has concluded, so nothing blocks the operator.
        ok, why = gates.merge_eligibility(_pr(verify=_verified(outcome=None)),
                                          today="2026-06-10")
        assert ok is True
        assert "None" not in why

    def test_merge_eligibility_ignores_a_held_verify(self):
        # The run errored, so no outcome was ever committed. Same rule: a
        # verification that reached no verdict is a verification that did not run.
        pr = _pr(verify=_verified(outcome=None, signals={
            "blind_adequacy": {"test_cmd": "pnpm -s test"},
            "red_green": {"apply_exit": 0, "red_exit": 137, "green_exit": None}}))
        ok, _ = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True

    def test_merge_eligibility_ignores_a_stale_verify_that_did_not_confirm(self):
        pr = _pr(verify=_verified(outcome="not-verified", against_head_sha="OLD"))
        ok, _ = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True

    def test_merge_allowed_never_names_a_null_outcome_to_the_operator(self):
        # merge_allowed's reason is the app's "Merge blocked" card text.
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(outcome=None))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False
        assert "None" not in why and "VERIFY" in why

    def test_merge_allowed_rejects_regressed(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(outcome="regressed"))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "regressed" in why

    def test_merge_eligibility_blocks_regressed(self):
        pr = _pr(verify=_verified(outcome="regressed"))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is False and "regressed" in why


class TestVerifyDisposition:
    """The ONE verify→disposition rule, applied at both the verify commit and any
    merge pick, so a 'merge' can never coexist with a blocking outcome."""

    def test_escalate_routes_to_needs_human(self):
        disp, why = gates.verify_disposition(_pr(verify=_verified(outcome="escalate")))
        assert disp == "needs-human" and "escalated" in why

    def test_escalate_with_a_lane_infra_error_names_the_lane(self):
        # A lane that hit an infrastructure exit is anomalous infrastructure,
        # not evidence about the PR — the rationale must say so and name it.
        pr = _pr(verify=_verified(
            outcome="escalate",
            signals={"lanes": {"build": {"cmd": "b", "exit": 137, "ok": False}}}))
        disp, why = gates.verify_disposition(pr)
        assert disp == "needs-human"
        assert "build" in why and "infrastructure" in why

    def test_deps_touched_routes_to_needs_human(self):
        disp, _ = gates.verify_disposition(_pr(verify=_verified(outcome="deps-touched")))
        assert disp == "needs-human"

    def test_regressed_forces_request_changes(self):
        route = gates.verify_disposition(_pr(verify=_verified(outcome="regressed")))
        assert route is not None
        assert route[0] == "request-changes" and "regression" in route[1]

    def test_regressed_with_a_failed_lane_names_the_lane(self):
        pr = _pr(verify=_verified(
            outcome="regressed",
            signals={"lanes": {"compile": {"cmd": "c", "exit": 20, "ok": False}}}))
        route = gates.verify_disposition(pr)
        assert route is not None
        assert route[0] == "request-changes"
        assert "compile" in route[1]

    def test_not_verified_routes_to_request_changes(self):
        disp, _ = gates.verify_disposition(_pr(verify=_verified(outcome="not-verified")))
        assert disp == "request-changes"

    def test_needs_rebase_routes_to_request_changes(self):
        disp, _ = gates.verify_disposition(_pr(verify=_verified(outcome="needs-rebase")))
        assert disp == "request-changes"

    def test_verified_fix_does_not_force(self):
        assert gates.verify_disposition(_pr(verify=_verified())) is None

    def test_the_unverifiable_outcomes_do_not_force(self):
        for outcome in ("unverifiable-no-test", "unverifiable-needs-live-agent"):
            assert gates.verify_disposition(_pr(verify=_verified(outcome=outcome))) is None

    def test_a_null_outcome_does_not_force(self):
        assert gates.verify_disposition(_pr(verify=_verified(outcome=None))) is None

    def test_a_stale_outcome_does_not_force(self):
        assert gates.verify_disposition(
            _pr(verify=_verified(outcome="escalate", against_head_sha="OLD"))) is None

    def test_a_missing_section_does_not_force(self):
        assert gates.verify_disposition(_pr()) is None


class TestForcedDisposition:
    """The composition of the two blocking facts. Every merge pick passes through
    it, so a `merge` disposition can never coexist with either."""

    def test_nothing_forces_a_clean_pr(self):
        assert gates.forced_disposition(_pr(security=_green(), verify=_verified())) is None

    def test_security_alone_forces(self):
        disp, why = gates.forced_disposition(
            _pr(security=_green(verdict="RED", findings=[{"title": "authz bypass"}])))
        assert disp == "needs-human" and "authz bypass" in why

    def test_verify_alone_forces(self):
        disp, why = gates.forced_disposition(_pr(verify=_verified(outcome="not-verified")))
        assert disp == "request-changes" and "Dynamic verification" in why

    def test_the_more_blocking_of_the_two_wins(self):
        # YELLOW is request-changes; escalate is needs-human.
        disp, why = gates.forced_disposition(
            _pr(security=_green(verdict="YELLOW", findings=[{"title": "weak check"}]),
                verify=_verified(outcome="escalate")))
        assert disp == "needs-human" and "Dynamic verification escalated" in why

    def test_the_more_blocking_of_the_two_wins_the_other_way(self):
        # RED is needs-human; not-verified is request-changes.
        disp, why = gates.forced_disposition(
            _pr(security=_green(verdict="RED", findings=[{"title": "authz bypass"}]),
                verify=_verified(outcome="not-verified")))
        assert disp == "needs-human" and "authz bypass" in why

    def test_a_tie_breaks_to_security(self):
        disp, why = gates.forced_disposition(
            _pr(security=_green(verdict="RED", findings=[{"title": "authz bypass"}]),
                verify=_verified(outcome="escalate")))
        assert disp == "needs-human" and "authz bypass" in why

    def test_it_only_ever_returns_a_ranked_disposition(self):
        for security in (_green(), _green(verdict="RED", findings=[]),
                         _green(verdict="YELLOW", findings=[])):
            for outcome in sorted(gates.VERIFY_OUTCOMES) + [None]:
                route = gates.forced_disposition(
                    _pr(security=security, verify=_verified(outcome=outcome)))
                if route is not None:
                    assert route[0] in gates.DISPOSITION_PRECEDENCE


class TestClusterStateAfterSelfDemotion:
    """The board chip for a merge-ready cluster whose merge pick self-demoted.

    A blocking fact lands after ANALYZE proposed the plan, and every mutator that
    can leave a `merge` disposition routes through gates.forced_disposition — so
    the PR demotes itself while the cluster keeps its stored `merge-ready`
    outcome. Driven through the real store mutators on both fact paths, since the
    chip is what tells the operator a cluster is safe to action."""

    def _seed(self, path) -> Store:
        store = Store(path)
        store.save_pr({
            "pr": 1,
            "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                     "head_sha": HEAD, "checked_at": NOW},
            "signals": {"greptile": 5, "ci": "passing", "mergeable": True,
                        "checked_at": NOW, "against_head_sha": HEAD},
            "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": HEAD},
        })
        store.save_cluster({"id": 1, "root_problem": "x", "prs": [1],
                            "outcome": "merge-ready", "checked_at": NOW})
        store.edit_pr(1).route_to("merge", "best of the cluster", from_cluster=1)
        return store

    def _state(self, store: Store) -> tuple[str, str, str]:
        pr = store.load_pr(1)
        cluster = store.load_cluster(1)
        return (pr.disposition, cluster.outcome,
                gates.cluster_state(cluster, {1: pr}, today="2026-06-10"))

    @pytest.mark.parametrize("verdict,expected", [
        ("RED", "blocked-on-decision"),
        ("YELLOW", "awaiting-authors"),
    ])
    def test_a_security_verdict_demotion_leaves_no_green_chip(self, tmp_path, verdict, expected):
        store = self._seed(tmp_path)
        store.edit_pr(1).record_security(verdict, [{"title": "authz bypass"}])
        disposition, outcome, state = self._state(store)
        assert disposition != "merge"
        assert outcome == "merge-ready"      # the stored plan still claims a merge
        assert state == expected

    @pytest.mark.parametrize("outcome_recorded,expected", [
        ("escalate", "blocked-on-decision"),
        ("not-verified", "awaiting-authors"),
    ])
    def test_a_verify_outcome_demotion_leaves_no_green_chip(self, tmp_path, outcome_recorded,
                                                            expected):
        store = self._seed(tmp_path)
        store.edit_pr(1).record_verify(outcome_recorded, {"host": {}}, base_sha="base1")
        disposition, outcome, state = self._state(store)
        assert disposition != "merge"
        assert outcome == "merge-ready"
        assert state == expected

    def test_a_reanalyzed_cluster_does_not_go_green_again(self, tmp_path):
        # The SECURITY driver reopens the cluster on RED, then ANALYZE re-runs. Its
        # bundle carries no security section, so the agent re-proposes merge-ready
        # and route_to demotes the pick a second time.
        store = self._seed(tmp_path)
        store.edit_pr(1).record_security("RED", [{"title": "authz bypass"}])
        store.edit_cluster(1).set_outcome(None)
        assert self._state(store)[2] == "needs-analysis"

        store.edit_cluster(1).set_outcome("merge-ready")
        store.edit_pr(1).route_to("merge", "best of the cluster", from_cluster=1)
        disposition, _, state = self._state(store)
        assert disposition == "needs-human"
        assert state == "blocked-on-decision"


def _repro_signals(*, repro_command: str | None = "node --test repro.mjs",
                   ran: bool = True, rating: dict | None = None) -> dict:
    signals: dict = {"blind_adequacy": {"repro_command": repro_command},
                     "independent_repro": {"ran": ran,
                                           "exit_code": 20 if ran else None}}
    if rating is not None:
        signals["repro_reason_match"] = rating
    return signals


class TestVerifySignalsIncomplete:
    """An attempted signal that does not corroborate makes the verified evidence
    partial: merge_allowed refuses to auto-recommend it, and merge_eligibility
    stays open but names the gap (operator decision, 2026-07-16 — #7524's repro
    never executed and hid behind a clean verified-fix)."""

    def test_no_repro_authored_is_complete(self):
        pr = _pr(verify=_verified(signals={"blind_adequacy": {"repro_command": None}}))
        assert gates.verify_signals_incomplete(pr) is None

    def test_a_corroborating_repro_is_complete(self):
        pr = _pr(verify=_verified(signals=_repro_signals(
            rating={"matches": True, "applicable": True, "confidence": "high"})))
        assert gates.verify_signals_incomplete(pr) is None

    def test_a_repro_that_never_ran_is_incomplete(self):
        pr = _pr(verify=_verified(signals=_repro_signals(ran=False)))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "never ran" in why

    def test_a_wrong_reason_repro_is_incomplete(self):
        pr = _pr(verify=_verified(signals=_repro_signals(
            rating={"matches": False, "applicable": True, "confidence": "high"})))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "did not corroborate" in why

    def test_an_unrated_repro_is_incomplete(self):
        pr = _pr(verify=_verified(signals=_repro_signals()))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "never rated" in why

    def test_merge_allowed_refuses_partial_evidence(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(signals=_repro_signals(
                     rating={"matches": False, "applicable": True,
                             "confidence": "high"})))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "incomplete" in why

    def test_merge_allowed_passes_complete_evidence(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(signals=_repro_signals(
                     rating={"matches": True, "applicable": True,
                             "confidence": "high"})))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is True, why

    def test_merge_eligibility_stays_open_but_names_the_gap(self):
        pr = _pr(verify=_verified(signals=_repro_signals(ran=False)))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True
        assert "incomplete" in why

    def test_a_rejected_null_repro_is_complete(self):
        # The blind lane rejected the agent's repro pre-run and committed the
        # verdict with the repro fields nulled: no repro was attempted, so the
        # "repro ran and did not corroborate" blocker must not fire.
        pr = _pr(verify=_verified(signals={"blind_adequacy": {
            "repro_command": None,
            "repro_rejected": "repro_command targets a test the PR itself "
                              "introduces ('src/x.test.ts')"}}))
        assert gates.verify_signals_incomplete(pr) is None

    def test_merge_allowed_passes_a_rejected_null_repro(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(signals={"blind_adequacy": {
                     "repro_command": None,
                     "repro_rejected": "repro_command targets a test the PR "
                                       "itself introduces ('src/x.test.ts')"}}))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is True, why


class TestCompilePreflightGate:
    """The merge-time compile preflight policy: only a run that concluded
    SENTINEL_PASS clears a live merge; everything else — refusal, error,
    missing or non-sentinel exit — blocks with its reason."""

    def test_pass_on_sentinel_pass_names_the_base(self):
        ok, why = gates.compile_preflight_gate({"exit": 0, "base_sha": "a" * 40})
        assert ok is True
        assert "aaaaaaaaaaaa" in why

    def test_compile_failure_blocks_and_names_the_error(self):
        ok, why = gates.compile_preflight_gate(
            {"exit": 20, "base_sha": "b" * 40,
             "error_excerpt": "src/x.ts(3,1): error TS2739: missing fields"})
        assert ok is False
        assert "TS2739" in why and "bbbbbbbbbbbb" in why

    def test_compile_failure_without_excerpt_still_blocks(self):
        ok, why = gates.compile_preflight_gate({"exit": 20, "base_sha": "b" * 40})
        assert ok is False
        assert "compile" in why

    def test_patch_conflict_blocks_as_needs_rebase(self):
        ok, why = gates.compile_preflight_gate({"exit": 30, "base_sha": "c" * 40})
        assert ok is False
        assert "rebase" in why and "cccccccccccc" in why

    def test_probe_failure_blocks(self):
        ok, why = gates.compile_preflight_gate({"exit": 10})
        assert ok is False
        assert "isolation" in why

    def test_non_sentinel_exit_blocks(self):
        ok, why = gates.compile_preflight_gate({"exit": 124})
        assert ok is False
        assert "124" in why

    def test_missing_exit_blocks(self):
        ok, why = gates.compile_preflight_gate({})
        assert ok is False
        assert "no exit" in why

    def test_refusal_blocks_with_its_reason(self):
        ok, why = gates.compile_preflight_gate(
            {"refused": "the PR changes dependency manifests"})
        assert ok is False
        assert "dependency manifests" in why

    def test_error_blocks_fail_closed(self):
        ok, why = gates.compile_preflight_gate({"error": "OSError: docker not found"})
        assert ok is False
        assert "docker not found" in why and "refused" in why


class TestLaneOutcomes:
    BLIND = {"test_cmd": "npx vitest run x.test.ts", "faithful": True}
    HOST = {"apply_exit": 0, "red_exit": 20, "green_exit": 0,
            "red_exit_confirm": 20, "green_exit_confirm": 0}
    JUDGE = {"red_reason_match": {"matches": True, "confidence": "high"}}
    REGRESS = {"ran": True, "exit_first": 0, "confirmed": False}

    def _outcome(self, lanes):
        return gates.verify_outcome(self.BLIND, self.HOST, self.JUDGE,
                                    regress=self.REGRESS, lanes=lanes)

    def test_all_lanes_green_is_verified_fix(self):
        lanes = {"compile": {"cmd": "c", "exit": 0, "ok": True},
                 "build": {"cmd": "b", "exit": 0, "ok": True}}
        assert self._outcome(lanes) == "verified-fix"

    def test_no_lanes_recorded_is_unchanged(self):
        assert self._outcome(None) == "verified-fix"

    def test_a_failed_lane_is_regressed(self):
        lanes = {"compile": {"cmd": "c", "exit": 20, "ok": False},
                 "build": {"cmd": "b", "skipped": "compile failed"}}
        assert self._outcome(lanes) == "regressed"

    def test_an_infra_lane_escalates(self):
        lanes = {"compile": {"cmd": "c", "exit": 0, "ok": True},
                 "build": {"cmd": "b", "exit": 137, "ok": False}}
        assert self._outcome(lanes) == "escalate"

    def test_a_malformed_entry_before_a_failed_lane_still_regresses(self):
        # order-independent: a malformed entry recorded ahead of a genuinely
        # failed lane must not mask the regression it proves — regressed is a
        # hard block and always wins over escalate.
        lanes = {"compile": "not-a-dict",
                 "build": {"cmd": "b", "exit": 20, "ok": False}}
        assert self._outcome(lanes) == "regressed"

    def test_a_malformed_entry_alone_among_passes_escalates(self):
        lanes = {"compile": {"cmd": "c", "exit": 0, "ok": True},
                 "build": "not-a-dict"}
        assert self._outcome(lanes) == "escalate"

    def test_a_lane_failure_on_the_authored_lane_is_regressed(self):
        blind = {"test_cmd": None, "faithful": True}
        authored = {"test_cmd": "npx vitest run a.test.ts",
                    "red_exit": 20, "green_exit": 0,
                    "red_exit_confirm": 20, "green_exit_confirm": 0}
        lanes = {"compile": {"cmd": "c", "exit": 20, "ok": False}}
        out = gates.verify_outcome(blind, {"apply_exit": 0}, self.JUDGE,
                                   regress=self.REGRESS, authored=authored,
                                   lanes=lanes)
        assert out == "regressed"

    def test_a_lane_skip_regress_is_not_an_errored_run(self):
        # A regress leg skipped because a lane failed is a complete result,
        # never a hold.
        regress = {"ran": False, "skipped_reason": "lane-compile-failed"}
        assert gates.verify_run_errored(self.BLIND, self.HOST,
                                        regress=regress) is False


class TestConfiguredLanes:
    def test_orders_compile_then_build(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"build_cmd": "pnpm build",
                                      "compile_cmd": "pnpm -r typecheck"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        assert gates.configured_lanes() == {"compile": "pnpm -r typecheck",
                                            "build": "pnpm build"}

    def test_empty_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(profile, "active",
                            lambda: profile.parse_profile({"version": 1}, "t"))
        assert gates.configured_lanes() == {}


class TestLanesIncomplete:
    def _pr(self, verify):
        from pipeline import model
        return model.Pr(None, {"pr": 1, "meta": {"title": "t", "state": "open",
                                                 "head_sha": "h"},
                               "verify": verify})

    def test_missing_lanes_named_when_deployment_requires_them(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "c", "build_cmd": "b"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        pr = self._pr({"outcome": "verified-fix", "against_base_sha": "x",
                       "signals": {"blind_adequacy": {}}})
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "compile" in why and "build" in why

    def test_recorded_lanes_fall_through_to_repro_logic(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "c"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        pr = self._pr({"outcome": "verified-fix", "against_base_sha": "x",
                       "signals": {"blind_adequacy": {},
                                   "lanes": {"compile": {"exit": 0, "ok": True}}}})
        assert gates.verify_signals_incomplete(pr) is None

    def test_a_failed_recorded_lane_is_incomplete_despite_a_verified_outcome(
            self, monkeypatch):
        # A stale writer can recompute a lane-blind verified-fix while a failed
        # lane entry remains on the record — the outcome is inconsistent with
        # its own lane evidence, and this is the one place that catches it.
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "c"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        pr = self._pr({"outcome": "verified-fix", "against_base_sha": "x",
                       "signals": {"blind_adequacy": {},
                                   "lanes": {"compile": {"cmd": "c", "exit": 20,
                                                        "ok": False}}}})
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "lane" in why


class TestAgentVerifiedLanesIncomplete:
    """merge_eligibility's agent-verified branch names a missing configured lane
    exactly like its verified-fix sibling, so partial evidence never presents
    as the plain provenance reason alone."""

    def test_missing_lanes_named_alongside_provenance(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "c", "build_cmd": "b"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        pr = _pr(verify=_verified(outcome="agent-verified",
                                  signals={"blind_adequacy": {}}))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True
        assert "agent-authored" in why and "verification incomplete" in why
        assert "compile" in why and "build" in why

    def test_recorded_lanes_keep_the_plain_provenance_reason(self, monkeypatch):
        p = profile.parse_profile(
            {"version": 1, "verify": {"compile_cmd": "c"}}, "t")
        monkeypatch.setattr(profile, "active", lambda: p)
        pr = _pr(verify=_verified(
            outcome="agent-verified",
            signals={"blind_adequacy": {},
                     "lanes": {"compile": {"exit": 0, "ok": True}}}))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True
        assert "agent-authored" in why
        assert "incomplete" not in why
