"""Canonical issue store — the ONLY accessor for issue_triage/store/.

One SQL row per issue (facts, each section stamped checked_at + against_updated_at),
one per issue-cluster (membership + curation + pain), and a shared runs table ledger.
Everything validates on write so reads never need defensive parsing.
Markdown is generated FROM this store (issue_views.py), never parsed back. Mirrors
pipeline/store.py for PRs, over the shared storekit core.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import schema
from pipeline import storekit
from pipeline.storekit import Collection, ValidationError

if TYPE_CHECKING:
    from issue_triage import issue_model

DEFAULT_ROOT = Path(__file__).resolve().parent / "store"

ISSUE_DISPOSITIONS = {"close-dup", "request-repro", "link-pr", "needs-human", "close-fixed"}
ISSUE_STATES = {"open", "closed"}
ISSUE_RESOLUTION_STATES = {"pending", "closed-dup"}
REPRO_GRADES = {"A", "B", "C", "D", "F"}
FIX_SCAN_STATES = {"fixed", "likely-fixed", "not-fixed"}
ISSUE_SECTIONS = ("meta", "summary", "repro", "cluster", "analysis", "links", "resolution",
                  "fix_scan")


def validate_issue(rec: dict) -> None:
    if not isinstance(rec.get("issue"), int):
        raise ValidationError("issue: required int")
    meta = rec.get("meta")
    if not isinstance(meta, dict):
        raise ValidationError("meta: required section")
    for field in ("title", "state", "updated_at"):
        if not meta.get(field):
            raise ValidationError(f"meta.{field}: required")
    if meta["state"] not in ISSUE_STATES:
        raise ValidationError(f"meta.state: {meta['state']!r} not in {sorted(ISSUE_STATES)}")
    for key in rec:
        if key != "issue" and key not in ISSUE_SECTIONS:
            storekit.warn_unknown_section("issues", key)
    a = rec.get("analysis")
    if a:
        if a.get("disposition") not in ISSUE_DISPOSITIONS:
            raise ValidationError(
                f"analysis.disposition: {a.get('disposition')!r} not in {sorted(ISSUE_DISPOSITIONS)}")
        if a["disposition"] == "close-dup" and not a.get("canonical"):
            raise ValidationError("analysis.canonical: required for close-dup")
        if a["disposition"] == "close-fixed" and not a.get("fixed_by"):
            raise ValidationError("analysis.fixed_by: required for close-fixed")
    rp = rec.get("repro")
    if rp and rp.get("grade") not in REPRO_GRADES:
        raise ValidationError(f"repro.grade: {rp.get('grade')!r} not in {sorted(REPRO_GRADES)}")
    r = rec.get("resolution")
    if r and r.get("status") not in ISSUE_RESOLUTION_STATES:
        raise ValidationError(
            f"resolution.status: {r.get('status')!r} not in {sorted(ISSUE_RESOLUTION_STATES)}")
    fs = rec.get("fix_scan")
    if fs and fs.get("status") not in FIX_SCAN_STATES:
        raise ValidationError(
            f"fix_scan.status: {fs.get('status')!r} not in {sorted(FIX_SCAN_STATES)}")


def validate_issue_cluster(rec: dict) -> None:
    if not isinstance(rec.get("id"), int):
        raise ValidationError("id: required int")
    if not rec.get("title"):
        raise ValidationError("title: required")
    if not isinstance(rec.get("members"), list):
        raise ValidationError("members: required list")
    if rec.get("canonical") is not None and not isinstance(rec["canonical"], int):
        raise ValidationError("canonical: must be int or null")


class IssueStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = storekit.get_engine(storekit.resolve_url(root, DEFAULT_ROOT))
        with storekit.bound_session(self.engine):
            storekit.ensure_schema(self.engine)
            storekit.refresh_schema_guard(self.engine)
        self._issues: Collection[issue_model.Issue] = Collection(
            self.engine, schema.issues, "issue", validate_issue, self._issue_view,
            schema.mirror_issue)
        self._clusters: Collection[issue_model.IssueCluster] = Collection(
            self.engine, schema.issue_clusters, "id", validate_issue_cluster,
            self._cluster_view, schema.mirror_issue_cluster)

    def _issue_view(self, rec: dict) -> issue_model.Issue:
        from issue_triage import issue_model
        return issue_model.Issue(self, rec)

    def _cluster_view(self, rec: dict) -> issue_model.IssueCluster:
        from issue_triage import issue_model
        return issue_model.IssueCluster(self, rec)

    @contextmanager
    def batch(self) -> Generator[None]:
        """Run a block of store operations over one reused connection instead of a
        fresh one per statement. Each write still commits in its own transaction —
        durable if interrupted — so this is a pure speed-up for a loop of per-record
        reads/writes against a networked pooler. Wrap any commit loop in it."""
        with storekit.bound_session(self.engine):
            yield

    # -- Issues -------------------------------------------------------------
    def load_issue(self, n: int) -> issue_model.Issue | None:
        return self._issues.load(n)

    def save_issue(self, rec) -> None:
        self._issues.save(rec)

    def edit_issue(self, n: int) -> issue_model.Issue:
        """A typed, auto-saving handle for mutating issue `n`. Raises KeyError if
        the issue is not in the store."""
        return self._issues.edit(n)

    def save_issues_many(self, recs: list[dict]) -> None:
        """Persist many issue records in one validated bulk UPSERT."""
        self._issues.save_many(recs)

    def all_issues(self, *, omit_candidates: bool = False) -> dict[int, issue_model.Issue]:
        omit = [("links", "candidates")] if omit_candidates else None
        return self._issues.all(omit_paths=omit)

    def issues_since(self, watermark: str | None, *, omit_candidates: bool = False
                     ) -> tuple[dict[int, issue_model.Issue], str | None]:
        omit = [("links", "candidates")] if omit_candidates else None
        return self._issues.since(watermark, omit_paths=omit)

    def load_issues(self, ns: list[int]) -> dict[int, issue_model.Issue]:
        from sqlalchemy import select
        ids = [int(n) for n in ns]
        if not ids:
            return {}

        def q(conn):
            return conn.execute(
                select(schema.issues.c.issue, schema.issues.c.data)
                .where(schema.issues.c.issue.in_(ids))).all()
        rows = storekit.read_retrying(self.engine, q)
        return {r[0]: self._issue_view(r[1]) for r in rows}

    def create_issue(self, n: int, meta: dict) -> issue_model.Issue:
        """Create + persist a new issue record from its meta section and return it
        bound. The meta section carries checked_at but no against_updated_at (meta
        carries updated_at, it is not stamped against it)."""
        from issue_triage import issue_model
        rec = {"issue": int(n), "meta": dict(meta, checked_at=storekit.now())}
        self._issues.save(rec)
        return issue_model.Issue(self, rec)

    # -- Issue clusters -----------------------------------------------------
    def load_issue_cluster(self, cid: int) -> issue_model.IssueCluster | None:
        return self._clusters.load(cid)

    def save_issue_cluster(self, rec) -> None:
        self._clusters.save(rec)

    def edit_issue_cluster(self, cid: int) -> issue_model.IssueCluster:
        return self._clusters.edit(cid)

    def delete_issue_clusters(self, cids: list[int]) -> None:
        """Delete many issue-cluster rows in one statement. Backrefs of issues
        pointing at them are the caller's to rewire (the cluster driver stages
        replacements and bulk-saves)."""
        self._clusters.delete_many(cids)

    def save_issue_clusters_many(self, recs: list[dict]) -> None:
        """Persist many issue-cluster records in one validated bulk UPSERT."""
        self._clusters.save_many(recs)

    def all_issue_clusters(self) -> dict[int, issue_model.IssueCluster]:
        return self._clusters.all()

    def issue_clusters_since(self, watermark: str | None
                             ) -> tuple[dict[int, issue_model.IssueCluster], str | None]:
        return self._clusters.since(watermark)

    def create_issue_cluster(self, cid: int, title: str, *, subsystem: str | None = None,
                             members: list[int] | None = None) -> issue_model.IssueCluster:
        """Create + persist a new issue-cluster (empty members unless given) and
        return it bound. Members are wired via IssueCluster.set_members so each
        member's backref is set."""
        from issue_triage import issue_model
        rec = {"id": int(cid), "title": title, "subsystem": subsystem,
               "members": [], "canonical": None, "needs_review": False,
               "checked_at": storekit.now()}
        self._clusters.save(rec)
        cl = issue_model.IssueCluster(self, rec)
        if members:
            cl.set_members(members)
        return cl

    # -- Runs ledger --------------------------------------------------------
    def append_run(self, record: dict) -> None:
        """Append one ledger record, validated: it must parse as one of the
        ledger's two shapes (storekit.parse_run) or the append raises."""
        from sqlalchemy import insert
        storekit.parse_run(record)
        storekit.assert_writable(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(schema.runs).values(
                kind="issue", data=record, ts=record.get("ts") or storekit.now()))

    def runs(self) -> list[storekit.RunRecord]:
        """The issue ledger's records, typed (PhaseRun | StoreEdit), oldest first."""
        from sqlalchemy import select
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.runs.c.data)
                .where(schema.runs.c.kind == "issue")
                .order_by(schema.runs.c.rowid)).all()
        return [storekit.parse_run(r[0]) for r in rows]
