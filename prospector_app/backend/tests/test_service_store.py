"""App backend over the pipeline-v2 store: rows, suggestions, board state.
Records are built in-memory; data loaders are monkeypatched."""
from datetime import datetime, timedelta, timezone

import pytest

from prospector_app.backend import data
from prospector_app.backend import decisions
from pipeline import model
from prospector_app.backend import models
from prospector_app.backend import pr_checks
from prospector_app.backend import service
from prospector_app.backend import suggest
from pipeline.testsupport import reviews_section

HEAD = "abc123"
# Anchored to the real "now" so the security freshness window (≤7 days) never
# lapses as wall-clock time passes — a literal date here is a time-bomb that
# turns the fresh-path fixtures stale once it ages past SECURITY_MAX_AGE_DAYS.
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _pr(n=1, **over):
    rec = {
        "pr": n,
        "meta": {"title": f"fix {n}", "author": "alice", "state": "open", "draft": False,
                 "head_sha": HEAD, "url": f"https://x/pull/{n}",
                 "created_at": NOW, "updated_at": NOW, "checked_at": NOW},
        "signals": {"ci": "passing", "mergeable": True, "has_tests": True,
                    "diffstat": {"additions": 5, "deletions": 1, "changed_files": 1},
                    "checked_at": NOW, "against_head_sha": HEAD},
        "reviews": reviews_section(HEAD, NOW),
        "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": HEAD},
    }
    rec.update(over)
    return model.Pr(None, rec)


def _analysis(disposition="merge", **over):
    return dict({"disposition": disposition, "rationale": "r",
                 "checked_at": NOW, "against_head_sha": HEAD}, **over)


def _green(verdict="GREEN", **over):
    return dict({"verdict": verdict, "findings": [], "override": None,
                 "checked_at": NOW, "against_head_sha": HEAD}, **over)


def _verified(**over):
    # Complete evidence by default: every attempted signal corroborated, the
    # independent repro included — the bar gates.verify_signals_incomplete holds
    # a verified-fix to. A test about a specific gap overrides `signals`.
    return dict({"outcome": "verified-fix",
                 "signals": {
                     "blind_adequacy": {"repro_command": "node --test repro.mjs"},
                     "independent_repro": {"ran": True, "exit_code": 20},
                     "repro_reason_match": {"matches": True, "applicable": True,
                                            "confidence": "high"}},
                 "findings": [], "tier": 0,
                 "against_base_sha": "base1", "checked_at": NOW,
                 "against_head_sha": HEAD}, **over)


# head SHA → stubbed unified diff, shared by every _diffed_pr stub in a test.
# Heads are unique per test case, so leftovers from earlier tests never match
# (and the service._FACETS memo, keyed by head SHA, stays unpolluted).
_DIFFS: dict[str, str] = {}


def _diffed_pr(n, head, path, monkeypatch):
    """A clean merge-ready PR whose cached diff touches exactly `path`, every
    fact section re-stamped against `head` so freshness holds."""
    rec = _pr(n, analysis=_analysis(), security=_green())
    raw = rec.raw
    raw["meta"]["head_sha"] = head
    for sec in ("signals", "drift", "analysis", "security"):
        raw[sec]["against_head_sha"] = head
    _DIFFS[head] = (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                    "@@ -1 +1,2 @@\n+const x = 1\n")
    monkeypatch.setattr(service, "diff_text", lambda r: _DIFFS.get(r.head_sha))
    return rec


def _cluster(prs, outcome="merge-ready", cid=7):
    rec = {"id": cid, "root_problem": "x", "prs": [r.n for r in prs],
           "outcome": outcome, "checked_at": NOW}
    return model.Cluster(None, rec)


@pytest.fixture
def patched(monkeypatch):
    def use(prs: dict, clusters: dict | None = None):
        monkeypatch.setattr(data, "prs", lambda: prs)
        monkeypatch.setattr(data, "clusters", lambda: clusters or {})
        p2c: dict[int, list[int]] = {}
        for cid, c in (clusters or {}).items():
            for n in (c.prs if hasattr(c, "prs") else c.get("prs", [])):
                p2c.setdefault(int(n), []).append(cid)
        for n in p2c:
            p2c[n].sort()
        monkeypatch.setattr(data, "pr_to_clusters", lambda: p2c)
    return use


class TestSuggest:
    def test_merge_allowed_suggests_merge(self):
        rec = _pr(analysis=_analysis(), security=_green(), verify=_verified())
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "MERGE" and s["accept"].kind == "merge"

    def test_unverifiable_names_reasonless_human_merge_instead_of_block(self):
        rec = _pr(analysis=_analysis(), security=_green(),
                  verify=_verified(outcome="unverifiable-no-test"))
        s = suggest.suggest_for_record(rec)
        assert s["label"] == "Human merge available"
        assert "without an override reason" in s["rationale"]

    def test_dependabot_bump_is_out_of_scope(self):
        rec = _pr(n=7750)
        rec.raw["meta"]["author"] = "dependabot[bot]"
        rec.raw["summary"] = {"paths": ["pnpm-lock.yaml", "package.json"],
                              "checked_at": NOW, "against_head_sha": HEAD}
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "OUT_OF_SCOPE" and s["accept"] is None

    def test_out_of_scope_overrides_a_stale_close_stale(self):
        # the hallucinated close-stale must not surface — deferred wins, even
        # before the analysis-staleness branch.
        rec = _pr(n=7742, analysis=_analysis("close-stale", against_head_sha="OLD"))
        rec.raw["meta"]["author"] = "dependabot[bot]"
        rec.raw["summary"] = {"paths": [".github/workflows/pr.yml"],
                              "checked_at": NOW, "against_head_sha": HEAD}
        assert suggest.suggest_for_record(rec)["action"] == "OUT_OF_SCOPE"

    def test_dependabot_touching_source_keeps_its_real_disposition(self):
        rec = _pr(analysis=_analysis("request-changes"))
        rec.raw["meta"]["author"] = "dependabot[bot]"
        rec.raw["summary"] = {"paths": ["pnpm-lock.yaml", "src/app.ts"],
                              "checked_at": NOW, "against_head_sha": HEAD}
        assert suggest.suggest_for_record(rec)["action"] == "REQUEST_CHANGES"

    def test_merge_without_security_is_blocked(self):
        s = suggest.suggest_for_record(_pr(analysis=_analysis()))
        assert s["action"] == "BLOCKED" and s["accept"] is None
        assert "security" in s["rationale"]

    def test_security_block_is_tagged_blocker_security(self):
        # The re-run SECURITY button keys on this: security is what unblocks merge.
        s = suggest.suggest_for_record(_pr(analysis=_analysis()))
        assert s["action"] == "BLOCKED" and s["blocker"] == "security"

    def test_quality_gate_gap_reads_request_changes(self):
        # Greptile < 5 derives request-changes with gap-closing asks — an
        # author-actionable card, not an operator BLOCKED card.
        rec = _pr(analysis=_analysis())
        rec.raw["reviews"]["greptile"]["score"] = 4
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "REQUEST_CHANGES"

    def test_blocked_merge_on_codeowners_pr_names_the_human_path(self):
        # Security is the stated blocker, but a CODEOWNERS-gated PR needs a
        # code-owner merge even after SECURITY re-runs — the card must say so.
        hm = {"required": True, "paths": ["skills/example/SKILL.md"],
              "owners": ["@o1", "@o2"]}
        s = suggest.suggest_for_record(_pr(analysis=_analysis()), human_merge=hm)
        assert s["action"] == "BLOCKED" and s["blocker"] == "security"
        assert "code-owner merge" in s["rationale"] and "@o1 + @o2" in s["rationale"]

    def test_blocked_merge_without_codeowners_keeps_reason_bare(self):
        s = suggest.suggest_for_record(_pr(analysis=_analysis()), human_merge=None)
        assert s["action"] == "BLOCKED" and "code-owner" not in s["rationale"]

    def test_stale_analysis_suggests_reanalyze(self):
        rec = _pr(analysis=_analysis(against_head_sha="OLD"), security=_green())
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "ANALYZE"

    def test_request_changes_carries_asks(self):
        rec = _pr(analysis=_analysis("request-changes", asks=["add a test", "fix the lock"]))
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "REQUEST_CHANGES"
        assert "add a test" in s["comment"]
        assert s["accept"].event == "request-changes"

    def test_secret_leak_prefills_credential_without_leaking_value(self):
        # `<file>: VAR: "value"` — the comment names VAR and file, never the value.
        secret = "01f5f8bf4cfb187bdfb583a7be1bf534ca4000abbc7bd5942156ff0033fd888d"
        rec = _pr(analysis=_analysis("request-changes"),
                  threat={"verdict": "suspicious", "signatures": ["secret-leak"],
                          "detail": {"secret-leak": f'ecosystem.config.cjs: BETTER_AUTH_SECRET: "{secret}"'}})
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "REQUEST_CHANGES"
        assert "`BETTER_AUTH_SECRET` in `ecosystem.config.cjs`" in s["comment"]
        assert secret not in s["comment"] and secret not in s["accept"].body
        assert "Rotate" in s["comment"]

    def test_request_changes_override_on_unanalyzed_pr_prefills_secret(self):
        # The manual "Request changes" button asks for the suggestion with an
        # explicit disposition even on a never-analyzed PR — it must skip the
        # "not analyzed" short-circuit and still prefill the secret comment.
        secret = "deadbeef" * 8
        rec = _pr(threat={"verdict": "suspicious", "signatures": ["secret-leak"],
                          "detail": {"secret-leak": f'docker/docker-compose.yml: BETTER_AUTH_SECRET: "{secret}"'}})
        rec.raw.pop("analysis", None)  # never analyzed
        s = suggest.suggest_for_record(rec, "request-changes")
        assert s["action"] == "REQUEST_CHANGES"
        assert "`BETTER_AUTH_SECRET` in `docker/docker-compose.yml`" in s["bot_comment"]
        assert secret not in s["bot_comment"]

    def test_secret_leak_handles_env_assignment_form(self):
        # `<file>: - VAR=value` (yaml list item) — leading "- " stripped, no value.
        secret = "1c688774012cfdd1440c21bab81c636c9b2358506031c5999d09a8b93af5f00f"
        rec = _pr(analysis=_analysis("request-changes"),
                  threat={"verdict": "suspicious", "signatures": ["secret-leak"],
                          "detail": {"secret-leak": f"docker-compose.override.yml: - OBSIDIAN_API_KEY={secret}"}})
        s = suggest.suggest_for_record(rec)
        assert "`OBSIDIAN_API_KEY` in `docker-compose.override.yml`" in s["comment"]
        assert secret not in s["comment"]

    def test_close_dup_carries_canonical(self):
        rec = _pr(analysis=_analysis("close-dup", canonical=999))
        s = suggest.suggest_for_record(rec)
        assert s["accept"].canonical == 999

    def test_close_fixed_carries_upstream_reference(self):
        rec = _pr(analysis=_analysis(
            "close-fixed",
            upstream_pr=6008,
            upstream_commit="f3db7b88ea38a89546719ef2e0e2101127e55480",
            upstream_date="2026-06-10",
        ))
        s = suggest.suggest_for_record(rec)
        assert s["accept"].upstream_pr == 6008
        assert s["accept"].upstream_commit == "f3db7b88ea38a89546719ef2e0e2101127e55480"
        assert s["accept"].upstream_date == "2026-06-10"

    def test_unanalyzed_clustered_points_at_cluster(self):
        rec = _pr(cluster={"ids": [7], "checked_at": NOW, "against_head_sha": HEAD})
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "ANALYZE" and s["label"] == "Not yet analyzed"
        assert "cluster" in s["rationale"]

    def test_unanalyzed_standalone(self):
        # a clustering pass considered it and left it standalone (no id)
        rec = _pr(cluster={"checked_at": NOW, "against_head_sha": HEAD})
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "STANDALONE" and s["accept"] is None
        assert "Standalone" in s["label"]

    def test_unanalyzed_not_yet_clustered(self):
        # no cluster section at all → no pass has reached it
        s = suggest.suggest_for_record(_pr())
        assert s["action"] == "CLUSTER" and s["accept"] is None
        assert "Not yet clustered" in s["label"]

    def test_unanalyzed_standalone_stamp_stale_is_not_yet_clustered(self):
        # the standalone stamp is from an old head → treated as not-yet-clustered
        rec = _pr(cluster={"checked_at": NOW, "against_head_sha": "OLD"})
        s = suggest.suggest_for_record(rec)
        assert s["action"] == "CLUSTER"


class TestSafetySummary:
    """The security banner must never contradict the merge gate: a verdict outside
    the SECURITY_MAX_AGE_DAYS window still renders, but says why it no longer
    counts for merge."""

    def test_current_green_is_unqualified(self):
        s = service._safety_summary(_pr(security=_green()))
        assert s["headline"] == "Likely safe — no concerns flagged"
        assert "no longer counts" not in s["detail"]

    def test_old_green_says_why_it_does_not_count(self):
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
        s = service._safety_summary(_pr(security=_green(checked_at=old)))
        assert s["level"] == "safe"
        assert s["headline"] == "Likely safe at last review — no concerns flagged"
        assert "outside the 7d window" in s["detail"]
        assert "Re-run SECURITY" in s["detail"]

    def test_head_moved_green_says_stale(self):
        s = service._safety_summary(_pr(security=_green(against_head_sha="OLD")))
        assert s["headline"] == "Likely safe at last review — no concerns flagged"
        assert "earlier head" in s["detail"]

    def test_stale_red_keeps_risk_level_with_note(self):
        sec = _green(verdict="RED", findings=[{"title": "x"}], against_head_sha="OLD")
        s = service._safety_summary(_pr(security=sec))
        assert s["level"] == "risk" and "earlier head" in s["detail"]

    def test_unreviewed_pr_unchanged(self):
        s = service._safety_summary(_pr())
        assert s["verdict"] is None and s["headline"] == "Not yet security-reviewed"


class TestChecks:
    def test_clean_record_all_pass(self):
        c = pr_checks.checks_for_record(_pr(security=_green()))
        assert c["passed"] == c["total"] > 0

    def test_greptile_4_fails_the_review_check(self):
        rec = _pr()
        rec.raw["reviews"]["greptile"]["score"] = 4
        c = pr_checks.checks_for_record(rec)
        review = next(x for x in c["checks"] if x["key"] == "review")
        assert review["status"] == "fail" and "Greptile 4/5" in review["detail"]

    def test_stale_security_marked(self):
        rec = _pr(security=_green(against_head_sha="OLD"))
        c = pr_checks.checks_for_record(rec)
        sec = next(x for x in c["checks"] if x["name"] == "Deep security review")
        assert sec["status"] == "warn" and "STALE" in sec["detail"]

    def test_stale_security_by_age_names_the_days(self):
        # #550: "security run 25 days old" needs to be a number the operator can
        # read, not just a generic STALE flag. Age is UTC-calendar days between
        # checked_at's date and the injected `today`.
        old = "2026-07-05T00:00:00+00:00"
        rec = _pr(security=_green(checked_at=old))
        c = pr_checks.checks_for_record(rec, today="2026-07-15")
        sec = next(x for x in c["checks"] if x["name"] == "Deep security review")
        assert sec["status"] == "warn" and "10d old" in sec["detail"]
        assert sec["at"] == old


class TestServiceRows:
    def test_pr_row_shape(self, patched):
        patched({1: _pr(analysis=_analysis(), security=_green(), verify=_verified())},
                {7: {"id": 7, "root_problem": "x", "prs": [1], "outcome": "merge-ready", "checked_at": NOW}})
        row = service.pr_row(1)
        assert row["clusters"] == [7]
        assert row["disposition"] == "merge"
        assert row["safety"] == "GREEN"
        assert row["clean"] is True
        assert row["suggestion"]["action"] == "MERGE"

    def test_merge_gate_allows_clean_unanalyzed_pr(self, patched, monkeypatch):
        # An Easy-Lane PR: clean signals, never analyzed, never security-reviewed.
        patched({1: _pr()})
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        monkeypatch.setattr(service, "live_changed_paths", lambda n: None)
        rec = service.data.prs()[1]
        rec.raw["meta"]["body"] = ""  # skip the live body fetch
        gate = service.pr_detail(1)["merge_gate"]
        assert gate["ok"] is True

    @pytest.mark.parametrize("outcome", [
        "unverifiable-no-test", "unverifiable-needs-live-agent",
    ])
    def test_merge_gate_allows_reasonless_unverifiable_pr(
            self, patched, monkeypatch, outcome):
        rec = _pr(verify=_verified(outcome=outcome))
        rec.raw["meta"]["body"] = ""
        patched({1: rec})
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        monkeypatch.setattr(service, "live_changed_paths", lambda n: None)
        gate = service.pr_detail(1)["merge_gate"]
        assert gate["ok"] is True
        assert gate["overridable"] is False
        assert outcome in gate["reason"]

    def test_merge_gate_blocks_dirty_pr(self, patched, monkeypatch):
        rec = _pr()
        rec.raw["reviews"]["greptile"]["score"] = 4
        rec.raw["meta"]["body"] = ""
        patched({1: rec})
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        monkeypatch.setattr(service, "live_changed_paths", lambda n: None)
        gate = service.pr_detail(1)["merge_gate"]
        assert gate["ok"] is False and "greptile" in gate["reason"]

    def test_merge_gate_blocks_codeowners_path(self, patched, monkeypatch):
        rec = _pr()
        rec.raw["meta"]["body"] = ""
        patched({1: rec})
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        monkeypatch.setattr(service, "live_changed_paths",
                            lambda n: [".github/workflows/ci.yml", "src/other.ts"])
        gate = service.pr_detail(1)["merge_gate"]
        assert gate["ok"] is False and "code owner" in gate["reason"]

    def test_pr_row_hides_greptile_severity_when_review_is_stale(self, patched):
        # greptile_review was stamped against an OLD head; the head has since
        # moved to HEAD, so the section's severity must surface as unknown
        # (None) rather than the stale label.
        rec = _pr(greptile_review={"severity": "defects", "findings": [],
                                    "checked_at": NOW, "against_head_sha": "OLD"})
        patched({1: rec})
        row = service.pr_row(1)
        assert row["signals"]["greptile_severity"] is None

    def test_pr_row_surfaces_greptile_severity_when_review_is_current(self, patched):
        rec = _pr(greptile_review={"severity": "defects", "findings": [],
                                    "checked_at": NOW, "against_head_sha": HEAD})
        patched({1: rec})
        row = service.pr_row(1)
        assert row["signals"]["greptile_severity"] == "defects"

    def test_pr_row_flags_trusted_author(self, patched):
        trusted = _pr(1)
        trusted.raw["meta"]["author"] = "trusted-dev"
        patched({1: trusted, 2: _pr(2)})
        assert service.pr_row(1)["trusted_author"] is True
        assert service.pr_row(2)["trusted_author"] is False

    def test_pr_row_exposes_upstream_reference(self, patched):
        patched({1: _pr(analysis=_analysis(
            "close-fixed",
            upstream_pr=6008,
            upstream_commit="f3db7b88ea38a89546719ef2e0e2101127e55480",
            upstream_date="2026-06-10",
        ))})
        row = service.pr_row(1)
        assert row["proposed_action"]["upstream_pr"] == 6008
        assert row["proposed_action"]["upstream_commit"] == "f3db7b88ea38a89546719ef2e0e2101127e55480"
        assert row["proposed_action"]["upstream_date"] == "2026-06-10"


class TestPrDetailDegradesOnLiveFailure:
    """pr_detail fans out up to three live round-trips (Greptile/CI, CODEOWNERS,
    PR body). A failure or timeout in any one must degrade to the cached value and
    still return the row — never let the exception 500 the whole detail endpoint."""

    def test_live_body_timeout_still_returns_cached_row(self, patched, monkeypatch):
        import subprocess
        patched({1: _pr(analysis=_analysis(), security=_green())})
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        monkeypatch.setattr(service, "live_changed_paths", lambda n: None)
        # _pr() has no meta.body, so body_fut fires the live gh fetch — make it
        # raise the way a 30s `gh` timeout does.
        def timeout(n):
            raise subprocess.TimeoutExpired(cmd=["gh"], timeout=30)
        monkeypatch.setattr(service, "_pr_body_live", timeout)

        row = service.pr_detail(1)

        assert row is not None
        assert row["number"] == 1
        assert row["disposition"] == "merge"   # cached store data intact
        assert row["safety"] == "GREEN"
        assert row["body"] is None             # live body degraded to the ingested value
        assert "body" in row["live_refresh_failed"]

    def test_live_ci_fetch_failure_degrades_row(self, patched, monkeypatch):
        patched({1: _pr(analysis=_analysis(), security=_green())})
        rec = service.data.prs()[1]
        rec.raw["meta"]["body"] = ""               # ingested → skip the live body fetch
        monkeypatch.setattr(service, "live_changed_paths", lambda n: None)
        def boom(sha):
            raise RuntimeError("worker unreachable")
        monkeypatch.setattr(service, "_ci_checks", boom)

        row = service.pr_detail(1)

        assert row is not None and row["number"] == 1
        assert row["ci_checks"] == []
        assert "ci" in row["live_refresh_failed"]
        # Reviewer feedback comes from the store, so it survives the live failure.
        assert row["reviews_detail"]["greptile"]["digest"]["status"] == "pass"

    def test_codeowners_fetch_failure_falls_back_to_cached_human_merge(self, patched, monkeypatch):
        patched({1: _pr(analysis=_analysis(), security=_green())})
        rec = service.data.prs()[1]
        rec.raw["meta"]["body"] = ""
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        def boom(n, sha):
            raise RuntimeError("gh files API unreachable")
        monkeypatch.setattr(service, "live_changed_paths", boom)

        row = service.pr_detail(1)

        assert row is not None and row["number"] == 1
        assert "human_merge" in row["live_refresh_failed"]
        # endpoint still returns a usable merge gate from the store
        assert row["merge_gate"]["ok"] is True

    def test_all_live_calls_succeed_leaves_no_failure_note(self, patched, monkeypatch):
        patched({1: _pr(analysis=_analysis(), security=_green())})
        rec = service.data.prs()[1]
        rec.raw["meta"]["body"] = ""
        monkeypatch.setattr(service, "_ci_checks", lambda sha: [])
        monkeypatch.setattr(service, "live_changed_paths", lambda n: None)
        assert service.pr_detail(1)["live_refresh_failed"] == []


class TestQueryPrs:
    def test_query_filters_and_returns_match_ids(self, patched):
        patched({
            1: _pr(1, analysis=_analysis(), security=_green()),
            2: _pr(2, signals={"greptile": 2, "ci": "failing", "mergeable": True,
                               "diffstat": {"additions": 9, "deletions": 9, "changed_files": 5},
                               "checked_at": NOW, "against_head_sha": HEAD},
                   analysis=_analysis("needs-human")),
        })
        out = service.query_prs({"disposition": "needs-human"})
        assert out["total"] == 1
        assert out["match_ids"] == [2]
        assert out["items"][0]["number"] == 2

    def test_query_by_risk_tier(self, patched, monkeypatch):
        # Tier is path-derived: the leaf PR (tier 3) matches a tier-3 filter; the
        # auth-core (tier 0) and diffless (tier unknown) PRs don't, despite
        # identical signals.
        leaf = _diffed_pr(1, "easyhead-leaf", "ui/src/App.tsx", monkeypatch)
        core = _diffed_pr(2, "easyhead-core", "server/src/middleware/auth.ts", monkeypatch)
        nodiff = _pr(3, analysis=_analysis(), security=_green())
        patched({1: leaf, 2: core, 3: nodiff})
        out = service.query_prs({"risk_tier": 3})
        assert out["match_ids"] == [1]

    def test_pr_row_exposes_risk_tier(self, patched, monkeypatch):
        rec = _diffed_pr(1, "tierhead-row", "docs/guide.md", monkeypatch)
        patched({1: rec, 2: _pr(2)})
        assert service.pr_row(1)["risk_tier"] == 3
        assert service.pr_row(2)["risk_tier"] is None  # no cached diff → unknown

    def test_query_sorts_by_tier(self, patched, monkeypatch):
        a = _diffed_pr(1, "tierhead-leaf", "ui/src/App.tsx", monkeypatch)      # tier 3
        b = _diffed_pr(2, "tierhead-core", "package.json", monkeypatch)        # tier 0
        c = _pr(3)                                                             # unknown → last
        patched({1: a, 2: b, 3: c})
        assert [r["number"] for r in service.query_prs({}, sort="tier")["items"]] == [2, 1, 3]

    def test_row_has_merge_gate_and_age_days(self, patched):
        patched({1: _pr(1, analysis=_analysis(), security=_green())})
        row = service.pr_row(1)
        assert "merge_gate" in row and "ok" in row["merge_gate"]
        assert "age_days" in row

    def test_query_sorts_by_loc_and_files(self, patched):
        def sized(n, add, dele, files):
            return _pr(n, signals={"greptile": 5, "ci": "passing", "mergeable": True,
                                   "checked_at": NOW, "against_head_sha": HEAD,
                                   "diffstat": {"additions": add, "deletions": dele, "changed_files": files}})
        patched({
            1: sized(1, add=5, dele=5, files=2),     # loc 10, files 2
            2: sized(2, add=100, dele=20, files=3),  # loc 120, files 3
            3: sized(3, add=1, dele=0, files=9),     # loc 1, files 9
        })
        # LOC defaults to largest-first
        assert [r["number"] for r in service.query_prs({}, sort="loc")["items"]] == [2, 1, 3]
        assert [r["number"] for r in service.query_prs({}, sort="loc", direction="asc")["items"]] == [3, 1, 2]
        # Files defaults to most-first
        assert [r["number"] for r in service.query_prs({}, sort="files")["items"]] == [3, 2, 1]

    def test_query_includes_drafts_excludes_closed(self, patched):
        closed = _pr(3)
        closed.raw["meta"]["state"] = "merged"
        draft = _pr(4)
        draft.raw["meta"]["draft"] = True
        patched({1: _pr(1), 3: closed, 4: draft})
        out = service.query_prs({})
        nums = [r["number"] for r in out["items"]]
        assert 1 in nums and 4 in nums          # open + draft both surface
        assert 3 not in nums                    # closed still excluded
        draft_row = next(r for r in out["items"] if r["number"] == 4)
        assert draft_row["draft"] is True

    def test_query_state_closed_returns_only_closed_and_merged(self, patched):
        merged = _pr(2)
        merged.raw["meta"]["state"] = "merged"
        closed = _pr(3)
        closed.raw["meta"]["state"] = "closed"
        patched({1: _pr(1), 2: merged, 3: closed})
        out = service.query_prs({"state": "closed"})
        assert [r["number"] for r in out["items"]] == [3, 2]

    def test_query_state_all_returns_every_state(self, patched):
        closed = _pr(3)
        closed.raw["meta"]["state"] = "closed"
        patched({1: _pr(1), 3: closed})
        out = service.query_prs({"state": "all"})
        assert {r["number"] for r in out["items"]} == {1, 3}


class TestDecisionComments:
    def test_close_fixed_comment_cites_upstream_pr_not_commit(self):
        # The repo squash-merges, so a branch commit hash may not exist on master;
        # cite the canonical PR (+ merge date), never the hash.
        body = decisions.default_comment(models.CloseAction(
            action="CLOSE_FIXED",
            upstream_pr=6008,
            upstream_commit="f3db7b88ea38a89546719ef2e0e2101127e55480",
            upstream_date="2026-06-10",
        ))
        assert "#6008" in body
        assert "f3db7b88ea38a89546719ef2e0e2101127e55480" not in body
        assert "2026-06-10" in body


class TestBoard:
    def test_cluster_summary_states(self, patched):
        prs = {1: _pr(1, analysis=_analysis(), security=_green(), verify=_verified()),
               2: _pr(2, analysis=_analysis("close-dup", canonical=1))}
        clusters = {7: _cluster([prs[1], prs[2]], outcome="merge-ready")}
        patched(prs, clusters)
        board = service.cluster_summaries()
        assert board[0]["state"] == "ready"
        assert board[0]["dispositions"] == {"merge": 1, "close-dup": 1}
        assert board[0]["security"]["green"] == 1

    def test_cluster_summaries_counts_draft_member(self, patched):
        open_pr = _pr(1, analysis=_analysis("merge"))
        draft = _pr(2, analysis=_analysis("close-dup", canonical=1))
        draft.raw["meta"]["draft"] = True
        clusters = {7: _cluster([open_pr, draft], outcome="merge-ready", cid=7)}
        patched({1: open_pr, 2: draft}, clusters)
        row = service.cluster_summaries()[0]
        # the draft is an active member and its close-dup disposition is counted,
        # while the open PR's merge disposition still rolls up normally
        assert row["dispositions"].get("close-dup", 0) == 1
        assert row["dispositions"].get("merge", 0) == 1

    def test_security_pending_when_merge_unreviewed(self, patched):
        prs = {1: _pr(1, analysis=_analysis())}
        clusters = {7: _cluster([prs[1]], outcome="merge-ready")}
        patched(prs, clusters)
        assert service.cluster_summaries()[0]["state"] == "security-pending"

    def test_security_rollup_flags_stale_verdicts(self, patched):
        # The rollup counts stored verdicts, so a stale GREEN still counts green —
        # `fresh` is what lets the board distinguish it from a gate-clearing one.
        prs = {1: _pr(1, analysis=_analysis(), security=_green()),
               2: _pr(2, analysis=_analysis(), security=_green(against_head_sha="OLD")),
               3: _pr(3, analysis=_analysis())}
        clusters = {7: _cluster([prs[1], prs[2], prs[3]], outcome="merge-ready")}
        patched(prs, clusters)
        row = service.cluster_summaries()[0]
        assert row["state"] == "security-pending"
        assert row["security"] == {"green": 2, "yellow": 0, "red": 0, "unknown": 1}
        fresh_by_pr = {p["pr"]: p["fresh"] for p in row["security_prs"]}
        assert fresh_by_pr == {1: True, 2: False, 3: None}

    def test_detail_buckets_by_disposition(self, patched):
        prs = {1: _pr(1, analysis=_analysis()), 2: _pr(2)}
        clusters = {7: _cluster([prs[1], prs[2]], outcome=None)}
        patched(prs, clusters)
        cd = service.cluster_detail(7)
        assert [r["number"] for r in cd["buckets"]["merge"]] == [1]
        assert [r["number"] for r in cd["buckets"]["unanalyzed"]] == [2]
        assert cd["state"] == "needs-analysis"

    def test_cluster_summary_includes_pain_score(self, patched):
        pr = _pr(1, issues={"linked": [{"issue": 42, "pain": 0.5, "how": "explicit"}],
                             "checked_at": NOW})
        pr.raw["meta"]["comments"] = 10
        pr.raw["meta"]["reactions_total"] = 4
        clusters = {7: _cluster([pr], outcome=None)}
        patched({1: pr}, clusters)
        row = service.cluster_summaries()[0]
        assert "pain_score" in row
        assert row["pain_score"] == round(0.5 + 0.01 * 10 + 0.01 * 4, 4)
        assert row["pain_breakdown"]["linked_issues"] == 1
        assert row["pain_breakdown"]["pr_comments"] == 10
        assert row["pain_breakdown"]["pr_reactions"] == 4

    def test_cluster_pain_sums_member_pr_scores(self, patched):
        # Two PRs both explicitly fix the same issue — the cluster rolls up both PR
        # scores (no cross-PR dedup), so the cluster gets 2× the per-PR issue pain.
        pr1 = _pr(1, issues={"linked": [{"issue": 42, "pain": 0.8, "how": "explicit"}],
                              "checked_at": NOW})
        pr2 = _pr(2, issues={"linked": [{"issue": 42, "pain": 0.8, "how": "explicit"}],
                              "checked_at": NOW})
        clusters = {7: _cluster([pr1, pr2], outcome=None)}
        patched({1: pr1, 2: pr2}, clusters)
        row = service.cluster_summaries()[0]
        assert row["pain_breakdown"]["linked_issues"] == 2
        assert row["pain_breakdown"]["issue_pain"] == round(0.8 + 0.8, 4)
        assert row["pain_score"] == round(0.8 + 0.8, 4)

    def test_subsystem_links_excluded_from_pain(self, patched):
        # Subsystem matches are discovery hints, not fix-claims: they don't feed the
        # pain score. Only the explicit (author-declared Fixes #N) link counts.
        pr = _pr(1, issues={"linked": [
            {"issue": 10, "pain": 0.5, "how": "explicit"},
            {"issue": 20, "pain": 0.9, "how": "subsystem"},
            {"issue": 30, "pain": 0.0, "how": "subsystem"},
        ], "checked_at": NOW})
        patched({1: pr})
        row = service.pr_row(1, pr)
        assert row["pain_breakdown"]["linked_issues"] == 1
        assert row["pain_breakdown"]["issue_pain"] == 0.5
        assert row["pain_score"] == 0.5

    def test_cluster_pain_excludes_bot_pr_engagement(self, patched):
        # Bot-authored PRs' comments and reactions don't count toward community pain.
        bot_pr = _pr(1, issues={"linked": [], "checked_at": NOW})
        bot_pr.raw["meta"]["author"] = "dependabot[bot]"
        bot_pr.raw["meta"]["comments"] = 99
        bot_pr.raw["meta"]["reactions_total"] = 50
        clusters = {7: _cluster([bot_pr], outcome=None)}
        patched({1: bot_pr}, clusters)
        row = service.cluster_summaries()[0]
        assert row["pain_breakdown"]["pr_comments"] == 0
        assert row["pain_breakdown"]["pr_reactions"] == 0
        assert row["pain_score"] == 0.0

    def test_cluster_pain_zero_when_no_signals(self, patched):
        prs = {1: _pr(1)}
        clusters = {7: _cluster([prs[1]], outcome=None)}
        patched(prs, clusters)
        row = service.cluster_summaries()[0]
        assert row["pain_score"] == 0.0

    def test_single_pr_cluster_pain_equals_pr_pain(self, patched):
        # Acceptance criterion: a single-PR cluster inherits the PR's pain score.
        pr = _pr(1, issues={"linked": [{"issue": 10, "pain": 0.6, "how": "explicit"}],
                             "checked_at": NOW})
        pr.raw["meta"]["comments"] = 5
        pr.raw["meta"]["reactions_total"] = 2
        clusters = {7: _cluster([pr], outcome=None)}
        patched({1: pr}, clusters)
        pr_row = service.pr_row(1, pr)
        cluster_row = service.cluster_summaries()[0]
        assert pr_row["pain_score"] == cluster_row["pain_score"]

    def test_pr_row_includes_pain_score(self, patched):
        pr = _pr(1, issues={"linked": [{"issue": 5, "pain": 0.3, "how": "explicit"}],
                             "checked_at": NOW})
        pr.raw["meta"]["comments"] = 20
        pr.raw["meta"]["reactions_total"] = 8
        patched({1: pr})
        row = service.pr_row(1, pr)
        expected = round(0.3 + 0.01 * 20 + 0.01 * 8, 4)
        assert row["pain_score"] == expected
        assert row["pain_breakdown"]["linked_issues"] == 1
        assert row["pain_breakdown"]["issue_pain"] == 0.3
        assert row["pain_breakdown"]["pr_comments"] == 20
        assert row["pain_breakdown"]["pr_reactions"] == 8

    def test_pr_row_pain_zero_for_bot_author(self, patched):
        pr = _pr(1, issues={"linked": [], "checked_at": NOW})
        pr.raw["meta"]["author"] = "renovate[bot]"
        pr.raw["meta"]["comments"] = 100
        pr.raw["meta"]["reactions_total"] = 50
        patched({1: pr})
        row = service.pr_row(1, pr)
        assert row["pain_score"] == 0.0
        assert row["pain_breakdown"]["pr_comments"] == 0


class TestVerifyAndReversible:
    def test_close_fixed_without_upstream_needs_verify(self):
        s = suggest.suggest_for_record(_pr(analysis=_analysis("close-fixed")))
        assert s["needs_verify"] and "didn't cite" in s["needs_verify"]
        assert s["reversible"] is True

    def test_close_fixed_with_upstream_is_clean(self):
        s = suggest.suggest_for_record(_pr(analysis=_analysis("close-fixed", upstream_pr=6008)))
        assert s["needs_verify"] is None

    def test_merge_is_not_reversible(self):
        s = suggest.suggest_for_record(
            _pr(analysis=_analysis("merge"), security=_green(), verify=_verified()))
        assert s["reversible"] is False

    def test_close_dup_with_canonical_is_clean_and_reversible(self):
        s = suggest.suggest_for_record(_pr(analysis=_analysis("close-dup", canonical=99)))
        assert s["needs_verify"] is None and s["reversible"] is True

    def test_close_fixed_comment_hedges_when_no_upstream(self):
        from prospector_app.backend import decisions
        body = decisions.default_comment(models.CloseAction(action="CLOSE_FIXED"))
        assert "appears" in body and "reopen" in body  # honest hedge, no fabricated commit
