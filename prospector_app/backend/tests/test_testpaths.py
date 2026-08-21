from pipeline import profile
from prospector_app.backend import testpaths as tp


def test_is_test_path_dirs():
    assert tp.is_test_path("src/tests/foo.py")
    assert tp.is_test_path("test/foo.js")
    assert tp.is_test_path("pkg/__tests__/x.tsx")
    assert tp.is_test_path("a/spec/b.rb")
    assert tp.is_test_path("e2e/login.spec.ts")


def test_is_test_path_files():
    assert tp.is_test_path("src/foo.test.ts")
    assert tp.is_test_path("src/foo.spec.js")
    assert tp.is_test_path("pkg/foo_test.go")
    assert tp.is_test_path("app/foo_spec.rb")
    assert tp.is_test_path("tests_unit/test_foo.py")


def test_is_not_test_path():
    assert not tp.is_test_path("src/foo.ts")
    assert not tp.is_test_path("src/contest.py")          # not test_/_test
    assert not tp.is_test_path("src/latest/index.js")     # 'test' not a segment
    assert not tp.is_test_path("README.md")
    assert not tp.is_test_path("")


SAMPLE_DIFF = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
+import sys
-old_line
 keep
diff --git a/tests/test_app.py b/tests/test_app.py
index 333..444 100644
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1,5 +1,8 @@
+def test_a(): pass
+def test_b(): pass
+def test_c(): pass
-def test_old(): pass
-def test_removed(): pass
"""


def test_split_unified_diff_counts():
    s = tp.split_unified_diff(SAMPLE_DIFF)
    assert s["non_test"] == {"additions": 1, "deletions": 1, "files": 1}
    assert s["test"] == {"additions": 3, "deletions": 2, "files": 1}
    assert s["removes_tests"] is False


REMOVAL_DIFF = """diff --git a/tests/test_x.py b/tests/test_x.py
--- a/tests/test_x.py
+++ b/tests/test_x.py
@@ -1,6 +1,1 @@
+def test_keep(): pass
-def test_a(): pass
-def test_b(): pass
-def test_c(): pass
"""


def test_removes_tests_flag():
    s = tp.split_unified_diff(REMOVAL_DIFF)
    assert s["removes_tests"] is True
    assert s["test"]["deletions"] == 3
    assert s["test"]["additions"] == 1


def test_empty_diff():
    s = tp.split_unified_diff("")
    assert s["test"] == {"additions": 0, "deletions": 0, "files": 0}
    assert s["non_test"] == {"additions": 0, "deletions": 0, "files": 0}
    assert s["removes_tests"] is False


def test_classify_path():
    # generated artifacts (the derived globs)
    assert tp.classify_path("pnpm-lock.yaml") == "lockfile"
    assert tp.classify_path("apps/web/package-lock.json") == "lockfile"
    assert tp.classify_path("packages/db/src/migrations/meta/0072_snapshot.json") == "migrations"
    assert tp.classify_path("ui/src/i18n/locales/de.json") == "locale"
    assert tp.classify_path("server/locales/fr.po") == "locale"
    assert tp.classify_path("node_modules/left-pad/index.js") == "vendored"
    assert tp.classify_path("dist/bundle.js") == "vendored"
    assert tp.classify_path("static/app.min.js") == "generated"
    assert tp.classify_path("assets/app.js.map") == "generated"
    assert tp.classify_path("server/src/api.d.ts") == "generated"
    assert tp.classify_path("out/app.js.map") == "vendored"   # build-dir rule wins over .map
    # real code wins as source/test, not swallowed by the artifact rules
    assert tp.classify_path("packages/db/src/migrations/0072_add_users.sql") == "source"
    assert tp.classify_path("server/src/router.ts") == "source"
    assert tp.classify_path("server/src/router.test.ts") == "test"
    assert tp.classify_path("") == "source"


def test_classify_path_generic_default_has_no_migrations_rule(monkeypatch):
    monkeypatch.setenv("TRIAGE_PROFILE", "")
    assert tp.classify_path("packages/db/src/migrations/meta/0072_snapshot.json") == "source"
    assert tp.classify_path("drizzle/meta/0001_snapshot.json") == "source"
    assert tp.classify_path("pnpm-lock.yaml") == "lockfile"


def test_classify_path_override_profile_rules(monkeypatch):
    monkeypatch.setattr(profile, "active", lambda: profile.RepoProfile(
        artifact_rules=(profile.ArtifactRule("vendored", "(^|/)third_party/"),)))
    assert tp.classify_path("third_party/lib.c") == "vendored"
    assert tp.classify_path("pnpm-lock.yaml") == "source"  # override replaces, never merges


# A diff shaped like the real offenders: a little hand-written source, a giant
# regenerated migration snapshot, and a lockfile bump.
NOISY_DIFF = """diff --git a/server/src/router.ts b/server/src/router.ts
--- a/server/src/router.ts
+++ b/server/src/router.ts
@@ -1,2 +1,4 @@
+const a = 1
+const b = 2
-const old = 0
diff --git a/packages/db/src/migrations/meta/0072_snapshot.json b/packages/db/src/migrations/meta/0072_snapshot.json
--- a/packages/db/src/migrations/meta/0072_snapshot.json
+++ b/packages/db/src/migrations/meta/0072_snapshot.json
@@ -1,1 +1,5 @@
+{ "a": 1 }
+{ "b": 2 }
+{ "c": 3 }
+{ "d": 4 }
-{ "old": 0 }
diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1,1 +1,3 @@
+dep-a
+dep-b
"""


def test_loc_breakdown_strips_artifacts():
    b = tp.loc_breakdown(NOISY_DIFF)
    assert b["effective"] == 3          # 2 added + 1 removed of source
    assert b["raw"] == 3 + 5 + 2        # source + migration + lockfile lines
    assert b["artifact"] == 7
    assert b["dominant_artifact"] == "migrations"
    assert b["by_category"]["source"] == {"additions": 2, "deletions": 1, "files": 1}
    assert b["by_category"]["migrations"]["additions"] == 4
    assert "test" not in b["by_category"]   # only non-empty categories listed


def test_loc_breakdown_from_files_matches_diff_semantics():
    files = [
        {"filename": "server/src/router.ts", "additions": 2, "deletions": 1},
        {"filename": "packages/db/src/migrations/meta/0072_snapshot.json", "additions": 4, "deletions": 1},
        {"filename": "pnpm-lock.yaml", "additions": 2, "deletions": 0},
    ]
    b = tp.loc_breakdown_from_files(files)
    assert b["effective"] == 3
    assert b["raw"] == 10
    assert b["dominant_artifact"] == "migrations"


def test_loc_breakdown_empty():
    b = tp.loc_breakdown("")
    assert b == {"effective": 0, "raw": 0, "artifact": 0,
                 "dominant_artifact": None, "by_category": {}}
