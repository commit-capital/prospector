"""Path→risk-tier policy: profile-driven glob map, severity precedence, rollup.

Runs under the fixture profile (root conftest); the generic-default tests
clear PROFILE_PATH per test — the same consumer against two profiles."""
from pipeline import risktier, settings


class TestFixtureProfile:
    def test_tier0(self):
        for p in ("server/core/locks.ts", ".github/workflows/ci.yml",
                  "package.json", "packages/x/package.json", "pnpm-lock.yaml"):
            assert risktier.classify_path(p) == 0, p

    def test_tier1(self):
        assert risktier.classify_path("db/schema/runs.ts") == 1
        assert risktier.classify_path("server/routes/approvals.ts") == 1

    def test_instruction_paths_pin_at_default_tier(self):
        assert risktier.classify_path("skills/foo/SKILL.md") == 2
        assert risktier.classify_path("skills/tests/thing.md") == 2   # dir looks test-y
        assert risktier.classify_path("AGENTS.md") == 2

    def test_tier3_leaf_and_tests(self):
        assert risktier.classify_path("ui/app.tsx") == 3
        assert risktier.classify_path("docs/guide.md") == 3
        assert risktier.classify_path("src/__tests__/x.test.ts") == 3

    def test_tier0_wins_over_test_convention(self):
        assert risktier.classify_path(".github/workflows/e2e/nightly.yml") == 0

    def test_tier0_wins_over_tier1_overlap(self):
        assert risktier.classify_path("server/core/locks.ts") == 0   # also matches tier1 server/**

    def test_unmatched_falls_to_default(self):
        assert risktier.classify_path("lib/anything/else.ts") == 2
        assert risktier.classify_path("") == 2
        assert risktier.classify_path("./ui/app.tsx") == 3   # ./ stripped first


class TestGenericDefault:
    def test_supply_chain_is_tier0(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "")
        for p in (".github/workflows/ci.yml", "package.json", "uv.lock",
                  "Cargo.lock", "go.sum", "pyproject.toml"):
            assert risktier.classify_path(p) == 0, p

    def test_everything_else_defaults_except_tests(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "")
        assert risktier.classify_path("server/core/locks.ts") == 2
        assert risktier.classify_path("docs/guide.md") == 2          # no tier3 globs
        assert risktier.classify_path("src/foo.test.ts") == 3        # test convention holds


class TestRollup:
    def test_pr_tier_is_min(self):
        assert risktier.pr_tier(["ui/app.tsx", "package.json"]) == 0
        assert risktier.pr_tier(["ui/app.tsx", "docs/guide.md"]) == 3
        assert risktier.pr_tier([]) is None

    def test_tier_facet_names_the_pinning_paths(self):
        f = risktier.tier_facet(["ui/app.tsx", "db/schema/runs.ts", "server/routes/approvals.ts"])
        assert f["tier"] == 1
        assert f["pinned_by"] == ["db/schema/runs.ts", "server/routes/approvals.ts"]
        assert risktier.tier_facet([]) == {"tier": None, "pinned_by": []}
