"""Redundancy signal: hunk post-image matching against upstream file contents,
and the cached/circuit-broken `gh` reader."""
from pipeline import redundancy

# Current upstream copy of the target file (ends with a newline, as a raw
# contents fetch does).
MASTER = "\n".join([
    "export const AGENT_ADAPTER_TYPES = [",
    '  "openai",',
    '  "gemini_local",',
    "] as const;",
    "",
])

# The motivating shape (#388 / upstream cluster 130): the PR re-adds a line the
# default branch already carries, at the same position.
DIFF_READD = """\
diff --git a/packages/shared/src/constants.ts b/packages/shared/src/constants.ts
index 1111111..2222222 100644
--- a/packages/shared/src/constants.ts
+++ b/packages/shared/src/constants.ts
@@ -1,3 +1,4 @@
 export const AGENT_ADAPTER_TYPES = [
   "openai",
+  "gemini_local",
 ] as const;
"""

DIFF_NET_NEW = """\
diff --git a/packages/shared/src/constants.ts b/packages/shared/src/constants.ts
index 1111111..2222222 100644
--- a/packages/shared/src/constants.ts
+++ b/packages/shared/src/constants.ts
@@ -1,3 +1,4 @@
 export const AGENT_ADAPTER_TYPES = [
   "openai",
+  "novel_adapter",
 ] as const;
"""

DIFF_MODIFY = """\
diff --git a/config.ts b/config.ts
index 1111111..2222222 100644
--- a/config.ts
+++ b/config.ts
@@ -1,3 +1,3 @@
 export const LIMITS = [
-  "low",
+  "high",
 ] as const;
"""

DIFF_DELETE = """\
diff --git a/packages/shared/src/constants.ts b/packages/shared/src/constants.ts
index 1111111..2222222 100644
--- a/packages/shared/src/constants.ts
+++ b/packages/shared/src/constants.ts
@@ -1,4 +1,3 @@
 export const AGENT_ADAPTER_TYPES = [
   "openai",
-  "gemini_local",
 ] as const;
"""

DIFF_NEW_FILE = """\
diff --git a/new.ts b/new.ts
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/new.ts
@@ -0,0 +1,2 @@
+export const A = 1;
+export const B = 2;
"""

DIFF_DELETED_FILE = """\
diff --git a/old.ts b/old.ts
deleted file mode 100644
index 1111111..0000000
--- a/old.ts
+++ /dev/null
@@ -1,2 +0,0 @@
-export const A = 1;
-export const B = 2;
"""


class TestCompute:
    def test_readd_of_present_line_is_redundant(self):
        r = redundancy.compute(DIFF_READD, lambda p: MASTER)
        assert r == {"hunks": 1, "redundant": 1, "unchecked": 0,
                     "files": {"packages/shared/src/constants.ts":
                               {"hunks": 1, "redundant": 1}}}

    def test_net_new_addition_is_not_redundant(self):
        r = redundancy.compute(DIFF_NET_NEW, lambda p: MASTER)
        assert r == {"hunks": 1, "redundant": 0, "unchecked": 0, "files": {}}

    def test_modification_already_applied_is_redundant(self):
        applied = 'export const LIMITS = [\n  "high",\n] as const;\n'
        r = redundancy.compute(DIFF_MODIFY, lambda p: applied)
        assert (r["redundant"], r["hunks"]) == (1, 1)

    def test_modification_not_applied_is_not_redundant(self):
        original = 'export const LIMITS = [\n  "low",\n] as const;\n'
        r = redundancy.compute(DIFF_MODIFY, lambda p: original)
        assert (r["redundant"], r["hunks"]) == (0, 1)

    def test_deletion_already_applied_is_redundant(self):
        without = ('export const AGENT_ADAPTER_TYPES = [\n  "openai",\n'
                   "] as const;\n")
        r = redundancy.compute(DIFF_DELETE, lambda p: without)
        assert (r["redundant"], r["hunks"]) == (1, 1)

    def test_deletion_not_applied_is_not_redundant(self):
        r = redundancy.compute(DIFF_DELETE, lambda p: MASTER)
        assert (r["redundant"], r["hunks"]) == (0, 1)

    def test_new_file_identical_upstream_is_redundant(self):
        r = redundancy.compute(DIFF_NEW_FILE,
                               lambda p: "export const A = 1;\nexport const B = 2;\n")
        assert (r["redundant"], r["hunks"]) == (1, 1)

    def test_new_file_absent_upstream_is_not_redundant(self):
        r = redundancy.compute(DIFF_NEW_FILE, lambda p: "")
        assert (r["redundant"], r["hunks"]) == (0, 1)

    def test_deleted_file_yields_no_checkable_hunks(self):
        assert redundancy.compute(DIFF_DELETED_FILE, lambda p: MASTER) is None

    def test_unreadable_file_counts_as_unchecked(self):
        r = redundancy.compute(DIFF_READD, lambda p: None)
        assert r == {"hunks": 0, "redundant": 0, "unchecked": 1, "files": {}}

    def test_files_breakdown_lists_only_redundant_paths(self):
        contents = {"packages/shared/src/constants.ts": MASTER,
                    "new.ts": ""}
        r = redundancy.compute(DIFF_READD + DIFF_NEW_FILE, contents.__getitem__)
        assert (r["hunks"], r["redundant"]) == (2, 1)
        assert list(r["files"]) == ["packages/shared/src/constants.ts"]

    def test_empty_diff_is_none(self):
        assert redundancy.compute("", lambda p: MASTER) is None


class _Res:
    def __init__(self, code: int, out: str = "", err: str = ""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


class TestMasterTree:
    def test_success_is_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(redundancy.subprocess, "run",
                            lambda cmd, **kw: (calls.append(cmd), _Res(0, out="body\n"))[1])
        t = redundancy.MasterTree(repo="o/r")
        assert t.read("a/b.ts") == "body\n"
        assert t.read("a/b.ts") == "body\n"
        assert len(calls) == 1
        assert calls[0][2] == "repos/o/r/contents/a/b.ts"

    def test_404_reads_as_absent(self, monkeypatch):
        monkeypatch.setattr(redundancy.subprocess, "run",
                            lambda cmd, **kw: _Res(1, err="gh: Not Found (HTTP 404)"))
        assert redundancy.MasterTree(repo="o/r").read("gone.ts") == ""

    def test_hard_failures_trip_the_breaker(self, monkeypatch):
        calls = []
        monkeypatch.setattr(redundancy.subprocess, "run",
                            lambda cmd, **kw: (calls.append(cmd), _Res(1, err="HTTP 401"))[1])
        t = redundancy.MasterTree(repo="o/r")
        for n in range(10):
            assert t.read(f"f{n}.ts") is None
        assert len(calls) == redundancy.MasterTree._MAX_FAILURES
