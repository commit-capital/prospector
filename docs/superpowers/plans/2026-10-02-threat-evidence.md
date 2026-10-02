# Threat Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the threat scan flags a PR malicious, append an immutable, inert evidence capture (full diff pinned to the flagged SHA, the pre-force-push diff, PR/commit/actor/force-push metadata, match locations, hashes) to the shared store, retrievable from the CLI and the PR page.

**Architecture:** A new `threat_evidence` table (insert-only, gzip blobs + payload-free JSON record) behind four `Store` accessors. `pipeline/threat_evidence.py` owns capture (read-only GitHub reads behind a small injectable interface), export/zip, verification and the CLI. `threat_scan.main` calls `capture` after every verdict is stamped. The app serves metadata and a hash-checked zip; the PR page's malicious callout lists captures with a download link.

**Tech Stack:** Python 3.14, SQLAlchemy (SQLite + Postgres), `gh` CLI, FastAPI, React + TypeScript (pnpm).

**Spec:** `docs/superpowers/specs/2026-10-02-threat-evidence-design.md`

## Global Constraints

- Every module starts with `from __future__ import annotations`; no quoted annotations; precise types everywhere; `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness` stays at 0 errors.
- `uv run ruff check .` stays at zero findings.
- Imports are qualified (`from pipeline import …`); no `sys.path` edits.
- Comments describe the code as it is: no "previously", "instead of", "would otherwise".
- `STORE_SCHEMA_VERSION` becomes 29.
- Diff cap before compression: `MAX_DIFF_BYTES = 25_000_000`. Match cap: `MAX_MATCHES = 500`.
- Payload text never enters `data`, the ledger, logs, or any agent prompt. Never copy the scan's `detail` excerpts.
- Capture makes read-only GitHub calls only and never writes a diff to disk.
- Frontend: `pnpm run build` passes; `pnpm exec eslint <touched files>` adds no errors.

## Review Focus

1. A diff that is not valid UTF-8: stored bytes must be exact; matching decodes with `errors="replace"`; the derived `force-push-changes.diff` re-encodes with `surrogateescape` so its bytes are exactly the source blocks. (Task 4 test `test_non_utf8_diff_round_trips`.)
2. The PR's head moved after the scan: capture must fetch the flagged SHA, never the current head, and must not use the per-file listing (which only describes the current head). (Task 4 test `test_head_moved_fetches_flagged_sha_and_skips_listing`.)
3. A scan run with no network (`--no-fetch`, tests, offline machine): no GitHub call is made and the scan's verdicts, registry and ledger entry land unchanged. (Task 6 tests.)
4. The Tables tab over the new table: binary columns render as a byte count, never bytes, and the overview still serializes. (Task 3 test `test_binary_columns_render_as_sizes`.)
5. Repeated scans of a PR whose diff GitHub refuses: rows stay bounded (at most one partial and one complete per head). (Task 4 test `test_second_partial_is_not_written`.)

---

### Task 1: `gh.gh_bytes`, a raw-body GitHub read

**Files:**
- Modify: `pipeline/gh.py` (after `gh_list`)
- Test: `pipeline/tests/test_gh.py`

**Interfaces:**
- Produces: `gh.gh_bytes(path: str, *, accept: str, timeout: int = 120) -> bytes | None`

- [ ] **Step 1: Write the failing tests** (append to `pipeline/tests/test_gh.py`)

```python
def test_gh_bytes_returns_raw_body_with_accept_header(monkeypatch):
    seen: list[list[str]] = []

    def fake_run(argv, **kw):
        seen.append(argv)
        assert "text" not in kw  # bytes, undecoded
        return subprocess.CompletedProcess(argv, 0, stdout=b"diff --git a/x b/x\n\xff", stderr=b"")

    monkeypatch.setattr(gh.subprocess, "run", fake_run)
    body = gh.gh_bytes("repos/o/r/compare/a...b", accept="application/vnd.github.diff")
    assert body == b"diff --git a/x b/x\n\xff"
    assert seen[0] == ["gh", "api", "-H", "Accept: application/vnd.github.diff",
                       "repos/o/r/compare/a...b"]


def test_gh_bytes_is_none_on_failure_or_flag_path(monkeypatch):
    monkeypatch.setattr(gh.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"404"))
    assert gh.gh_bytes("repos/o/r/compare/a...b", accept="x") is None
    assert gh.gh_bytes("--paginate", accept="x") is None
```

(Use the module's existing `import subprocess` / `from pipeline import gh` imports; add them if absent.)

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_gh.py -k gh_bytes -q` — expect FAIL (`gh_bytes` undefined).

- [ ] **Step 3: Implement** in `pipeline/gh.py`:

```python
def gh_bytes(path: str, *, accept: str, timeout: int = 120) -> bytes | None:
    """`gh api <path>` sent with `Accept: <accept>`, returning the response body
    undecoded, or None on any failure (non-zero exit, timeout). For media types
    that are not JSON, such as a diff. A path gh would parse as a flag is
    refused."""
    if path.startswith("-"):
        return None
    try:
        res = subprocess.run(["gh", "api", "-H", f"Accept: {accept}", path],
                             capture_output=True, timeout=timeout, env=operator_env())
    except (subprocess.SubprocessError, OSError):
        return None
    return res.stdout if res.returncode == 0 else None
```

- [ ] **Step 4: Run** the same tests — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Add gh.gh_bytes for non-JSON GitHub reads"`

---

### Task 2: `threats.locate`, where each signature matches

**Files:**
- Modify: `pipeline/threats.py`
- Test: `pipeline/tests/test_threats.py`

**Interfaces:**
- Produces: `threats.Match` (frozen dataclass: `signature: str, file: str | None, diff_line: int`) and `threats.locate(diff_text: str, *, limit: int | None = None) -> list[Match]`.
- `scan_diff` keeps its exact return value; its per-line matching moves into `_line_signatures(fname: str | None, body: str) -> list[str]`, used by both.

- [ ] **Step 1: Write the failing tests**

```python
class TestLocate:
    def test_locate_reports_file_and_1_based_diff_line(self):
        hits = threats.locate(PAYLOAD_DIFF)
        lines = PAYLOAD_DIFF.split("\n")
        assert {h.signature for h in hits} == {
            "obfuscated-self-decoder", "capability-smuggle", "build-config-require-injection"}
        for h in hits:
            assert h.file == "cli/esbuild.config.mjs"
            assert lines[h.diff_line - 1].startswith("+")

    def test_locate_agrees_with_scan_diff_on_line_signatures(self):
        for diff in [PAYLOAD_DIFF, CLEAN_DIFF, LEAKED_KEY_DIFF, *FP_DIFFS.values(),
                     *REAL_LEAK_DIFFS.values()]:
            line_sigs = set(threats.scan_diff(diff)["signatures"]) - {"eol-churn-camouflage"}
            assert {h.signature for h in threats.locate(diff)} == line_sigs

    def test_locate_honours_limit(self):
        many = PAYLOAD_DIFF + PAYLOAD_DIFF.replace("cli/", "web/")
        assert len(threats.locate(many, limit=2)) == 2
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_threats.py -k Locate -q` — expect FAIL.

- [ ] **Step 3: Implement.** In `threats.py` add `from dataclasses import dataclass`, then replace the per-line block of `scan_diff` with a shared helper:

```python
_LINE_SIGNATURES = ("obfuscated-self-decoder", "capability-smuggle",
                    "build-config-require-injection", "secret-leak")


def _line_signatures(fname: str | None, body: str) -> list[str]:
    """The line-pattern signatures one added line fires, in _LINE_SIGNATURES
    order: the decoder and smuggle patterns anywhere, the require injection only
    in a build-config file, and a secret leak outside the excluded files."""
    pats = {name: patterns for name, _, _, patterns in SIGNATURES}
    fired: list[str] = []
    for name in ("obfuscated-self-decoder", "capability-smuggle"):
        if any(p.search(body) for p in pats[name]):
            fired.append(name)
    if fname and _BUILD_CONFIG.search(fname) and any(
            p.search(body) for p in pats["build-config-require-injection"]):
        fired.append("build-config-require-injection")
    provider_hit = any(p.search(body) for p in pats["secret-leak"])
    if (provider_hit and not (fname and _SECRET_EXCLUDE_FILE.search(fname))) \
            or _secret_evidence(fname, body):
        fired.append("secret-leak")
    return fired
```

and in `scan_diff` the loop becomes:

```python
    for fname, body in _added_lines_by_file(diff_text):
        for name in _line_signatures(fname, body):
            if name in fired:
                continue
            if name in ("build-config-require-injection", "secret-leak"):
                fired[name] = f"{fname or '?'}: {_evidence(body)}"
            else:
                fired[name] = _evidence(body)
```

(`build-config-require-injection` only fires with a filename, so `fname or '?'` renders it exactly as before.) Then add:

```python
@dataclass(frozen=True)
class Match:
    """Where one signature fired: the diff's target file and the 1-based line of
    the added line within the diff text (lines split on "\\n")."""
    signature: str
    file: str | None
    diff_line: int


def locate(diff_text: str, *, limit: int | None = None) -> list[Match]:
    """Every (signature, file, line) the line-pattern signatures fire on over the
    added lines of `diff_text`, in diff order, at most `limit` of them. The
    churn-camouflage signature is a whole-diff measure and has no location."""
    out: list[Match] = []
    current: str | None = None
    for i, line in enumerate((diff_text or "").split("\n"), start=1):
        if line.startswith("+++ "):
            current = line[4:].strip().removeprefix("b/")
            continue
        if not line.startswith("+"):
            continue
        for name in _line_signatures(current, line[1:]):
            out.append(Match(name, current, i))
            if limit is not None and len(out) >= limit:
                return out
    return out
```

- [ ] **Step 4: Run** `uv run pytest pipeline/tests/test_threats.py -q` — expect the whole file PASS (scan_diff unchanged).
- [ ] **Step 5: Commit** `git commit -m "threats.locate: report where each signature matches"`

---

### Task 3: The `threat_evidence` table, its Store accessors, and the Tables tab

**Files:**
- Modify: `pipeline/schema.py` (table, `STORE_SCHEMA_VERSION = 29` + history line)
- Modify: `pipeline/storekit.py` (dataclasses `EvidenceRecord`, `EvidenceBlobs`)
- Modify: `pipeline/store.py` (four accessors)
- Modify: `prospector_app/backend/tables.py` (binary columns as sizes; description)
- Test: `pipeline/tests/test_threat_evidence_store.py` (new), `prospector_app/backend/tests/test_tables.py`

**Interfaces:**
- Produces:
  - `storekit.EvidenceRecord(id: int, pr: int, head_sha: str, author: str | None, captured_at: str, complete: bool, data: dict)`
  - `storekit.EvidenceBlobs(diff_gz: bytes | None, prior_gz: bytes | None)`
  - `Store.append_threat_evidence(*, pr: int, head_sha: str, author: str | None, captured_at: str, complete: bool, data: dict, diff_gz: bytes | None, prior_gz: bytes | None) -> int`
  - `Store.threat_evidence(*, pr: int | None = None, author: str | None = None) -> list[storekit.EvidenceRecord]` (newest first, no blobs)
  - `Store.threat_evidence_record(capture_id: int) -> storekit.EvidenceRecord | None`
  - `Store.threat_evidence_blobs(capture_id: int) -> storekit.EvidenceBlobs | None`

- [ ] **Step 1: Write the failing tests** — `pipeline/tests/test_threat_evidence_store.py`:

```python
"""The threat_evidence table: insert-only rows, metadata reads without blobs."""
from __future__ import annotations

import pytest

from pipeline import schema
from pipeline.store import Store
from pipeline.storekit import ValidationError


def _append(store: Store, pr: int = 7, head: str = "h1", *, author: str | None = "mallory",
            at: str = "2026-10-02T17:00:00+00:00", complete: bool = True) -> int:
    return store.append_threat_evidence(
        pr=pr, head_sha=head, author=author, captured_at=at, complete=complete,
        data={"pr": pr, "head_sha": head}, diff_gz=b"\x1f\x8bdiff", prior_gz=None)


def test_append_returns_id_and_reads_back_without_blobs(tmp_path):
    store = Store(tmp_path)
    cid = _append(store)
    [rec] = store.threat_evidence(pr=7)
    assert rec.id == cid and rec.head_sha == "h1" and rec.complete is True
    assert rec.data == {"pr": 7, "head_sha": "h1"}
    blobs = store.threat_evidence_blobs(cid)
    assert blobs is not None and blobs.diff_gz == b"\x1f\x8bdiff" and blobs.prior_gz is None


def test_listing_is_newest_first_and_filters(tmp_path):
    store = Store(tmp_path)
    _append(store, 7, "a", at="2026-10-01T00:00:00+00:00")
    _append(store, 7, "b", at="2026-10-02T00:00:00+00:00")
    _append(store, 8, "c", author="eve")
    assert [r.head_sha for r in store.threat_evidence(pr=7)] == ["b", "a"]
    assert [r.pr for r in store.threat_evidence(author="eve")] == [8]
    assert len(store.threat_evidence()) == 3


def test_record_and_blobs_for_unknown_id_are_none(tmp_path):
    store = Store(tmp_path)
    assert store.threat_evidence_record(99) is None
    assert store.threat_evidence_blobs(99) is None


def test_append_validates(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(ValidationError):
        store.append_threat_evidence(pr=7, head_sha="", author=None, captured_at="x",
                                     complete=False, data={}, diff_gz=None, prior_gz=None)
    with pytest.raises(ValidationError):
        store.append_threat_evidence(pr=7, head_sha="h", author=None, captured_at="x",
                                     complete=False, data={}, diff_gz="text",  # type: ignore[arg-type]
                                     prior_gz=None)


def test_store_has_no_update_or_delete_for_evidence():
    names = [n for n in dir(Store) if "evidence" in n]
    assert sorted(names) == ["append_threat_evidence", "threat_evidence",
                             "threat_evidence_blobs", "threat_evidence_record"]


def test_schema_version_is_29():
    assert schema.STORE_SCHEMA_VERSION == 29
```

and in `prospector_app/backend/tests/test_tables.py`:

```python
def test_binary_columns_render_as_sizes(temp_store):
    from pipeline import schema, storekit
    eng = storekit.get_engine(temp_store)
    with eng.begin() as conn:
        conn.execute(schema.threat_evidence.insert().values(
            pr=1, head_sha="h", author="a", captured_at="2026-10-02T00:00:00+00:00",
            complete=True, data={"pr": 1}, diff_gz=b"\x1f\x8b" + b"\xff" * 98, prior_gz=None))
    ov = next(s for s in tables.overview() if s["name"] == "threat_evidence")
    assert ov["preview"][0]["diff_gz"] == "100 bytes (binary, not shown)"
    assert ov["preview"][0]["prior_gz"] is None
    page = tables.rows("threat_evidence")
    assert page["rows"][0]["diff_gz"] == "100 bytes (binary, not shown)"
    import json
    json.dumps(ov)  # serializable
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_threat_evidence_store.py prospector_app/backend/tests/test_tables.py -q` — expect FAIL.

- [ ] **Step 3: Implement.**

`pipeline/schema.py`: import `LargeBinary`; add history line and bump:

```python
# 29 — the threat_evidence table (an append-only capture of each PR the threat
#      scan flags malicious); an older threat scan flags a malicious PR without
#      preserving its evidence.
STORE_SCHEMA_VERSION = 29
```

and after `diffs`:

```python
# Evidence of each PR the threat scan flagged malicious, one row per capture of a
# flagged head (pipeline/threat_evidence.py is the writer). Rows are insert-only.
# The diffs live gzip-compressed in the binary columns; `data` is the capture's
# record and holds no diff text, so nothing reading the table as text sees a
# payload.
threat_evidence = Table(
    "threat_evidence", METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr", Integer, index=True),
    Column("head_sha", String, index=True),
    Column("author", String, index=True),
    Column("captured_at", String, index=True),
    Column("complete", Boolean),
    Column("data", _JSON, nullable=False),
    Column("diff_gz", LargeBinary),
    Column("prior_gz", LargeBinary),
)
```

`pipeline/storekit.py` (beside `PhaseRun`):

```python
@dataclass(frozen=True)
class EvidenceRecord:
    """One threat_evidence row without its diff blobs."""
    id: int
    pr: int
    head_sha: str
    author: str | None
    captured_at: str
    complete: bool
    data: dict


@dataclass(frozen=True)
class EvidenceBlobs:
    """A threat_evidence row's gzip-compressed diffs."""
    diff_gz: bytes | None
    prior_gz: bytes | None
```

`pipeline/store.py` (new section after the shared diff cache):

```python
    # -- Threat evidence -----------------------------------------------------
    # One row per capture of a head the threat scan flagged malicious
    # (`threat_evidence` table, pipeline/threat_evidence.py the writer). Rows are
    # insert-only: there is no update or delete accessor.
    _EVIDENCE_META = ("id", "pr", "head_sha", "author", "captured_at", "complete", "data")

    def append_threat_evidence(self, *, pr: int, head_sha: str, author: str | None,
                               captured_at: str, complete: bool, data: dict,
                               diff_gz: bytes | None, prior_gz: bytes | None) -> int:
        """Insert one capture and return its id. Validated."""
        from sqlalchemy import insert
        if not isinstance(pr, int) or isinstance(pr, bool):
            raise ValidationError("threat_evidence.pr: required int")
        if not head_sha or not isinstance(head_sha, str):
            raise ValidationError("threat_evidence.head_sha: required str")
        if not captured_at or not isinstance(captured_at, str):
            raise ValidationError("threat_evidence.captured_at: required str")
        if not isinstance(data, dict):
            raise ValidationError("threat_evidence.data: required dict")
        for name, blob in (("diff_gz", diff_gz), ("prior_gz", prior_gz)):
            if blob is not None and not isinstance(blob, bytes):
                raise ValidationError(f"threat_evidence.{name}: bytes or None")
        storekit.assert_writable(self.engine)
        with self.engine.begin() as conn:
            res = conn.execute(insert(schema.threat_evidence).values(
                pr=pr, head_sha=head_sha, author=author, captured_at=captured_at,
                complete=bool(complete), data=data, diff_gz=diff_gz, prior_gz=prior_gz))
            return int(res.inserted_primary_key[0])

    def _evidence_meta(self, row: Sequence) -> storekit.EvidenceRecord:  # type per pyright
        return storekit.EvidenceRecord(id=int(row[0]), pr=int(row[1]), head_sha=row[2],
                                       author=row[3], captured_at=row[4],
                                       complete=bool(row[5]), data=row[6])

    def threat_evidence(self, *, pr: int | None = None,
                        author: str | None = None) -> list[storekit.EvidenceRecord]:
        """Captures, newest first, without their blobs; `pr` and `author` filter."""
        from sqlalchemy import select
        t = schema.threat_evidence
        stmt = select(*(t.c[c] for c in self._EVIDENCE_META))
        if pr is not None:
            stmt = stmt.where(t.c.pr == pr)
        if author is not None:
            stmt = stmt.where(t.c.author == author)
        stmt = stmt.order_by(t.c.captured_at.desc(), t.c.id.desc())
        rows = storekit.read_retrying(self.engine, lambda conn: conn.execute(stmt).all())
        return [self._evidence_meta(r) for r in rows]

    def threat_evidence_record(self, capture_id: int) -> storekit.EvidenceRecord | None:
        from sqlalchemy import select
        t = schema.threat_evidence
        stmt = select(*(t.c[c] for c in self._EVIDENCE_META)).where(t.c.id == capture_id)
        row = storekit.read_retrying(self.engine, lambda conn: conn.execute(stmt).first())
        return None if row is None else self._evidence_meta(row)

    def threat_evidence_blobs(self, capture_id: int) -> storekit.EvidenceBlobs | None:
        from sqlalchemy import select
        t = schema.threat_evidence
        stmt = select(t.c.diff_gz, t.c.prior_gz).where(t.c.id == capture_id)
        row = storekit.read_retrying(self.engine, lambda conn: conn.execute(stmt).first())
        return None if row is None else storekit.EvidenceBlobs(diff_gz=row[0], prior_gz=row[1])
```

(Type `_evidence_meta`'s `row` as `sqlalchemy.engine.Row[Any]` under `TYPE_CHECKING`; make it a `@staticmethod`.)

`prospector_app/backend/tables.py`: import `LargeBinary`; add a `DESCRIPTIONS` entry:

```python
    "threat_evidence": "Append-only evidence of each PR the threat scan flagged "
                       "malicious: the capture's record in `data`, the diffs "
                       "gzip-compressed in binary columns (shown as sizes only).",
```

and select binary columns as their length:

```python
def _selectable(tbl: Table) -> list[ColumnElement[Any]]:
    """The table's columns for a page read, a binary column read as its byte
    length so no blob leaves the database."""
    return [func.length(c).label(c.name) if isinstance(c.type, LargeBinary) else c
            for c in tbl.columns]


def _binary_sizes(tbl: Table, row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    for c in tbl.columns:
        if isinstance(c.type, LargeBinary) and row.get(c.name) is not None:
            row[c.name] = f"{int(row[c.name]):,} bytes (binary, not shown)"
    return row
```

In `overview()` use `select(*_selectable(tbl))` and `rows = [_binary_sizes(tbl, dict(r)) for r in raw]`; in `rows()` start `page_stmt = select(*_selectable(tbl))` and build `page = [_binary_sizes(tbl, dict(r)) for r in raw]`.

- [ ] **Step 4: Run** the two test files, plus `uv run pytest pipeline/tests/test_schema_fingerprint.py pipeline/tests/test_schema.py pipeline/tests/test_schema_guard.py -q` — expect PASS (fix any test that pins version 28).
- [ ] **Step 5: Commit** `git commit -m "Add the threat_evidence table and its insert-only accessors"`

---

### Task 4: `threat_evidence.capture`

**Files:**
- Create: `pipeline/threat_evidence.py`
- Test: `pipeline/tests/test_threat_evidence.py` (new)

**Interfaces:**
- Consumes: `gh.gh_bytes`, `gh.gh_json`, `gh.gh_graphql`, `gh.pr_files`, `gh.operator_login`, `threats.locate`, `threats.scan_diff`, the Task 3 Store accessors.
- Produces:
  - `Flag(pr: int, head_sha: str, author: str | None, signatures: list[str], scanned_at: str | None)` (frozen dataclass)
  - `GitHubReads` (Protocol) and `LiveGitHub` (its `gh` implementation): `pull(n) -> dict | None`, `compare(base, head) -> dict | None`, `compare_diff(base, head) -> bytes | None`, `listing_diff(n) -> tuple[bytes, bool] | None`, `force_pushes(n) -> list[ForcePush] | None`, `user(login) -> dict | None`, `login() -> str | None`
  - `ForcePush(at: str | None, actor: str | None, before: str | None, after: str | None)`
  - `Status = Literal["captured", "partial", "failed", "already"]`
  - `capture(store: Store, flag: Flag, *, github: GitHubReads | None = None, diffs_dir: Path | None = None) -> Status`

Behaviour (each point has a test below):
1. A complete capture for `(pr, head_sha)` already stored → `"already"`, no GitHub call.
2. Base = `pull["base"]["sha"]`, else `settings.default_branch()`.
3. Diff: `compare_diff(base, head)` (source `compare`, complete); else, only when `pull["head"]["sha"] == head`, `listing_diff(n)` (source `per-file-listing`, complete iff every file carried a patch); else the scan cache `diffs_dir/<head>.diff` (source `scan-cache`, incomplete). Bytes over `MAX_DIFF_BYTES` are cut, `truncated: True`, incomplete.
4. Prior: the latest force-push whose `after == head` with a `before` → `compare_diff(base, before)`; recorded as `artifacts.prior` with `before_sha`.
5. Commits from `compare(base, head)["commits"]`; actor from `user(flag.author or pull.user.login)`.
6. Read failures go to `errors`; they do not make a capture incomplete (completeness is the diff's).
7. Nothing at all fetched (no pull, no diff, no commits) → no row, `"failed"`.
8. An incomplete capture is written only when no incomplete row exists for that head; otherwise `"failed"` and no write.
9. `data` holds no diff text; matches are `{signature, file, diff_line}` from `threats.locate(..., limit=MAX_MATCHES)`.
10. Every written capture appends a `threat-evidence:capture` ledger run.

- [ ] **Step 1: Write the failing tests** — `pipeline/tests/test_threat_evidence.py`:

```python
"""threat_evidence.capture: an immutable, payload-free record of a flagged head,
with its diffs gzip-compressed and hashed, from read-only GitHub reads."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from pipeline import threat_evidence as te
from pipeline.store import Store

PAYLOAD = "global['!']='9-0008-2';var _$_1e42=(function(l,e){var x=String.fromCharCode(127);})"
HEAD = "e" * 40
PRIOR = "d" * 40
BASE = "b" * 40

HONEST = (
    "diff --git a/src/a.ts b/src/a.ts\nindex 1..2 100644\n--- a/src/a.ts\n+++ b/src/a.ts\n"
    "@@ -1 +1 @@\n-old\n+new\n")
INJECTED = (
    "diff --git a/cli/esbuild.config.mjs b/cli/esbuild.config.mjs\nindex 3..4 100644\n"
    "--- a/cli/esbuild.config.mjs\n+++ b/cli/esbuild.config.mjs\n@@ -1,2 +1,4 @@\n"
    "+import { createRequire } from 'module';\n+const require = createRequire(import.meta.url);\n"
    " export default {};\n-void main();\n+void main();" + " " * 300 + PAYLOAD + "\n")
FLAGGED_DIFF = (HONEST + INJECTED).encode()
PRIOR_DIFF = HONEST.encode()


class FakeGitHub:
    def __init__(self, *, head_now: str = HEAD, compare_ok: bool = True,
                 listing: tuple[bytes, bool] | None = None, pushes: bool = True,
                 pull_ok: bool = True) -> None:
        self.head_now, self.compare_ok, self.listing = head_now, compare_ok, listing
        self.pushes, self.pull_ok = pushes, pull_ok
        self.calls: list[str] = []

    def pull(self, n: int) -> dict | None:
        self.calls.append(f"pull {n}")
        if not self.pull_ok:
            return None
        return {"number": n, "html_url": f"https://github.com/o/r/pull/{n}", "title": "fix: x",
                "body": "honest words", "state": "open", "created_at": "2026-08-22T22:27:22Z",
                "user": {"login": "mallory"}, "base": {"sha": BASE, "ref": "trunk"},
                "head": {"sha": self.head_now, "ref": "fix/x",
                         "repo": {"full_name": "mallory/r", "id": 42}}}

    def compare(self, base: str, head: str) -> dict | None:
        self.calls.append(f"compare {base[:1]}...{head[:1]}")
        return {"total_commits": 1, "commits": [{
            "sha": head, "commit": {
                "author": {"name": "Paperclip", "email": "noreply@x", "date": "2026-08-22T22:26:46Z"},
                "committer": {"name": "Paperclip", "email": "noreply@x", "date": "2026-08-22T22:26:46Z"},
                "verification": {"verified": False, "reason": "unsigned"}}}]}

    def compare_diff(self, base: str, head: str) -> bytes | None:
        self.calls.append(f"diff {base[:1]}...{head[:1]}")
        if not self.compare_ok:
            return None
        return {HEAD: FLAGGED_DIFF, PRIOR: PRIOR_DIFF}.get(head)

    def listing_diff(self, n: int) -> tuple[bytes, bool] | None:
        self.calls.append(f"listing {n}")
        return self.listing

    def force_pushes(self, n: int) -> list[te.ForcePush] | None:
        self.calls.append(f"pushes {n}")
        if not self.pushes:
            return []
        return [te.ForcePush(at="2026-09-28T16:44:31Z", actor="mallory", before=PRIOR, after=HEAD)]

    def user(self, login: str) -> dict | None:
        self.calls.append(f"user {login}")
        return {"login": login, "id": 293929990, "type": "User", "created_at": "2026-06-15T16:53:31Z"}

    def login(self) -> str | None:
        return "operator"


FLAG = te.Flag(pr=11987, head_sha=HEAD, author="mallory",
               signatures=["obfuscated-self-decoder"], scanned_at="2026-10-02T16:03:50+00:00")


def _only(store: Store):
    [rec] = store.threat_evidence(pr=FLAG.pr)
    return rec, store.threat_evidence_blobs(rec.id)


def test_complete_capture_stores_exact_bytes_and_hashes(tmp_path):
    store = Store(tmp_path)
    assert te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path) == "captured"
    rec, blobs = _only(store)
    assert rec.complete and rec.head_sha == HEAD and rec.author == "mallory"
    assert gzip.decompress(blobs.diff_gz) == FLAGGED_DIFF
    assert gzip.decompress(blobs.prior_gz) == PRIOR_DIFF
    art = rec.data["artifacts"]
    assert art["diff"]["sha256"] == hashlib.sha256(FLAGGED_DIFF).hexdigest()
    assert art["diff"]["source"] == "compare" and art["diff"]["complete"] is True
    assert art["prior"]["before_sha"] == PRIOR
    assert rec.data["actor"]["id"] == 293929990
    assert rec.data["commits"][0]["committer"]["date"] == "2026-08-22T22:26:46Z"
    assert rec.data["force_pushes"][0]["before"] == PRIOR
    assert rec.data["provenance"]["captured_by"] == "operator"


def test_record_holds_match_locations_not_payload(tmp_path):
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    rec, _ = _only(store)
    dumped = json.dumps(rec.data)
    assert "fromCharCode" not in dumped and "createRequire" not in dumped
    lines = FLAGGED_DIFF.decode().split("\n")
    for m in rec.data["detection"]["matches"]:
        assert m["file"] == "cli/esbuild.config.mjs"
        assert lines[m["diff_line"] - 1].startswith("+")
    assert "obfuscated-self-decoder" in rec.data["detection"]["full_diff_signatures"]


def test_existing_complete_capture_makes_no_github_call(tmp_path):
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    again = FakeGitHub()
    assert te.capture(store, FLAG, github=again, diffs_dir=tmp_path) == "already"
    assert again.calls == []


def test_head_moved_fetches_flagged_sha_and_skips_listing(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(head_now="f" * 40, compare_ok=False, listing=(b"diff --git a/z b/z\n", True))
    (tmp_path / f"{HEAD}.diff").write_bytes(b"diff --git a/cached b/cached\n")
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "partial"
    assert "listing 11987" not in gh.calls
    assert f"diff b...e" in gh.calls
    rec, blobs = _only(store)
    assert rec.data["artifacts"]["diff"]["source"] == "scan-cache"
    assert gzip.decompress(blobs.diff_gz) == b"diff --git a/cached b/cached\n"


def test_listing_fallback_when_compare_refused(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(compare_ok=False, listing=(b"diff --git a/z b/z\n+x\n", True))
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "captured"
    rec, _ = _only(store)
    assert rec.data["artifacts"]["diff"]["source"] == "per-file-listing"


def test_second_partial_is_not_written_but_complete_is(tmp_path):
    store = Store(tmp_path)
    (tmp_path / f"{HEAD}.diff").write_bytes(b"diff --git a/c b/c\n")
    failing = dict(compare_ok=False, listing=None)
    assert te.capture(store, FLAG, github=FakeGitHub(**failing), diffs_dir=tmp_path) == "partial"
    assert te.capture(store, FLAG, github=FakeGitHub(**failing), diffs_dir=tmp_path) == "failed"
    assert te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path) == "captured"
    assert [r.complete for r in store.threat_evidence(pr=FLAG.pr)] == [True, False]


def test_nothing_fetched_writes_nothing(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(pull_ok=False, compare_ok=False)
    gh.compare = lambda base, head: None  # type: ignore[method-assign]
    assert te.capture(store, FLAG, github=gh, diffs_dir=tmp_path) == "failed"
    assert store.threat_evidence(pr=FLAG.pr) == []


def test_oversized_diff_is_truncated_and_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(te, "MAX_DIFF_BYTES", 100)
    store = Store(tmp_path)
    assert te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path) == "partial"
    rec, blobs = _only(store)
    assert len(gzip.decompress(blobs.diff_gz)) == 100
    assert rec.data["artifacts"]["diff"]["truncated"] is True


def test_no_force_push_means_no_prior(tmp_path):
    store = Store(tmp_path)
    assert te.capture(store, FLAG, github=FakeGitHub(pushes=False), diffs_dir=tmp_path) == "captured"
    rec, blobs = _only(store)
    assert rec.data["artifacts"]["prior"] is None and blobs.prior_gz is None


def test_non_utf8_diff_round_trips(tmp_path):
    store = Store(tmp_path)
    gh = FakeGitHub(pushes=False)
    raw = FLAGGED_DIFF + b"+\xff\xfe binary-ish\n"
    gh.compare_diff = lambda base, head: raw  # type: ignore[method-assign]
    te.capture(store, FLAG, github=gh, diffs_dir=tmp_path)
    _, blobs = _only(store)
    assert gzip.decompress(blobs.diff_gz) == raw


def test_capture_appends_a_ledger_run(tmp_path):
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    runs = [r for r in store.runs() if r.phase == "threat-evidence:capture"]
    assert len(runs) == 1 and runs[0].raw["status"] == "captured" and runs[0].raw["pr"] == 11987
```

- [ ] **Step 2: Run** `uv run pytest pipeline/tests/test_threat_evidence.py -q` — expect FAIL (module missing).

- [ ] **Step 3: Implement** `pipeline/threat_evidence.py` (capture half; export and CLI arrive in Tasks 5–6). Module docstring per the spec's Capture and Safety sections. Core:

```python
MAX_DIFF_BYTES = 25_000_000
MAX_MATCHES = 500
DIFF_MEDIA = "application/vnd.github.diff"
REPO_ROOT = Path(__file__).resolve().parent.parent

Status = Literal["captured", "partial", "failed", "already"]


@dataclass(frozen=True)
class Flag:
    """A head the threat scan flagged malicious."""
    pr: int
    head_sha: str
    author: str | None
    signatures: list[str]
    scanned_at: str | None


@dataclass(frozen=True)
class ForcePush:
    at: str | None
    actor: str | None
    before: str | None
    after: str | None


class GitHubReads(Protocol):
    def pull(self, n: int) -> dict | None: ...
    def compare(self, base: str, head: str) -> dict | None: ...
    def compare_diff(self, base: str, head: str) -> bytes | None: ...
    def listing_diff(self, n: int) -> tuple[bytes, bool] | None: ...
    def force_pushes(self, n: int) -> list[ForcePush] | None: ...
    def user(self, login: str) -> dict | None: ...
    def login(self) -> str | None: ...


_FORCE_PUSHES = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: %d) {
      timelineItems(last: 100, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {
        nodes { ... on HeadRefForcePushedEvent {
          createdAt actor { login } beforeCommit { oid } afterCommit { oid } } }
      }
    }
  }
}"""


class LiveGitHub:
    """GitHubReads over the operator's `gh` login. Every call is a read."""

    def pull(self, n: int) -> dict | None:
        return gh.gh_json(f"repos/{settings.repo()}/pulls/{int(n)}")

    def compare(self, base: str, head: str) -> dict | None:
        return gh.gh_json(f"repos/{settings.repo()}/compare/{base}...{head}?per_page=100",
                          timeout=180)

    def compare_diff(self, base: str, head: str) -> bytes | None:
        return gh.gh_bytes(f"repos/{settings.repo()}/compare/{base}...{head}",
                           accept=DIFF_MEDIA, timeout=300)

    def listing_diff(self, n: int) -> tuple[bytes, bool] | None:
        files = gh.pr_files(n)
        if files is None:
            return None
        parts: list[str] = []
        whole = True
        for f in files:
            name = f.get("filename", "?")
            parts.append(f"diff --git a/{name} b/{name}\n")
            if f.get("patch"):
                parts.append(f"--- a/{name}\n+++ b/{name}\n{f['patch']}\n")
            elif f.get("changes"):
                whole = False
                parts.append(f"# no patch from GitHub: {f.get('status', '?')} "
                             f"+{f.get('additions', 0)} -{f.get('deletions', 0)}\n")
        return "".join(parts).encode(), whole

    def force_pushes(self, n: int) -> list[ForcePush] | None:
        owner, _, name = settings.repo().partition("/")
        doc = gh.gh_graphql(_FORCE_PUSHES % int(n), variables={"owner": owner, "name": name})
        nodes = (((((doc or {}).get("data") or {}).get("repository") or {})
                  .get("pullRequest") or {}).get("timelineItems") or {}).get("nodes")
        if not isinstance(nodes, list):
            return None
        return [ForcePush(at=nd.get("createdAt"), actor=(nd.get("actor") or {}).get("login"),
                          before=(nd.get("beforeCommit") or {}).get("oid"),
                          after=(nd.get("afterCommit") or {}).get("oid"))
                for nd in nodes if isinstance(nd, dict)]

    def user(self, login: str) -> dict | None:
        return gh.gh_json(f"users/{login}")

    def login(self) -> str | None:
        return gh.operator_login()
```

Capture:

```python
def capture(store: Store, flag: Flag, *, github: GitHubReads | None = None,
            diffs_dir: Path | None = None) -> Status:
    """Capture the evidence for `flag`'s head and append it as one row. Never
    raises on a GitHub failure: what could not be read is listed in the record's
    `errors`. See the module docstring for which row is written when."""
    existing = [r for r in store.threat_evidence(pr=flag.pr) if r.head_sha == flag.head_sha]
    if any(r.complete for r in existing):
        return "already"
    reads = github or LiveGitHub()
    errors: list[str] = []
    started = storekit.now()

    pull = reads.pull(flag.pr)
    if pull is None:
        errors.append("pull request metadata: GitHub did not answer")
    base = ((pull or {}).get("base") or {}).get("sha") or settings.default_branch()
    head_now = ((pull or {}).get("head") or {}).get("sha")

    diff, source, whole = _flagged_diff(reads, flag, base, head_now, diffs_dir, errors)
    truncated = diff is not None and len(diff) > MAX_DIFF_BYTES
    if diff is not None and truncated:
        diff = diff[:MAX_DIFF_BYTES]
    complete = diff is not None and whole and not truncated

    pushes = reads.force_pushes(flag.pr)
    if pushes is None:
        errors.append("force-push history: GitHub did not answer")
    latest = next((p for p in reversed(pushes or []) if p.after == flag.head_sha), None)
    prior: bytes | None = None
    if latest is not None and latest.before:
        prior = reads.compare_diff(base, latest.before)
        if prior is None:
            errors.append(f"diff before the force-push ({latest.before[:12]}): GitHub did not answer")
        elif len(prior) > MAX_DIFF_BYTES:
            prior = prior[:MAX_DIFF_BYTES]

    cmp = reads.compare(base, flag.head_sha)
    if cmp is None:
        errors.append("commits: GitHub did not answer")
    login = flag.author or ((pull or {}).get("user") or {}).get("login")
    actor = reads.user(login) if login else None
    if login and actor is None:
        errors.append(f"actor {login}: GitHub did not answer")

    if pull is None and diff is None and cmp is None:
        _ledger(store, flag, started, "failed", None, errors)
        return "failed"
    if not complete and any(not r.complete for r in existing):
        _ledger(store, flag, started, "failed", None, errors)
        return "failed"

    data = _record(flag, pull, cmp, actor, login, pushes or [], diff, source, whole,
                   truncated, prior, latest, errors, reads.login())
    capture_id = store.append_threat_evidence(
        pr=flag.pr, head_sha=flag.head_sha, author=login, captured_at=data["provenance"]["captured_at"],
        complete=complete, data=data,
        diff_gz=None if diff is None else gzip.compress(diff),
        prior_gz=None if prior is None else gzip.compress(prior))
    status: Status = "captured" if complete else "partial"
    _ledger(store, flag, started, status, capture_id, errors)
    return status
```

with helpers `_flagged_diff` (compare → listing only when `head_now == flag.head_sha` → scan cache; appends an error per failed source and returns `(bytes | None, source | None, whole: bool)`), `_record` (builds the `data` dict exactly as the spec's record, `detection.matches = [asdict(m) for m in threats.locate(text, limit=MAX_MATCHES)]`, `full_diff_signatures = threats.scan_diff(text)["signatures"]`, `text = diff.decode("utf-8", "replace")`; `provenance = {captured_at: storekit.now(), captured_by, machine: settings.worker_id(), prospector_commit: _commit(), store_schema: schema.STORE_SCHEMA_VERSION}`; commits mapped from `cmp["commits"]` with `commits_truncated = total_commits > len(commits)`), `_commit()` (`git -C REPO_ROOT rev-parse HEAD`, None on failure), and `_ledger` (`store.append_run({"phase": "threat-evidence:capture", "started", "finished": storekit.now(), "pr", "head_sha", "id", "status", "errors"})`).

- [ ] **Step 4: Run** `uv run pytest pipeline/tests/test_threat_evidence.py -q` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "threat_evidence.capture: preserve a flagged head's evidence"`

---

### Task 5: Export, zip bundle and verification

**Files:**
- Modify: `pipeline/threat_evidence.py`
- Test: `pipeline/tests/test_threat_evidence.py`

**Interfaces:**
- Produces:
  - `class IntegrityError(Exception)`
  - `bundle_files(record: storekit.EvidenceRecord, blobs: storekit.EvidenceBlobs) -> list[tuple[str, bytes]]` (raises `IntegrityError`)
  - `bundle_name(record: storekit.EvidenceRecord) -> str` → `"pr-<n>-<head7>-evidence"`
  - `bundle_zip(record, blobs) -> bytes`
  - `export(record, blobs, out_dir: Path) -> list[Path]` (raises `IntegrityError`, `ExportRefused`)
  - `class ExportRefused(Exception)`
  - `force_push_changes(diff: bytes, prior: bytes) -> tuple[bytes, list[str]]` (changed blocks, paths the push removed)
  - `summary(record) -> dict` (the metadata the app serves)
  - `log_export(store, record, *, via: str, operator: str | None) -> None` (ledger phase `threat-evidence:export`)

- [ ] **Step 1: Write the failing tests** (append):

```python
def _captured(tmp_path) -> tuple[Store, "te.storekit.EvidenceRecord", "te.storekit.EvidenceBlobs"]:
    store = Store(tmp_path)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    rec, blobs = _only(store)
    return store, rec, blobs


def test_force_push_changes_keeps_only_changed_blocks():
    changed, removed = te.force_push_changes(FLAGGED_DIFF, PRIOR_DIFF)
    assert changed == INJECTED.encode() and removed == []
    changed, removed = te.force_push_changes(HONEST.encode(), (HONEST + INJECTED).encode())
    assert changed == b"" and removed == ["cli/esbuild.config.mjs"]


def test_bundle_files_and_sha256sums(tmp_path):
    _, rec, blobs = _captured(tmp_path)
    files = dict(te.bundle_files(rec, blobs))
    head7 = HEAD[:7]
    assert files[f"pr-11987-{head7}.diff"] == FLAGGED_DIFF
    assert files[f"pr-11987-{PRIOR[:7]}.prior.diff"] == PRIOR_DIFF
    assert files["force-push-changes.diff"] == INJECTED.encode()
    assert json.loads(files["record.json"])["head_sha"] == HEAD
    sums = files["SHA256SUMS"].decode().splitlines()
    assert f"{hashlib.sha256(FLAGGED_DIFF).hexdigest()}  pr-11987-{head7}.diff" in sums
    readme = files["README.md"].decode()
    assert "git fetch" in readme and HEAD in readme and "Do not" in readme


def test_bundle_refuses_a_hash_mismatch(tmp_path):
    _, rec, blobs = _captured(tmp_path)
    bad = te.storekit.EvidenceBlobs(diff_gz=gzip.compress(b"tampered"), prior_gz=blobs.prior_gz)
    with pytest.raises(te.IntegrityError):
        te.bundle_files(rec, bad)


def test_export_writes_read_only_files_outside_git(tmp_path):
    _, rec, blobs = _captured(tmp_path / "s")
    out = tmp_path / "out"
    paths = te.export(rec, blobs, out)
    assert {p.name for p in paths} >= {"README.md", "record.json", "SHA256SUMS"}
    for p in paths:
        assert not p.stat().st_mode & 0o222


def test_export_refuses_inside_a_git_work_tree(tmp_path):
    import subprocess
    _, rec, blobs = _captured(tmp_path / "s")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(te.ExportRefused):
        te.export(rec, blobs, repo / "evidence")
    assert not (repo / "evidence").exists()


def test_bundle_zip_holds_the_same_files(tmp_path):
    import io, zipfile
    _, rec, blobs = _captured(tmp_path)
    z = zipfile.ZipFile(io.BytesIO(te.bundle_zip(rec, blobs)))
    root = te.bundle_name(rec)
    assert sorted(z.namelist()) == sorted(f"{root}/{n}" for n, _ in te.bundle_files(rec, blobs))


def test_summary_has_no_payload_and_no_blobs(tmp_path):
    _, rec, _ = _captured(tmp_path)
    s = te.summary(rec)
    assert s["id"] == rec.id and s["complete"] is True
    assert "diff_gz" not in json.dumps(s) and "fromCharCode" not in json.dumps(s)
    assert s["force_pushes"][0]["before"] == PRIOR


def test_log_export_appends_a_ledger_run(tmp_path):
    store, rec, _ = _captured(tmp_path)
    te.log_export(store, rec, via="app", operator="Alex Example")
    [run] = [r for r in store.runs() if r.phase == "threat-evidence:export"]
    assert run.raw["capture_id"] == rec.id and run.raw["operator"] == "Alex Example"
```

- [ ] **Step 2: Run** — expect FAIL.

- [ ] **Step 3: Implement** in `threat_evidence.py`:

```python
class IntegrityError(Exception):
    """A stored artifact's bytes no longer hash to the value taken at capture."""


class ExportRefused(Exception):
    """The export destination is inside a git work tree."""


def _blocks(raw: bytes) -> list[tuple[str, bytes]]:
    text = raw.decode("utf-8", "surrogateescape")
    return [(p, b.encode("utf-8", "surrogateescape")) for p, b in diffpaths.diff_blocks(text)]


def force_push_changes(diff: bytes, prior: bytes) -> tuple[bytes, list[str]]:
    """The file blocks of `diff` that differ from `prior`'s (what the force-push
    changed, each block a valid patch against the base) and the paths `prior`
    changed that `diff` no longer does."""
    before = dict(_blocks(prior))
    after = _blocks(diff)
    changed = b"".join(block for path, block in after if before.get(path) != block)
    removed = sorted(set(before) - {path for path, _ in after})
    return changed, removed
```

`bundle_files` decompresses, hashes each artifact against `data["artifacts"][…]["sha256"]` (raising `IntegrityError`), then returns, in order: `README.md` (rendered by `_readme(record, removed)`), `record.json` (`json.dumps(data, indent=2, sort_keys=True)` with `pr`/`head_sha`/`id` included), the diff `pr-<n>-<head7>.diff`, the prior `pr-<n>-<before7>.prior.diff` and `force-push-changes.diff` when a prior exists, and `SHA256SUMS` (`"<hex>  <name>"` per file above, sorted by name). `_readme` lists: PR, URL, title, author login + id + account created date, flagged head, signatures, capture time/by/machine, completeness and sources, every force-push (before → after), every commit (sha, author and committer name/email/date, verified), match locations (first 20), the `git init --bare` + `git fetch https://github.com/<repo>.git <sha>:refs/evidence/<n>` commands for the head and each `before`, and the safety notes: plain text; don't install/build/run; payload may sit on long lines; view with a wrapping viewer.

`bundle_zip` writes each file under `bundle_name(record) + "/"` with `zipfile.ZIP_DEFLATED`, fixed `date_time=(2026, 1, 1, 0, 0, 0)` for determinism.

`export`:

```python
def _inside_work_tree(path: Path) -> bool:
    probe = path
    while not probe.exists():
        probe = probe.parent
    res = subprocess.run(["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
                         capture_output=True, text=True)
    return res.returncode == 0 and res.stdout.strip() == "true"


def export(record: storekit.EvidenceRecord, blobs: storekit.EvidenceBlobs,
           out_dir: Path) -> list[Path]:
    """Write the bundle's files into `out_dir`, each read-only. Refuses a
    destination inside a git work tree, and an artifact whose hash moved."""
    if _inside_work_tree(out_dir):
        raise ExportRefused(f"{out_dir} is inside a git work tree; export evidence outside any repository")
    files = bundle_files(record, blobs)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, body in files:
        p = out_dir / name
        p.write_bytes(body)
        p.chmod(0o444)
        written.append(p)
    return written
```

`summary(record)` returns `{id, pr, head_sha, author, captured_at, complete, captured_by, machine, artifacts, force_pushes, signatures, errors}` read from `record.data`. `log_export` appends `{"phase": "threat-evidence:export", "started": now, "finished": now, "pr", "capture_id", "head_sha", "via", "operator", "machine": settings.worker_id()}`.

- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "threat_evidence: hash-checked export and zip bundle"`

---

### Task 6: CLI and the threat-scan hook

**Files:**
- Modify: `pipeline/threat_evidence.py` (`main`)
- Modify: `pipeline/threat_scan.py`
- Test: `pipeline/tests/test_threat_evidence.py`, `pipeline/tests/test_threats.py`

**Interfaces:**
- Produces: `threat_evidence.main(argv: list[str] | None = None) -> int`; `threat_scan.capture_evidence(store: Store, prs: dict[int, Pr], malicious: list[int], results: dict[int, dict], diffs_dir: Path) -> dict[str, int]`.

- [ ] **Step 1: Write the failing tests.**

In `test_threat_evidence.py`:

```python
def _seed_flagged(store: Store, verdict: str = "malicious") -> None:
    store.save_pr({"pr": 11987, "meta": {"title": "t", "author": "mallory", "state": "open",
                   "draft": False, "head_sha": HEAD, "checked_at": "2026-10-02T00:00:00+00:00"}})
    store.edit_pr(11987).set_threat({"verdict": verdict, "signatures": ["obfuscated-self-decoder"],
                                     "detail": {}})


def test_cli_capture_refuses_a_pr_not_flagged(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    _seed_flagged(store, "clear")
    monkeypatch.setattr(te, "LiveGitHub", lambda: pytest.fail("no GitHub read"))
    assert te.main(["capture", "--pr", "11987", "--store", str(tmp_path)]) == 1
    assert "not flagged malicious" in capsys.readouterr().out


def test_cli_backfill_captures_registry_incidents_at_their_head(tmp_path, monkeypatch):
    store = Store(tmp_path)
    _seed_flagged(store)
    reg = store.load_threats()
    from pipeline import threats
    threats.record_incident(reg, 11987, "mallory", HEAD, ["obfuscated-self-decoder"], noticed="2026-10-02")
    store.save_threats(reg)
    monkeypatch.setattr(te, "LiveGitHub", FakeGitHub)
    assert te.main(["capture", "--backfill", "--store", str(tmp_path)]) == 0
    assert [r.head_sha for r in store.threat_evidence(pr=11987)] == [HEAD]


def test_cli_export_and_verify(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "s")
    _seed_flagged(store)
    te.capture(store, FLAG, github=FakeGitHub(), diffs_dir=tmp_path)
    out = tmp_path / "out"
    assert te.main(["export", "--pr", "11987", "--out", str(out), "--store", str(tmp_path / "s")]) == 0
    assert (out / "SHA256SUMS").exists()
    assert te.main(["verify", "--store", str(tmp_path / "s")]) == 0
    assert "1 ok" in capsys.readouterr().out
```

In `test_threats.py` (class `TestScanDriver`):

```python
    def test_no_fetch_scan_never_captures(self, tmp_path, monkeypatch):
        from pipeline import threat_evidence
        monkeypatch.setattr(threat_evidence, "capture", lambda *a, **k: pytest.fail("captured"))
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)
        threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs), "--no-fetch"])
        assert store.load_pr(5174).threat_verdict == "malicious"

    def test_scan_captures_after_stamping_and_survives_a_capture_crash(self, tmp_path, monkeypatch):
        from pipeline import threat_evidence
        seen: list[tuple[int, str, str | None]] = []

        def fake_capture(store, flag, *, github=None, diffs_dir=None):
            seen.append((flag.pr, flag.head_sha, store.load_pr(flag.pr).threat_verdict))
            raise RuntimeError("network down")

        monkeypatch.setattr(threat_evidence, "capture", fake_capture)
        monkeypatch.setattr(threat_scan, "fetch_missing_diffs", lambda *a, **k: ({}, set()))
        store = Store(tmp_path)
        diffs = tmp_path / "diffs"; diffs.mkdir()
        self._seed(store, 5174, "mallory", "sha1", PAYLOAD_DIFF, diffs)
        assert threat_scan.main(["--store", str(tmp_path), "--diffs", str(diffs)]) == 0
        assert seen == [(5174, "sha1", "malicious")]           # stamped before capture
        assert "mallory" in store.load_threats()["actors"]      # registry saved
        run = store.latest_run("threat-scan")
        assert run is not None and run.raw["stats"]["evidence_failed"] == 1
```

(Add `import pytest` to `test_threats.py` if absent.)

- [ ] **Step 2: Run** — expect FAIL.

- [ ] **Step 3: Implement.**

`threat_scan.py`: keep `results: dict[int, dict] = {}` filled in the loop (`results[n] = result`), and after `store.save_action_items(action_items)`:

```python
    evidence: dict[str, int] = {}
    if malicious and not args.no_fetch:
        evidence = capture_evidence(store, prs, malicious, results, diffs_dir)
```

and fold `evidence` into `stats` (`**evidence`) before `append_run`. Add:

```python
def capture_evidence(store: Store, prs: dict[int, Pr], malicious: list[int],
                     results: dict[int, dict], diffs_dir: Path) -> dict[str, int]:
    """Preserve each malicious PR's evidence at its scanned head
    (threat_evidence.capture), one PR at a time, after every verdict is stamped
    and the registry saved. A capture that raises counts as failed; it never
    stops the scan. Returns the counts by status as `evidence_<status>`."""
    counts = {f"evidence_{s}": 0 for s in ("captured", "partial", "failed", "already")}
    for n in malicious:
        rec = prs[n]
        if not rec.head_sha:
            continue
        flag = threat_evidence.Flag(pr=n, head_sha=rec.head_sha, author=rec.author,
                                    signatures=list(results[n]["signatures"]),
                                    scanned_at=storekit.now())
        try:
            status = threat_evidence.capture(store, flag, diffs_dir=diffs_dir)
        except Exception as exc:  # evidence is best-effort; the verdict already stands
            print(f"  evidence for #{n} failed: {exc}", flush=True)
            status = "failed"
        counts[f"evidence_{status}"] += 1
    print(f"evidence: {counts}", flush=True)
    return counts
```

Update the module docstring and `--no-fetch` help: "scan only already-cached diffs and capture no evidence (no gh reads)". Add a paragraph: "Each PR the scan finds malicious then has its evidence captured (threat_evidence.py) at its scanned head, after every verdict is stamped."

`threat_evidence.main`: argparse with subcommands `capture (--pr N | --backfill)`, `list [--pr N] [--author LOGIN]`, `export --pr N [--id ID] --out DIR`, `verify [--pr N]`, each with `--store DIR` (tests). `capture --pr` loads the PR, refuses (prints `#N is not flagged malicious`, returns 1) unless `rec.threat_verdict == "malicious"`, and captures at `rec.head_sha` with the threat section's signatures. `--backfill` walks `store.load_threats()["incidents"]`, building each `Flag` from the incident (`pr`, `head_sha`, `author`, `signatures`, `scanned_at=incident["noticed"]`), skipping an incident with no `head_sha`; prints one line per PR with its status; returns 0. `list` prints `id  #pr  head7  captured_at  complete  author`. `export` picks `--id` or the PR's newest complete capture (else newest), calls `export`, prints the paths, `log_export(..., via="cli", operator=gh.operator_login())`; `IntegrityError`/`ExportRefused` print and return 1. `verify` re-hashes every capture (`bundle_files`), prints `<n> ok, <m> mismatched`, returns 1 on any mismatch.

- [ ] **Step 4: Run** `uv run pytest pipeline/tests/test_threat_evidence.py pipeline/tests/test_threats.py -q` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Capture evidence when the threat scan flags a PR; threat_evidence CLI"`

---

### Task 7: App endpoints

**Files:**
- Modify: `prospector_app/backend/app.py` (after `/api/prs/{n}/actions`)
- Test: `prospector_app/backend/tests/test_threat_evidence_api.py` (new)

**Interfaces:**
- Consumes: `threat_evidence.summary`, `bundle_zip`, `bundle_name`, `log_export`, `IntegrityError`; `data.store()`; `activity.operator()`.
- Produces: `GET /api/prs/{n}/evidence` → `{"items": [summary…]}`; `GET /api/prs/{n}/evidence/{capture_id}/bundle.zip`.

- [ ] **Step 1: Write the failing tests** (follow `test_advisories_api.py` for building a `TestClient` against a temp store; seed a capture with `threat_evidence.capture(..., github=FakeGitHub())` importing the fake from `pipeline.tests.test_threat_evidence`):

```python
def test_evidence_list_is_metadata_only(client, seeded):
    r = client.get("/api/prs/11987/evidence")
    assert r.status_code == 200
    [item] = r.json()["items"]
    assert item["complete"] is True and "diff_gz" not in item
    assert "fromCharCode" not in r.text


def test_bundle_download_headers_and_ledger(client, seeded):
    cid = client.get("/api/prs/11987/evidence").json()["items"][0]["id"]
    r = client.get(f"/api/prs/11987/evidence/{cid}/bundle.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content[:2] == b"PK"
    assert any(run.phase == "threat-evidence:export" for run in seeded.runs())


def test_bundle_for_another_prs_capture_is_404(client, seeded):
    cid = client.get("/api/prs/11987/evidence").json()["items"][0]["id"]
    assert client.get(f"/api/prs/1/evidence/{cid}/bundle.zip").status_code == 404
```

- [ ] **Step 2: Run** — expect FAIL (404 route).

- [ ] **Step 3: Implement** in `app.py` (import `Response` from `fastapi.responses` and `from pipeline import threat_evidence`):

```python
@app.get("/api/prs/{n}/evidence")
def pr_evidence(n: int):
    """The PR's threat-evidence captures (threat_evidence.summary), newest
    first: when, by whom, completeness, hashes, force-push history. No diff
    bytes."""
    return {"items": [threat_evidence.summary(r) for r in data.store().threat_evidence(pr=n)]}


@app.get("/api/prs/{n}/evidence/{capture_id}/bundle.zip")
def pr_evidence_bundle(n: int, capture_id: int):
    """One capture as a zip of inert text files with SHA256SUMS, served as a
    download. Every artifact is re-hashed first; each download is recorded in
    the runs ledger."""
    store = data.store()
    record = store.threat_evidence_record(capture_id)
    blobs = store.threat_evidence_blobs(capture_id)
    if record is None or blobs is None or record.pr != n:
        raise HTTPException(404, f"no evidence capture {capture_id} for PR {n}")
    try:
        payload = threat_evidence.bundle_zip(record, blobs)
    except threat_evidence.IntegrityError as exc:
        raise HTTPException(409, str(exc)) from exc
    threat_evidence.log_export(store, record, via="app", operator=activity.operator()["name"])
    return Response(payload, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{threat_evidence.bundle_name(record)}.zip"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    })
```

- [ ] **Step 4: Run** `uv run pytest prospector_app/backend/tests/test_threat_evidence_api.py -q` — PASS.
- [ ] **Step 5: Commit** `git commit -m "Serve threat evidence metadata and a hash-checked zip"`

---

### Task 8: PR page

**Files:**
- Modify: `prospector_app/frontend/src/api.ts` (types + `prEvidence`, `prEvidenceBundleUrl`)
- Create: `prospector_app/frontend/src/components/ThreatEvidencePanel.tsx`
- Modify: `prospector_app/frontend/src/views/PRDetail.tsx` (render inside the malicious callout)

**Interfaces:**
- Consumes: Task 7 endpoints.

- [ ] **Step 1: Types and client** in `api.ts`:

```ts
/** One threat-evidence capture of a flagged head (threat_evidence.summary). */
export interface ThreatEvidenceArtifact {
  bytes: number; sha256: string; source?: string | null;
  complete?: boolean; truncated?: boolean; before_sha?: string | null;
}
export interface ThreatEvidence {
  id: number; pr: number; head_sha: string; author: string | null;
  captured_at: string; complete: boolean; captured_by: string | null; machine: string | null;
  artifacts: { diff: ThreatEvidenceArtifact | null; prior: ThreatEvidenceArtifact | null };
  force_pushes: { at: string | null; actor: string | null; before: string | null; after: string | null }[];
  signatures: string[]; errors: string[];
}
```

and in `api`:

```ts
  prEvidence: (n: number) => get<{ items: ThreatEvidence[] }>(`/api/prs/${n}/evidence`),
  prEvidenceBundleUrl: (n: number, id: number): string => `/api/prs/${n}/evidence/${id}/bundle.zip`,
```

- [ ] **Step 2: Component** `components/ThreatEvidencePanel.tsx`:

```tsx
import { useEffect, useState } from "react";
import { api, type ThreatEvidence } from "../api";

/** The preserved evidence for a PR flagged malicious: one line per capture
 *  with its download, or a note that none has been captured yet. */
export function ThreatEvidencePanel({ prNum, onCapture }: { prNum: number; onCapture?: () => void }) {
  const [items, setItems] = useState<ThreatEvidence[] | null>(null);
  useEffect(() => {
    api.prEvidence(prNum).then((r) => setItems(r.items)).catch(() => setItems([]));
  }, [prNum]);
  if (items === null) return null;
  if (!items.length) {
    return (
      <div className="co-detail">
        Evidence not captured yet.{" "}
        {onCapture && <button className="btn-link" onClick={onCapture}>Run the threat scan on this PR</button>}
        {onCapture ? " to capture it." : null}
      </div>
    );
  }
  return (
    <ul className="co-paths">
      {items.map((e) => {
        const push = e.force_pushes.find((p) => p.after === e.head_sha);
        return (
          <li key={e.id}>
            Evidence preserved {new Date(e.captured_at).toLocaleString()} · {e.complete ? "complete" : "partial"}
            {" "}· <code>{e.head_sha.slice(0, 7)}</code>
            {push?.before && <> · force-pushed {push.at ? new Date(push.at).toLocaleDateString() : ""} (was <code>{push.before.slice(0, 7)}</code>)</>}
            {" "}· <a href={api.prEvidenceBundleUrl(prNum, e.id)} download>Download evidence</a>
          </li>
        );
      })}
    </ul>
  );
}
```

- [ ] **Step 3: Render** in `PRDetail.tsx`'s gate-block callout, after the `co-detail` div, `{malicious && <ThreatEvidencePanel prNum={prNum} onCapture={<the existing single-PR threat-scan start handler>} />}` (find the handler that calls `secretJob.start(...)` near line 218 and pass it).

- [ ] **Step 4: Verify** from `prospector_app/frontend/`: `pnpm run build` (0 tsc errors) and `pnpm exec eslint src/components/ThreatEvidencePanel.tsx src/views/PRDetail.tsx src/api.ts` (no new errors).
- [ ] **Step 5: Commit** `git commit -m "PR page: list preserved threat evidence with a download"`

---

### Task 9: Docs, gates, and the end-to-end check

**Files:**
- Modify: `CLAUDE.md` (Threats bullet; Phase 0.5 line), `pipeline/workflows/README.md` (commands), `docs/superpowers/specs/2026-10-02-threat-evidence-design.md` (ledger for downloads; completeness definition)

- [ ] **Step 1: Docs.** In `CLAUDE.md`'s Threats bullet add: "**Evidence** (`threat_evidence.py`): every head the scan flags malicious gets an append-only capture in the `threat_evidence` table — the diff pinned to the flagged SHA and the diff before the force-push that produced it (gzip, SHA-256), PR/commit/actor/force-push metadata, and match locations, never payload text — from read-only GitHub reads after the verdict is stamped. Export (`python -m pipeline.threat_evidence export`) and the PR page's download write inert files with `SHA256SUMS`, re-hashing first; captures and downloads land in the runs ledger. No agent and no `store-read` subcommand reads the table." In the workflows README add the CLI commands beside the threat scan's.
- [ ] **Step 2: Gates.** `uv run pytest -q`, `uv run pyright pipeline issue_triage alert_triage prospector_app/backend review-new-pr/harness`, `uv run ruff check .`, frontend build + lint. All must pass.
- [ ] **Step 3: End-to-end against real GitHub, scratch store.** With `TRIAGE_STORE_URL=sqlite:///<scratchpad>/e2e.db` and `TRIAGE_REPO=paperclipai/paperclip`, seed PR 11987's record and its registry incident from `~/Downloads/paperclip-malware-evidence-2026-10-02/prospector-store-records.json`, run `capture --backfill`, `export --pr 11987 --out <scratchpad>/e2e-export`, and check: `git patch-id --stable < force-push-changes.diff` → `5e56c29534cf5663948fc228b87f8f47d92d02b5`; the PR diff → `15de2acc7b5d9f9d3c5748656f0dad8534f5cf1b`; `shasum -a 256 -c SHA256SUMS` passes. Never point this run at the live store: this branch's schema 29 would lock out every v28 writer.
- [ ] **Step 4: Commit** `git commit -m "Document threat evidence"`
