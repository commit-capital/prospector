"""SECURITY driver: eligibility manifest + validated verdict commits + RED flip."""
import json

from pipeline import security_driver as sd
from pipeline.store import Store
from pipeline.wire import VerdictItem
from pipeline.testsupport import greptile_entry, reviews_section

NOW = "2026-06-10T00:00:00+00:00"


def _pr(store, n, head="h1", disposition="merge", greptile=5, security=None, cluster=None):
    rec = {"pr": n,
           "meta": {"title": f"t{n}", "author": "a", "state": "open", "draft": False,
                    "head_sha": head, "checked_at": NOW},
           "signals": {"ci": "passing", "mergeable": True,
                       "checked_at": NOW, "against_head_sha": head},
           "reviews": reviews_section(head, NOW, greptile=greptile_entry(greptile, head)),
           "drift": {"state": "applicable", "checked_at": NOW, "against_head_sha": head},
           "analysis": {"disposition": disposition, "rationale": "r",
                        "checked_at": NOW, "against_head_sha": head}}
    if disposition == "close-dup":
        rec["analysis"]["canonical"] = 999
    if security:
        rec["security"] = dict(security, checked_at=security.get("checked_at", NOW),
                               against_head_sha=security.get("against_head_sha", head))
    if cluster:
        rec["cluster"] = {"ids": [cluster], "checked_at": NOW, "against_head_sha": head}
    store.save_pr(rec)


def _agent_log(path, prompt, usage):
    path.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": prompt}}),
        json.dumps({"type": "assistant", "message": {"usage": usage, "content": "ok"}}),
    ]))


class TestEligible:
    def test_clean_merge_candidates_without_current_verdict(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)                                       # eligible
        _pr(s, 2, disposition="close-dup")              # not merge-routed
        _pr(s, 3, greptile=4)                           # not clean
        _pr(s, 4, security={"verdict": "GREEN", "findings": []})   # already reviewed
        _pr(s, 5, security={"verdict": "GREEN", "findings": [],
                            "against_head_sha": "OLD"})  # stale review → re-run
        assert [m.pr for m in sd.eligible(s, today="2026-06-10")] == [1, 5]

    def test_old_verdict_needs_rereview(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, security={"verdict": "GREEN", "findings": [],
                            "checked_at": "2026-05-01T00:00:00+00:00"})
        assert [m.pr for m in sd.eligible(s, today="2026-06-10")] == [1]

    def _diff(self, diffs_dir, head, path):
        diffs_dir.mkdir(exist_ok=True)
        (diffs_dir / f"{head}.diff").write_text(
            f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1,2 @@\n+x\n")

    def test_ordered_riskiest_first(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        diffs = tmp_path / "diffs"
        monkeypatch.setattr(sd, "DIFFS", diffs)
        _pr(s, 1, head="hleaf")
        self._diff(diffs, "hleaf", "ui/src/App.tsx")                  # tier 3
        _pr(s, 2, head="hcore")
        self._diff(diffs, "hcore", "server/src/middleware/auth.ts")   # tier 0
        _pr(s, 3, head="hmiss")                                       # no diff → unknown, last
        _pr(s, 4, head="hgov")
        self._diff(diffs, "hgov", "server/src/routes/agents.ts")      # tier 1
        assert [m.pr for m in sd.eligible(s, today="2026-06-10")] == [2, 4, 1, 3]

    def test_max_n_truncates_after_tier_ordering(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        diffs = tmp_path / "diffs"
        monkeypatch.setattr(sd, "DIFFS", diffs)
        _pr(s, 1, head="hleaf")
        self._diff(diffs, "hleaf", "docs/guide.md")                   # tier 3
        _pr(s, 2, head="hcore")
        self._diff(diffs, "hcore", "pnpm-lock.yaml")                  # tier 0
        assert [m.pr for m in sd.eligible(s, today="2026-06-10", max_n=1)] == [2]


class TestEstimate:
    def test_estimate_reports_calls_tokens_and_legacy_savings(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        _pr(s, 1, head="h1")
        _pr(s, 2, head="h2")
        monkeypatch.setattr(sd, "DIFFS", tmp_path / "diffs")
        sd.DIFFS.mkdir(parents=True, exist_ok=True)
        (sd.DIFFS / "h1.diff").write_text("a" * 400)
        (sd.DIFFS / "h2.diff").write_text("b" * 800)

        est = sd.estimate_security_run(
            s,
            today="2026-06-10",
            expected_findings_per_pr=2.0,
            input_price_per_m=10,
            output_price_per_m=20,
        )

        assert est["eligible_prs"] == 2
        assert est["missing_cached_diffs"] == 0
        assert est["model_calls"]["review"] == 6
        assert est["model_calls"]["verify"] == 2
        assert est["model_calls"]["legacy_verify"] == 4
        assert est["tokens"]["legacy_metered"] > est["tokens"]["metered"]
        assert est["tokens"]["estimated_saved_metered"] > 0
        assert est["cost"]["legacy_estimated"] >= est["cost"]["estimated"]

    def test_usage_log_calibrates_estimate(self, tmp_path, monkeypatch):
        s = Store(tmp_path / "store")
        _pr(s, 1, head="h1")
        monkeypatch.setattr(sd, "DIFFS", tmp_path / "diffs")
        usage = tmp_path / "usage"
        usage.mkdir()
        for i in range(3):
            _agent_log(
                usage / f"agent-review-{i}.jsonl",
                "Pre-merge security review of PR #1",
                {"input_tokens": 10, "cache_creation_input_tokens": 20,
                 "cache_read_input_tokens": 30, "output_tokens": 40},
            )
        for i in range(6):
            _agent_log(
                usage / f"agent-verify-{i}.jsonl",
                "Adversarially verify a YELLOW finding",
                {"input_tokens": 100, "cache_creation_input_tokens": 200,
                 "cache_read_input_tokens": 300, "output_tokens": 400},
            )

        est = sd.estimate_security_run(s, usage_log=usage)

        assert est["assumptions"]["basis"] == "measured_workflow_usage"
        assert est["calibration"]["reviewed_prs"] == 1
        assert est["calibration"]["legacy_verify_findings_per_pr"] == 6
        assert est["model_calls"]["verify"] == 2
        assert est["tokens"]["metered"] == 3 * 100 + 2 * 1000

    def test_estimate_honors_max(self, tmp_path, monkeypatch):
        s = Store(tmp_path)
        _pr(s, 1, head="h1")
        _pr(s, 2, head="h2")
        monkeypatch.setattr(sd, "DIFFS", tmp_path / "diffs")
        assert sd.estimate_security_run(s, max_n=1)["eligible_prs"] == 1

    def test_estimate_budget_failure_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        s = Store(tmp_path)
        _pr(s, 1, head="h1")
        monkeypatch.setattr(sd, "DIFFS", tmp_path / "diffs")
        assert sd.main(["estimate", "--store", str(tmp_path), "--max-metered-tokens", "1"]) == 2
        assert "exceed --max-metered-tokens" in capsys.readouterr().err

    def test_estimate_can_require_calibration(self, tmp_path, capsys):
        assert sd.main(["estimate", "--store", str(tmp_path), "--require-calibration"]) == 2
        assert "requires --usage-log" in capsys.readouterr().err


class TestCommit:
    def test_green_commit(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        n_ok, held, errs = sd.commit_verdicts(s, [VerdictItem.from_dict(
            {"pr": 1, "head_sha": "h1", "verdict": "GREEN", "findings": []})])
        assert (n_ok, held, errs) == (1, [], [])
        assert s.load_pr(1).section("security")["verdict"] == "GREEN"
        assert s.load_pr(1).section("security")["against_head_sha"] == "h1"

    def test_incomplete_is_held_not_written(self, tmp_path):
        """A failed-coverage run yields INCOMPLETE: no section written, no error,
        PR stays eligible so it re-runs — never a fabricated GREEN."""
        s = Store(tmp_path)
        _pr(s, 1)
        n_ok, held, errs = sd.commit_verdicts(s, [VerdictItem.from_dict(
            {"pr": 1, "head_sha": "h1", "verdict": "INCOMPLETE", "findings": [],
             "lenses_ok": 0, "lenses_total": 3})])
        assert (n_ok, held, errs) == (0, [1], [])
        assert s.load_pr(1).section("security") is None  # un-reviewed → still security-eligible
        assert [m.pr for m in sd.eligible(s, today="2026-06-10")] == [1]

    def test_red_flips_disposition_and_reopens_cluster(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, cluster=7)
        s.save_cluster({"id": 7, "root_problem": "x", "prs": [1],
                        "outcome": "merge-ready", "checked_at": NOW})
        sd.commit_verdicts(s, [VerdictItem.from_dict(
            {"pr": 1, "head_sha": "h1", "verdict": "RED",
             "findings": [{"severity": "red", "lens": "security", "category": "authz",
                           "title": "drops ownership check", "detail": "d", "location": "x.ts"}]})])
        rec = s.load_pr(1)
        assert rec.section("security")["verdict"] == "RED"
        assert rec.disposition == "needs-human"
        assert "RED" in rec.rationale
        assert s.load_cluster(7).outcome is None  # back to needs-analysis

    def test_yellow_routes_merge_to_request_changes(self, tmp_path):
        """A merge candidate that comes back YELLOW no longer reads as a merge —
        the consequence derives from the verdict itself, so there is no state in
        which a non-GREEN verdict and a merge read coexist."""
        s = Store(tmp_path)
        _pr(s, 1)   # disposition=merge
        sd.commit_verdicts(s, [VerdictItem.from_dict(
            {"pr": 1, "head_sha": "h1", "verdict": "YELLOW",
             "findings": [{"severity": "yellow", "lens": "correctness",
                           "title": "unbounded retry loop", "detail": "d", "location": "x"}]})])
        rec = s.load_pr(1)
        assert rec.disposition == "request-changes"
        assert "YELLOW" in rec.rationale

    def test_yellow_on_non_merge_pr_is_not_clobbered(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1, disposition="close-dup")
        sd.commit_verdicts(s, [VerdictItem.from_dict(
            {"pr": 1, "head_sha": "h1", "verdict": "YELLOW", "findings": []})])
        assert s.load_pr(1).section("analysis")["disposition"] == "close-dup"

    def test_invalid_verdict_rejected(self, tmp_path):
        s = Store(tmp_path)
        _pr(s, 1)
        n_ok, held, errs = sd.commit_verdicts(s, [VerdictItem.from_dict(
            {"pr": 1, "head_sha": "h1", "verdict": "MAUVE", "findings": []})])
        assert n_ok == 0 and held == [] and len(errs) == 1


class TestCommitDir:
    def test_commits_per_pr_verdict_files(self, tmp_path):
        import json
        s = Store(tmp_path)
        _pr(s, 1)
        _pr(s, 2, cluster=7)
        s.save_cluster({"id": 7, "root_problem": "x", "prs": [2],
                        "outcome": "merge-ready", "checked_at": NOW})
        outdir = tmp_path / "secout"
        outdir.mkdir()
        (outdir / "pr-1.json").write_text(json.dumps(
            {"pr": 1, "head_sha": "h1", "verdict": "GREEN", "findings": []}))
        (outdir / "pr-2.json").write_text(json.dumps(
            {"pr": 2, "head_sha": "h1", "verdict": "RED", "findings": [
                {"severity": "red", "lens": "security", "category": "authz",
                 "title": "t", "detail": "d", "location": "x"}]}))
        ok, held, errs = sd.commit_verdicts_dir(s, outdir)
        assert (ok, held, errs) == (2, [], [])
        assert s.load_pr(1).section("security")["verdict"] == "GREEN"
        assert s.load_pr(2).disposition == "needs-human"  # RED flipped the read

    def test_partial_dir_commits_finished(self, tmp_path):
        import json
        s = Store(tmp_path)
        _pr(s, 1)
        outdir = tmp_path / "secout"
        outdir.mkdir()
        (outdir / "pr-1.json").write_text(json.dumps(
            {"pr": 1, "head_sha": "h1", "verdict": "YELLOW", "findings": []}))
        ok, _, _ = sd.commit_verdicts_dir(s, outdir)
        assert ok == 1 and s.load_pr(1).section("security")["verdict"] == "YELLOW"

    def test_missing_dir_is_noop(self, tmp_path):
        s = Store(tmp_path)
        assert sd.commit_verdicts_dir(s, tmp_path / "nope") == (0, [], [])


def test_red_reopens_all_member_clusters(tmp_path):
    from pipeline import store as S
    from pipeline import security_driver
    st = S.Store(tmp_path)
    st.save_pr({"pr": 1, "meta": {"title": "t", "state": "open", "head_sha": "h1"},
                "analysis": {"disposition": "needs-human", "rationale": "r",
                             "checked_at": "t", "against_head_sha": "h1"},
                "security": {"verdict": "RED", "findings": [], "tier": "adversarial",
                             "checked_at": "t", "against_head_sha": "h1"},
                "cluster": {"ids": [7, 9], "checked_at": "t", "against_head_sha": "h1"}})
    for cid in (7, 9):
        st.save_cluster({"id": cid, "root_problem": "rp", "prs": [1],
                         "outcome": "merge-ready", "checked_at": "t"})
    security_driver._reset_cluster_on_red(st, 1, was_merge=True)
    assert st.load_cluster(7).outcome is None
    assert st.load_cluster(9).outcome is None
