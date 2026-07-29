"""CODEOWNERS gating: profile-driven globs + owners.

Runs under the fixture profile (root conftest); the generic-default tests
clear PROFILE_PATH per test — the same consumer against two profiles."""
from pipeline import codeowners as co
from pipeline import settings


class TestFixtureProfile:
    def test_gated(self):
        for p in (".github/workflows/pr.yml", ".github/CODEOWNERS",
                  "skills/foo/SKILL.md", "package.json",
                  "packages/core/package.json", "pnpm-lock.yaml",
                  "scripts/release-foo.mjs"):
            assert co.is_gated(p), p

    def test_not_gated(self):
        for p in ("src/index.ts", "scripts/build.sh", "package-lock.json", ""):
            assert not co.is_gated(p), p

    def test_human_merge(self):
        hm = co.human_merge(["src/a.ts", ".github/workflows/ci.yml", "package.json"])
        assert hm is not None
        assert hm["required"] is True
        assert set(hm["paths"]) == {".github/workflows/ci.yml", "package.json"}
        assert hm["owners"] == ["@owner-a", "@owner-b"]   # fixture profile owners
        assert co.human_merge(["src/a.ts", "README.md"]) is None


class TestGenericDefault:
    def test_nothing_gated(self, monkeypatch):
        monkeypatch.setattr(settings, "PROFILE_PATH", "")
        assert not co.is_gated(".github/workflows/pr.yml")
        assert not co.is_gated("package.json")
        assert co.human_merge([".github/workflows/ci.yml", "package.json"]) is None
