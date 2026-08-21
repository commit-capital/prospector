"""Canonical pipeline store — the ONLY accessor for pipeline/store/.

One SQL row per PR (facts, each section stamped with checked_at +
against_head_sha), one per cluster (membership + analysis + outcome), and a
`runs` table ledger. Everything validates on write so reads never need
defensive parsing. Markdown is generated FROM this store (views.py) and
never parsed back.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import gates
from pipeline import reviewers
from pipeline import schema
from pipeline import settings
from pipeline import storekit
from pipeline.storekit import Collection, ValidationError

if TYPE_CHECKING:
    from pipeline import model

DEFAULT_ROOT = Path(__file__).resolve().parent / "store"

DISPOSITIONS = {"merge", "request-changes", "close-dup", "close-fixed", "close-stale", "needs-human"}
OUTCOMES = {"merge-ready", "awaiting-authors", "needs-first-party-work", "close-out", "blocked-on-decision"}
SECURITY_VERDICTS = {"GREEN", "YELLOW", "RED"}
DRIFT_STATES = {"applicable", "already-fixed", "conflicts"}
PR_STATES = {"open", "closed", "merged"}
THREAT_VERDICTS = {"malicious", "suspicious", "clear"}
GREPTILE_SEVERITIES = ("defects", "nits", "clean")
GREPTILE_FINDING_CLASSES = ("substantive", "nitpick")
# The sandbox tiers a base image can be built at: 0 ships the scrubbed clone, 1
# additionally bakes main's pinned dependencies in via an offline install. Both
# run with no egress; the tier names which image, and is a property of it.
VERIFY_TIERS = {0, 1}

# The lifecycle of an operator-queued sandbox verification (the verify_request
# section): queued in the app, picked up and run by the verify worker,
# parked as waiting-for-base while the runner has no usable pinned base, or
# re-queued `queued` with an attempt count after a transient failure (the
# worker retries both, bounded), and finished as done / error / cancelled.
# Terminal states stay on the record so the app can show what happened;
# re-queueing replaces the section.
VERIFY_REQUEST_STATUSES = {"queued", "running", "waiting-for-base", "done", "error",
                           "cancelled"}

# Why a verification request ended in `error` (or was transiently re-queued):
# the safety floor refused it, no usable pinned base exists on the runner, the
# PR's diff could not be fetched upstream, a headless agent failed, the sandbox
# infrastructure failed, the worker restarted mid-run, the run errored so no
# verdict is trusted (hold), or the orchestrator itself crashed.
VERIFY_ERROR_KINDS = {"refused-safety", "no-base", "fetch-error", "agent-failed",
                      "sandbox-error", "interrupted", "hold", "exception"}

# Who queued a verification request: the idle auto-hunter stamps its picks
# "auto", and "auto-resweep" on the lane that re-runs a concluded verification
# whose independent repro the harness broke; the app's operator path leaves the
# field unset.
VERIFY_REQUEST_SOURCES = {"operator", "auto", "auto-resweep"}

# The sources the hunter selected itself. The security-clearance precondition
# (the sandbox never runs code no adversarial review cleared), the autohunt
# run-ledger trigger, and the queue's operator-picks-first ordering all key on
# this rather than on any single spelling, so a new hunter lane is covered by
# each of them the moment it is added here.
AUTO_REQUEST_SOURCES = {"auto", "auto-resweep"}

# What an autofix request asks the fix worker to do on the PR's head branch:
# merge the base branch in so checks re-run against current base code, rebase
# onto current base behind a pinned lease to clear a conflict, have an agent
# author a change against a failing gate, or resolve a base merge's conflicts
# with agent-authored content parked for review.
FIX_ACTIONS = set(settings.FIX_ACTIONS)

# The lifecycle of a fix request: queued in any app, picked up and run by the
# fix worker, parked as awaiting-review with the authored diff for a human to
# approve, approved and pushed. `refused` covers a gate or preflight that
# blocked the push (nothing reached upstream); `failed` an action that errored.
# Terminal states stay on the record so the app can show what happened;
# re-queueing replaces the section.
FIX_REQUEST_STATUSES = {"queued", "running", "awaiting-review", "approved", "pushing",
                        "pushed", "refused", "failed", "cancelled"}

# Who queued a fix request: the idle auto-hunter stamps its picks "auto"; the
# app's operator path leaves the field unset.
FIX_REQUEST_SOURCES = {"operator", "auto"}

# every per-PR record section this code knows; an unknown section is preserved
# through save (with a stderr notice) so a checkout behind the store schema
# round-trips newer records without loss
PR_SECTIONS = ("meta", "signals", "reviews", "drift", "summary", "cluster", "analysis",
               "security", "issues", "threat", "greptile_review", "verify", "verify_request",
               "fix_request", "security_run")


def _older_than(stamp: object, seconds: float) -> bool:
    """Whether an ISO timestamp is further back than `seconds`. An absent or
    unparseable stamp reads as old: a claim that cannot say when it started is
    one nothing can wait on."""
    from datetime import datetime, timezone
    if not isinstance(stamp, str):
        return True
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - at).total_seconds() > seconds


def _mirror_pr(rec: dict) -> dict:
    """The prs table's convenience mirror columns. The disposition column mirrors
    the DERIVED disposition (model.Pr.disposition — the stored ANALYZE verdict
    plus any route current facts force on a merge pick). Every fact lives in this
    same row, so the derived value only changes on a row write and the column
    stays in step."""
    from pipeline import model
    row = schema.mirror_pr(rec)
    row["disposition"] = model.Pr(None, rec).disposition
    return row


def validate_pr(rec: dict) -> None:
    if not isinstance(rec.get("pr"), int):
        raise ValidationError("pr: required int")
    meta = rec.get("meta")
    if not isinstance(meta, dict):
        raise ValidationError("meta: required section")
    for field in ("title", "state", "head_sha"):
        if not meta.get(field):
            raise ValidationError(f"meta.{field}: required")
    if meta["state"] not in PR_STATES:
        raise ValidationError(f"meta.state: {meta['state']!r} not in {sorted(PR_STATES)}")
    for key in rec:
        if key != "pr" and key not in PR_SECTIONS:
            storekit.warn_unknown_section("prs", key)
    a = rec.get("analysis")
    if a:
        if a.get("disposition") not in DISPOSITIONS:
            raise ValidationError(f"analysis.disposition: {a.get('disposition')!r} not in {sorted(DISPOSITIONS)}")
        if a["disposition"] == "close-dup" and not a.get("canonical"):
            raise ValidationError("analysis.canonical: required for close-dup")
        if a.get("upstream_pr") is not None and not isinstance(a.get("upstream_pr"), int):
            raise ValidationError("analysis.upstream_pr: must be int")
    cl = rec.get("cluster")
    if cl is not None:
        if "id" in cl:
            raise ValidationError("cluster.id: removed — use cluster.ids (list of ints)")
        ids = cl.get("ids", [])
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValidationError("cluster.ids: must be a list of ints")
    if a and a.get("from_cluster") is not None and not isinstance(a.get("from_cluster"), int):
        raise ValidationError("analysis.from_cluster: must be int")
    s = rec.get("security")
    if s and s.get("verdict") not in SECURITY_VERDICTS:
        raise ValidationError(f"security.verdict: {s.get('verdict')!r} not in {sorted(SECURITY_VERDICTS)}")
    v = rec.get("verify")
    if v:
        if v.get("outcome") is not None and v["outcome"] not in gates.VERIFY_OUTCOMES:
            raise ValidationError(
                f"verify.outcome: {v.get('outcome')!r} not in {sorted(gates.VERIFY_OUTCOMES)}")
        if not v.get("against_base_sha"):
            raise ValidationError("verify.against_base_sha: required")
        signals = v.get("signals")
        blind = signals.get("blind_adequacy") if isinstance(signals, dict) else None
        if isinstance(blind, dict):
            rr = blind.get("repro_rejected")
            if rr is not None and not isinstance(rr, str):
                raise ValidationError(
                    "verify.signals.blind_adequacy.repro_rejected: must be a string")
    vr = rec.get("verify_request")
    if vr:
        if vr.get("status") not in VERIFY_REQUEST_STATUSES:
            raise ValidationError(
                f"verify_request.status: {vr.get('status')!r} not in "
                f"{sorted(VERIFY_REQUEST_STATUSES)}")
        if vr.get("error_kind") is not None and vr["error_kind"] not in VERIFY_ERROR_KINDS:
            raise ValidationError(
                f"verify_request.error_kind: {vr.get('error_kind')!r} not in "
                f"{sorted(VERIFY_ERROR_KINDS)}")
        if vr.get("source") is not None and vr["source"] not in VERIFY_REQUEST_SOURCES:
            raise ValidationError(
                f"verify_request.source: {vr.get('source')!r} not in "
                f"{sorted(VERIFY_REQUEST_SOURCES)}")
    fr = rec.get("fix_request")
    if fr:
        if fr.get("status") not in FIX_REQUEST_STATUSES:
            raise ValidationError(
                f"fix_request.status: {fr.get('status')!r} not in "
                f"{sorted(FIX_REQUEST_STATUSES)}")
        if fr.get("action") not in FIX_ACTIONS:
            raise ValidationError(
                f"fix_request.action: {fr.get('action')!r} not in {sorted(FIX_ACTIONS)}")
        if fr.get("source") is not None and fr["source"] not in FIX_REQUEST_SOURCES:
            raise ValidationError(
                f"fix_request.source: {fr.get('source')!r} not in "
                f"{sorted(FIX_REQUEST_SOURCES)}")
    sr = rec.get("security_run")
    if sr and not sr.get("host"):
        raise ValidationError("security_run.host: required")
    d = rec.get("drift")
    if d and d.get("state") not in DRIFT_STATES:
        raise ValidationError(f"drift.state: {d.get('state')!r} not in {sorted(DRIFT_STATES)}")
    t = rec.get("threat")
    if t and t.get("verdict") not in THREAT_VERDICTS:
        raise ValidationError(f"threat.verdict: {t.get('verdict')!r} not in {sorted(THREAT_VERDICTS)}")
    gr = rec.get("greptile_review")
    if gr:
        if gr.get("severity") not in GREPTILE_SEVERITIES:
            raise ValidationError(
                f"greptile_review.severity: {gr.get('severity')!r} not in {sorted(GREPTILE_SEVERITIES)}")
        findings = gr.get("findings", [])
        if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
            raise ValidationError("greptile_review.findings: must be a list of dicts")
        for f in findings:
            if f.get("class") not in GREPTILE_FINDING_CLASSES:
                raise ValidationError(
                    f"greptile_review.findings[].class: {f.get('class')!r} not in "
                    f"{sorted(GREPTILE_FINDING_CLASSES)}")
    rv = rec.get("reviews")
    if rv:
        for rid, entry in rv.items():
            if rid not in reviewers.REVIEWERS:
                continue
            if not isinstance(entry, dict) or entry.get("kind") not in reviewers.KINDS:
                raise ValidationError(
                    f"reviews.{rid}.kind: {(entry or {}).get('kind') if isinstance(entry, dict) else entry!r} "
                    f"not in {list(reviewers.KINDS)}")
            for field_name in ("findings", "checks"):
                val = entry.get(field_name, [])
                if not isinstance(val, list) or not all(isinstance(x, dict) for x in val):
                    raise ValidationError(f"reviews.{rid}.{field_name}: must be a list of dicts")


def validate_cluster(rec: dict) -> None:
    if not isinstance(rec.get("id"), int):
        raise ValidationError("id: required int")
    if not rec.get("root_problem"):
        raise ValidationError("root_problem: required")
    if not isinstance(rec.get("prs"), list):
        raise ValidationError("prs: required list")
    if "outcome" in rec and rec["outcome"] is not None and rec["outcome"] not in OUTCOMES:
        raise ValidationError(f"outcome: {rec['outcome']!r} not in {sorted(OUTCOMES)}")
    if rec.get("rationale_summary") is not None and not isinstance(rec["rationale_summary"], str):
        raise ValidationError("rationale_summary: must be str or null")
    if "deleted" in rec and not isinstance(rec["deleted"], bool):
        raise ValidationError("deleted: must be bool")
    props = rec.get("proposals")
    if props is not None:
        if not isinstance(props, list):
            raise ValidationError("proposals: must be a list")
        for p in props:
            if not isinstance(p, dict) or not isinstance(p.get("pr"), int):
                raise ValidationError("proposals[]: each row needs an int pr")
            if p.get("disposition") not in DISPOSITIONS:
                raise ValidationError(
                    f"proposals[].disposition: {p.get('disposition')!r} not in {sorted(DISPOSITIONS)}")


class Store:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = storekit.get_engine(storekit.resolve_url(root, DEFAULT_ROOT))
        # One connection for the whole construction: against a networked pooler the
        # connect handshake costs far more than the schema check and guard read that
        # run over it.
        with storekit.bound_session(self.engine):
            storekit.ensure_schema(self.engine)
            storekit.refresh_schema_guard(self.engine)
        self._prs: Collection[model.Pr] = Collection(
            self.engine, schema.prs, "pr", validate_pr, self._pr_view, _mirror_pr)
        self._clusters: Collection[model.Cluster] = Collection(
            self.engine, schema.clusters, "id", validate_cluster, self._cluster_view,
            schema.mirror_cluster)

    def _pr_view(self, rec: dict) -> model.Pr:
        from pipeline import model
        return model.Pr(self, rec)

    def _cluster_view(self, rec: dict) -> model.Cluster:
        from pipeline import model
        return model.Cluster(self, rec)

    @contextmanager
    def batch(self) -> Generator[None]:
        """Run a block of store operations over one reused connection instead of a
        fresh one per statement. Each write still commits in its own transaction —
        durable if interrupted — so this is a pure speed-up for a loop of per-record
        reads/writes against a networked pooler. Wrap any commit loop in it."""
        with storekit.bound_session(self.engine):
            yield

    # -- PRs ----------------------------------------------------------------
    def load_pr(self, n: int) -> model.Pr | None:
        return self._prs.load(n)

    def save_pr(self, rec) -> None:
        self._prs.save(rec)

    def save_prs_many(self, prs: list[model.Pr]) -> None:
        """Persist many PR records in one validated bulk UPSERT."""
        self._prs.save_many([pr.raw for pr in prs])

    def all_prs(self) -> dict[int, model.Pr]:
        """Every PR, with `meta.body` (the PR description — ~40% of the store and
        unused by the board, summarize, gates, and suggest) omitted from the bulk
        read. The detail and deep-search paths rehydrate it per-PR via
        `pr_bodies`; everything else never touches it."""
        return self._prs.all(omit_paths=[("meta", "body")])

    def prs_since(self, watermark: str | None) -> tuple[dict[int, model.Pr], str | None]:
        """PRs written at or after `watermark` (all when None), body omitted, plus
        the new watermark — the incremental refetch a cached reader runs to pick up
        only changed PRs. See storekit Collection.since."""
        return self._prs.since(watermark, omit_paths=[("meta", "body")])

    def clusters_since(self, watermark: str | None
                       ) -> tuple[dict[int, model.Cluster], list[int], str | None]:
        """Clusters written at or after `watermark` (all when None), split into live
        records and the ids of soft-deleted (tombstoned) ones, plus the new
        watermark — the incremental refetch for cluster changes. The watermark
        covers tombstones too, so a cached reader drops a removed cluster once and
        then advances past it. A tombstone rides the same `saved_at` channel as any
        update, so a recluster's deletions reach every operator's snapshot."""
        views, hi = self._clusters.since(watermark)
        live: dict[int, model.Cluster] = {}
        deleted: list[int] = []
        for cid, c in views.items():
            if c.raw.get("deleted"):
                deleted.append(cid)
            else:
                live[cid] = c
        return live, deleted, hi

    def pr_states(self) -> dict[int, str]:
        """Every stored PR's current state, keyed by PR number. Reads the indexed
        `state` mirror column alone, so a caller that needs nothing else never
        pulls the JSON records."""
        from sqlalchemy import select

        def q(conn) -> list:
            return conn.execute(select(schema.prs.c.pr, schema.prs.c.state)).all()
        return {r[0]: r[1] for r in storekit.read_retrying(self.engine, q) if r[1]}

    def pr_bodies(self, ns: list[int]) -> dict[int, str | None]:
        """The stored `meta.body` for each of `ns` — the field `all_prs` omits —
        fetched on demand for the detail and deep-search paths. Reads the records
        for just these PRs and projects the body in Python, so it stays
        dialect-agnostic; these paths touch few PRs (one open detail, or deep
        search's ≤500 capped candidate set)."""
        from sqlalchemy import select
        ids = [int(n) for n in ns]
        if not ids:
            return {}
        def q(conn) -> list:
            return conn.execute(
                select(schema.prs.c.pr, schema.prs.c.data)
                .where(schema.prs.c.pr.in_(ids))).all()
        rows = storekit.read_retrying(self.engine, q)
        return {r[0]: (r[1].get("meta") or {}).get("body") for r in rows}

    def edit_pr(self, n: int) -> model.Pr:
        """A typed, auto-saving handle for mutating PR `n`. Raises KeyError if the
        PR is not in the store."""
        return self._prs.edit(n)

    def create_pr(self, n: int, meta: dict) -> model.Pr:
        """Create + persist a new PR record from its meta section and return it
        bound. Mirrors create_cluster; the meta section carries checked_at but no
        against_head_sha (meta is the head, not stamped against it)."""
        from pipeline import model
        rec = {"pr": int(n), "meta": dict(meta, checked_at=storekit.now())}
        self._prs.save(rec)
        return model.Pr(self, rec)

    # -- Clusters -----------------------------------------------------------
    def load_cluster(self, cid: int) -> model.Cluster | None:
        """The cluster `cid`, or None when absent or soft-deleted — a tombstoned
        cluster reads as gone to every caller; only `clusters_since` surfaces it
        (as a deletion) and `reap_cluster_tombstones` sees the row."""
        c = self._clusters.load(cid)
        return None if c is not None and c.raw.get("deleted") else c

    def save_cluster(self, rec) -> None:
        self._clusters.save(rec)

    def edit_cluster(self, cid: int) -> model.Cluster:
        """A typed, auto-saving handle for mutating cluster `cid`. Raises KeyError if
        the cluster is not in the store."""
        return self._clusters.edit(cid)

    def create_cluster(self, cid: int, root_problem: str,
                       *, outcome: str | None = None) -> model.Cluster:
        """Create + persist a new cluster record (empty prs) and return it bound.
        Members are wired separately via Cluster.set_members."""
        from pipeline import model
        rec = {"id": int(cid), "root_problem": root_problem, "prs": [],
               "outcome": outcome, "checked_at": storekit.now()}
        self._clusters.save(rec)
        return model.Cluster(self, rec)

    def delete_cluster(self, cid: int) -> None:
        """Soft-delete: tombstone the cluster (mark deleted, which bumps its
        saved_at) so the removal rides the `clusters_since` watermark every app
        polls and reaches each operator's snapshot without a server bounce. The row
        is hard-removed later by reap_cluster_tombstones. No-op if the cluster is
        absent or already tombstoned."""
        c = self._clusters.load(cid)
        if c is None or c.raw.get("deleted"):
            return
        rec = c.raw
        rec["deleted"] = True
        self._clusters.save(rec)

    def reap_cluster_tombstones(self, older_than_seconds: int = 3600) -> int:
        """Hard-delete tombstones whose soft-delete is older than
        `older_than_seconds`, returning how many were reaped. Out-of-band cleanup:
        the cutoff is vast next to the ~10s freshen poll, so every watermark reader
        has long since observed the removal (and a reader that was down rebuilds
        from the live rows on its next cold load, never needing the tombstone)."""
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import delete as sa_delete
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
                  ).isoformat(timespec="microseconds")
        with self.engine.begin() as conn:
            res = conn.execute(sa_delete(schema.clusters).where(
                schema.clusters.c.deleted.is_(True), schema.clusters.c.saved_at < cutoff))
        return res.rowcount or 0

    def all_clusters(self) -> dict[int, model.Cluster]:
        """Every live cluster, keyed by id; tombstoned rows awaiting reaping are
        skipped, so every consumer sees only real clusters."""
        return {cid: c for cid, c in self._clusters.all().items() if not c.raw.get("deleted")}

    # -- Runs ledger ----------------------------------------------------------
    def append_run(self, record: dict) -> None:
        """Append one ledger record, validated: it must parse as one of the
        ledger's two shapes (storekit.parse_run) or the append raises."""
        from sqlalchemy import insert
        storekit.parse_run(record)
        storekit.assert_writable(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(schema.runs).values(
                kind="pr", data=record, ts=record.get("ts") or storekit.now()))

    def runs(self, limit: int | None = None, since: str | None = None) -> list[storekit.RunRecord]:
        """The run-ledger records, typed (PhaseRun | StoreEdit), oldest first.
        With `limit` omitted, every record; with `limit` given, only the last
        `limit` records — read newest-first with a bounded query, then
        reversed back to insertion order before returning. `since` filters to
        records inserted at or after that ISO instant, against the ledger's
        indexed `ts` column — cheap regardless of the ledger's total size —
        and composes with `limit`."""
        from sqlalchemy import select
        query = select(schema.runs.c.data).where(schema.runs.c.kind == "pr")
        if since is not None:
            query = query.where(schema.runs.c.ts >= since)
        if limit is None:
            query = query.order_by(schema.runs.c.rowid)
            with self.engine.connect() as conn:
                rows = conn.execute(query).all()
            return [storekit.parse_run(r[0]) for r in rows]
        query = query.order_by(schema.runs.c.rowid.desc()).limit(limit)
        with self.engine.connect() as conn:
            rows = conn.execute(query).all()
        return [storekit.parse_run(r[0]) for r in reversed(rows)]

    # -- Singleton registries (threats, action_items) -----------------------
    # Durable, cross-PR data: not head-stamped. Each registry is one row in the
    # `registries` table keyed by name; the JSON shape is unchanged.
    def _load_registry(self, name: str, default: dict) -> dict:
        from sqlalchemy import select
        with self.engine.connect() as conn:
            row = conn.execute(
                select(schema.registries.c.data)
                .where(schema.registries.c.name == name)).first()
        return row[0] if row is not None else default

    def _save_registry(self, name: str, data: dict) -> None:
        from sqlalchemy import delete as sa_delete, insert
        storekit.assert_writable(self.engine)
        with self.engine.begin() as conn:
            conn.execute(sa_delete(schema.registries).where(schema.registries.c.name == name))
            conn.execute(insert(schema.registries).values(name=name, data=data))

    def load_reviewers(self) -> dict:
        """Each automated reviewer's latest observed activity over the open
        corpus — what `review_policy` auto-detection reads."""
        return self._load_registry("reviewers", {"seen": {}, "computed_at": None})

    def save_reviewers(self, registry: dict) -> None:
        self._save_registry("reviewers", registry)

    def load_threats(self) -> dict:
        return self._load_registry("threats", {"actors": {}, "incidents": []})

    def save_threats(self, registry: dict) -> None:
        if not isinstance(registry.get("actors"), dict):
            raise ValidationError("threats.actors: required dict")
        if not isinstance(registry.get("incidents"), list):
            raise ValidationError("threats.incidents: required list")
        self._save_registry("threats", registry)

    def load_action_items(self) -> dict:
        return self._load_registry("action_items", {"items": []})

    def save_action_items(self, registry: dict) -> None:
        if not isinstance(registry.get("items"), list):
            raise ValidationError("action_items.items: required list")
        self._save_registry("action_items", registry)

    def load_response_acks(self) -> dict:
        """Which PRs' community-response signals an operator has marked seen
        (`{acks: {"<pr>": {at, by}}}`), shared across operators — an ack clears
        the signal from every instance's queue until a response newer than the
        ack's `at` supersedes it."""
        return self._load_registry("response_acks", {"acks": {}})

    def save_response_acks(self, registry: dict) -> None:
        if not isinstance(registry.get("acks"), dict):
            raise ValidationError("response_acks.acks: required dict")
        self._save_registry("response_acks", registry)

    def load_live_sweep(self) -> dict:
        """When the app's live sweep last ran (`{swept_at}`), shared across
        operators — one operator's sweep tells every app how fresh the live
        state is, and gates the launch-time re-sweep (PROSPECTOR_LIVE_TTL_MIN)."""
        return self._load_registry("live_sweep", {"swept_at": None})

    def save_live_sweep(self, registry: dict) -> None:
        self._save_registry("live_sweep", registry)

    # -- Shared diff cache ---------------------------------------------------
    # One row per fetched PR head (`diffs` table): the capped diff text keyed
    # by the head SHA it was taken at. A head's diff never changes, so rows are
    # immutable — writes insert absent heads and leave existing ones untouched,
    # and reads need no staleness check. diff_cache.py is the one writer and
    # owns the size cap; these accessors own the rows.
    def load_diff(self, head_sha: str) -> str | None:
        """The cached diff body for `head_sha`, or None when never uploaded."""
        from sqlalchemy import select
        def q(conn) -> tuple | None:
            return conn.execute(
                select(schema.diffs.c.body)
                .where(schema.diffs.c.head_sha == head_sha)).first()
        row = storekit.read_retrying(self.engine, q)
        return None if row is None else row[0]

    def load_diffs(self, head_shas: list[str]) -> dict[str, str]:
        """The cached diff bodies present for `head_shas`, in one query —
        absent heads are simply missing from the result."""
        from sqlalchemy import select
        if not head_shas:
            return {}
        def q(conn) -> list:
            return conn.execute(
                select(schema.diffs.c.head_sha, schema.diffs.c.body)
                .where(schema.diffs.c.head_sha.in_(head_shas))).all()
        return {r[0]: r[1] for r in storekit.read_retrying(self.engine, q)}

    def save_diffs_many(self, rows: list[tuple[str, int | None, str]]) -> None:
        """Insert `(head_sha, pr, body)` rows for heads absent from the table;
        an existing head is left untouched. Validated; one statement per call."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        storekit.assert_writable(self.engine)
        seen: set[str] = set()
        values: list[dict] = []
        for head_sha, pr, body in rows:
            if not head_sha or not isinstance(head_sha, str):
                raise ValidationError("diffs.head_sha: required str")
            if not body or not isinstance(body, str):
                raise ValidationError("diffs.body: required str")
            if head_sha in seen:
                continue
            seen.add(head_sha)
            values.append({"head_sha": head_sha, "pr": pr, "body": body,
                           "fetched_at": storekit.now()})
        if not values:
            return
        ins = (pg_insert(schema.diffs) if self.engine.dialect.name == "postgresql"
               else sqlite_insert(schema.diffs))
        stmt = ins.values(values).on_conflict_do_nothing(
            index_elements=[schema.diffs.c.head_sha])
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def load_author_baseline(self) -> dict:
        """Per-author PR counts for PRs closed/merged before our first ingest —
        the ones absent from the store. Captured once by `pipeline.authors` and
        summed with the live store group-by to form the author leaderboard (#453)."""
        return self._load_registry("author_baseline", {"authors": {}, "materialized_at": None})

    def save_author_baseline(self, registry: dict) -> None:
        if not isinstance(registry.get("authors"), dict):
            raise ValidationError("author_baseline.authors: required dict")
        self._save_registry("author_baseline", registry)

    # One machine's absent pin. `baseline_failing` of None means no baseline was
    # ever captured there — distinct from [], the suite passed clean.
    _NO_PIN = {"base_sha": None, "tier": None, "pinned_at": None,
               "baseline_failing": None, "baseline_captured_at": None}

    def load_verify_base_hosts(self) -> dict[str, dict]:
        """Every machine's pinned base, keyed by hostname (`{<hostname>:
        {base_sha, tier, pinned_at, baseline_failing, baseline_captured_at}}`).

        A pin names a Docker image and a scrubbed clone on that machine's own
        disk, so each verification machine carries its own and tracks the
        default branch on its own daily cadence. Pins are never pruned: a
        machine that is merely offline still holds its artifacts.

        A record written flat (one machine's pin with no `hosts` key) reads as a
        single-entry map under the host that prepared it; that host name becomes
        the key and is dropped from the record."""
        reg = self._load_registry("verify_base", {"hosts": {}})
        if "hosts" in reg:
            return reg["hosts"]
        host = reg.pop("prepared_on", None)
        return {str(host): reg} if host else {}

    def load_verify_base(self, host: str) -> dict:
        """`host`'s pinned base, or the empty pin when that machine has none.
        verify_driver.prepare_base writes it; verify_pr reads the base SHA and
        tier back to name the image it boots."""
        return self.load_verify_base_hosts().get(host, dict(self._NO_PIN))

    def load_verify_worker(self) -> dict:
        """Sandbox verification-worker heartbeats, one record per host
        (`{hosts: {<hostname>: {host, pid, last_beat, current_pr, autohunt,
        security_failed}}}`) — which machines' workers exist, when each last
        beat, the PR each is running (None when idle), whether each idle
        auto-hunt is enabled, and each worker's this-process security-failure
        memory. Every worker merges its own record in each drain tick; any
        app reads the map to show which runners are online. An empty map
        means no worker has ever run against this store. A record written flat
        (a single host's heartbeat with no `hosts` key) reads as a
        single-entry map."""
        reg = self._load_registry("verify_worker", {"hosts": {}})
        if "hosts" in reg:
            return reg
        host = reg.get("host")
        return {"hosts": {str(host): reg}} if host else {"hosts": {}}

    # Heartbeat records that stop beating are pruned once this old — long past
    # every liveness window, so a decommissioned worker eventually leaves the map.
    _WORKER_PRUNE_SECONDS = 7 * 24 * 3600

    def save_verify_worker(self, record: dict) -> None:
        """Merge one host's heartbeat record into the verify_worker registry,
        pruning entries stale past _WORKER_PRUNE_SECONDS. host and last_beat
        are required: a beat that names no machine or no time tells an app
        nothing about whether a runner is online. Two hosts' merges may race on
        the shared row; a lost beat is rewritten by the loser's next tick."""
        if not record.get("host"):
            raise ValidationError("verify_worker.host: required")
        if not record.get("last_beat"):
            raise ValidationError("verify_worker.last_beat: required")
        from datetime import datetime, timedelta, timezone
        host = str(record["host"])
        hosts = dict(self.load_verify_worker()["hosts"])
        hosts[host] = record
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=self._WORKER_PRUNE_SECONDS)
                  ).isoformat(timespec="microseconds")
        hosts = {h: r for h, r in hosts.items()
                 if h == host or str(r.get("last_beat") or "") >= cutoff}
        self._save_registry("verify_worker", {"hosts": hosts})

    def claim_verify_request(self, n: int, *, host: str) -> dict | None:
        """Atomically claim PR `n`'s `queued`/`waiting-for-base` verify_request
        for `host`: flip it to running (step `claimed`) only while the row is
        unchanged since it was read — a compare-and-swap on the saved_at
        write-stamp — so two workers against the shared store can never both
        run one PR. Returns the claimed section on success; None when the
        request is not claimable or another writer got there first."""
        row = self._prs.stamped(n)
        if row is None:
            return None
        rec, stamp = row
        req = rec.get("verify_request") or {}
        if req.get("status") not in ("queued", "waiting-for-base"):
            return None
        section: dict = {"status": "running", "step": "claimed", "host": host,
                         "started_at": storekit.now()}
        for field in ("queued_at", "source", "attempts"):
            if req.get(field) is not None:
                section[field] = req[field]
        head = (rec.get("meta") or {}).get("head_sha")
        storekit.stamp(rec, "verify_request", section, "against_head_sha", head)
        return section if self._prs.save_if(rec, stamp) else None

    def load_fix_worker(self) -> dict:
        """Autofix-worker heartbeats, one record per host (`{hosts: {<hostname>:
        {host, pid, last_beat, current_pr, autohunt}}}`) — which machines' fix
        workers exist, when each last beat, the PR each is acting on (None when
        idle), and whether each idle auto-hunt is enabled. Every worker merges
        its own record in each drain tick; any app reads the map to show which
        runners are online. An empty map means no fix worker has ever run
        against this store."""
        return self._load_registry("fix_worker", {"hosts": {}})

    def save_fix_worker(self, record: dict) -> None:
        """Merge one host's heartbeat record into the fix_worker registry,
        pruning entries stale past _WORKER_PRUNE_SECONDS. host and last_beat are
        required: a beat that names no machine or no time tells an app nothing
        about whether a runner is online. Two hosts' merges may race on the
        shared row; a lost beat is rewritten by the loser's next tick."""
        if not record.get("host"):
            raise ValidationError("fix_worker.host: required")
        if not record.get("last_beat"):
            raise ValidationError("fix_worker.last_beat: required")
        from datetime import datetime, timedelta, timezone
        host = str(record["host"])
        hosts = dict(self.load_fix_worker()["hosts"])
        hosts[host] = record
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=self._WORKER_PRUNE_SECONDS)
                  ).isoformat(timespec="microseconds")
        hosts = {h: r for h, r in hosts.items()
                 if h == host or str(r.get("last_beat") or "") >= cutoff}
        self._save_registry("fix_worker", {"hosts": hosts})

    def claim_fix_request(self, n: int, *, host: str,
                          statuses: tuple[str, ...] = ("queued",),
                          to_status: str = "running") -> dict | None:
        """Atomically claim PR `n`'s fix_request for `host`: flip it from one of
        `statuses` to `to_status` (step `claimed`) only while the row is
        unchanged since it was read — a compare-and-swap on the saved_at
        write-stamp — so two workers against the shared store can never both run
        one PR. The approved→pushing hand-off claims through the same path.
        Returns the claimed section on success; None when the request is not
        claimable or another writer got there first."""
        row = self._prs.stamped(n)
        if row is None:
            return None
        rec, stamp = row
        req = rec.get("fix_request") or {}
        if req.get("status") not in statuses:
            return None
        section: dict = {"status": to_status, "action": req.get("action"),
                         "step": "claimed", "host": host,
                         "started_at": storekit.now()}
        for field in ("queued_at", "source", "attempts", "base_sha", "result",
                      "guidance"):
            if req.get(field) is not None:
                section[field] = req[field]
        head = (rec.get("meta") or {}).get("head_sha")
        storekit.stamp(rec, "fix_request", section, "against_head_sha", head)
        return section if self._prs.save_if(rec, stamp) else None

    def claim_security_run(self, n: int, *, host: str, stale_after: float) -> bool:
        """Atomically claim PR `n`'s autohunt security review for `host` — a
        compare-and-swap on the row's write-stamp, so two idle workers never both
        spend an agent review on one PR. Returns whether the claim is held.

        A claim this host already holds is its own restart's leftover, retaken
        here. Another host's is honoured until `stale_after` seconds have passed,
        which is what frees a PR whose machine died mid-review; the window is
        sized well past a review so a slow one is never stolen."""
        row = self._prs.stamped(n)
        if row is None:
            return False
        rec, stamp = row
        run = rec.get("security_run") or {}
        holder = run.get("host")
        if (holder is not None and holder != host
                and not _older_than(run.get("started_at"), stale_after)):
            return False
        rec["security_run"] = {"host": host, "started_at": storekit.now()}
        return self._prs.save_if(rec, stamp)

    def release_security_run(self, n: int, *, host: str) -> None:
        """Drop `host`'s claim on PR `n`'s security review. A claim another host
        now holds is left alone: this one lost a reclaim race, and the winner's
        review is live."""
        row = self._prs.stamped(n)
        if row is None:
            return
        rec, stamp = row
        run = rec.get("security_run") or {}
        if run.get("host") not in (None, host):
            return
        rec["security_run"] = None
        self._prs.save_if(rec, stamp)

    def save_verify_base(self, record: dict) -> None:
        """Merge one machine's pin into the verify_base registry. host is
        required: the clone and image a pin names are local to the machine that
        built them, so a pin naming no machine tells another one nothing it can
        act on. base_sha and tier are required because together they name one
        built image (pr-verify-base:<sha12>-t<tier>), and a pin missing either
        names no image the sandbox could boot. baseline_failing and
        baseline_captured_at are required too: a pin without a captured baseline
        names no exclusion set, and the regression gate fails closed by refusing
        the pin."""
        if not record.get("host"):
            raise ValidationError("verify_base.host: required")
        if not record.get("base_sha"):
            raise ValidationError("verify_base.base_sha: required")
        if record.get("tier") not in VERIFY_TIERS:
            raise ValidationError(
                f"verify_base.tier: {record.get('tier')!r} not in {sorted(VERIFY_TIERS)}")
        baseline = record.get("baseline_failing")
        if not isinstance(baseline, list) or not all(isinstance(f, str) for f in baseline):
            raise ValidationError(
                "verify_base.baseline_failing: required list of test file paths")
        if not record.get("baseline_captured_at"):
            raise ValidationError("verify_base.baseline_captured_at: required")
        hosts = dict(self.load_verify_base_hosts())
        hosts[str(record["host"])] = record
        self._save_registry("verify_base", {"hosts": hosts})
