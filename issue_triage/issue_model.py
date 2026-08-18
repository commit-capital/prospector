"""Domain model over the issue store: typed, invariant-owning mutation of issue
and issue-cluster records. These objects are the ONLY way disposition, curation,
and membership state should change — each method mutates, stamps, and persists in
one call, mirroring pipeline/model.py for PRs.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import storekit
from issue_triage.issue_freshness import UPDATED_BOUND, is_current

if TYPE_CHECKING:
    from issue_triage.issue_store import IssueStore


def _stamp(rec: dict, section: str, payload: dict, against_updated_at: str | None) -> None:
    """Stage one section, stamping checked_at and (for UPDATED_BOUND sections) the
    updated_at the fact was computed against. Does not persist — typed methods
    stage then persist once, so a multi-section mutation is a single write."""
    token_field = "against_updated_at" if section in UPDATED_BOUND else None
    token_value = (against_updated_at or rec["meta"]["updated_at"]) if token_field else None
    storekit.stamp(rec, section, payload, token_field, token_value)


class Issue:
    """A store-bound, auto-saving wrapper around one issue record."""

    def __init__(self, store: IssueStore | None, rec: dict):
        self._store = store
        self.rec = rec

    @property
    def raw(self) -> dict:
        return self.rec

    @property
    def number(self) -> int:
        return self.rec["issue"]

    def section(self, name: str) -> dict | None:
        return self.rec.get(name)

    def _meta(self) -> dict:
        return self.rec.get("meta") or {}

    @property
    def title(self) -> str | None:
        return self._meta().get("title")

    @property
    def body(self) -> str | None:
        return self._meta().get("body")

    @property
    def state(self) -> str | None:
        return self._meta().get("state")

    @property
    def state_reason(self) -> str | None:
        return self._meta().get("state_reason")

    @property
    def author(self) -> str | None:
        return self._meta().get("author")

    @property
    def labels(self) -> list[str]:
        return self._meta().get("labels") or []

    @property
    def updated_at(self) -> str | None:
        return self._meta().get("updated_at")

    @property
    def created_at(self) -> str | None:
        return self._meta().get("created_at")

    @property
    def comments(self) -> int:
        return self._meta().get("comments") or 0

    @property
    def reactions_total(self) -> int:
        return self._meta().get("reactions_total") or 0

    @property
    def thumbs_up(self) -> int:
        return self._meta().get("thumbs_up") or 0

    @property
    def url(self) -> str | None:
        return self._meta().get("url")

    @property
    def subsystem(self) -> str | None:
        return (self.rec.get("summary") or {}).get("subsystem")

    @property
    def identifiers(self) -> list[str]:
        return (self.rec.get("summary") or {}).get("identifiers") or []

    @property
    def repro_grade(self) -> str | None:
        return (self.rec.get("repro") or {}).get("grade")

    @property
    def repro_score(self) -> int | None:
        return (self.rec.get("repro") or {}).get("score")

    def _fixed_now(self) -> bool:
        fs = self.rec.get("fix_scan") or {}
        return (fs.get("status") == "fixed" and fs.get("fixed_by") is not None
                and is_current(self, "fix_scan"))

    @property
    def disposition(self) -> str | None:
        """close-fixed while the fix scan cites a merged fixer, else the stored
        ANALYZE verdict. Derived, so a staled fact heals the read in place."""
        if self._fixed_now():
            return "close-fixed"
        return (self.rec.get("analysis") or {}).get("disposition")

    @property
    def canonical(self) -> int | None:
        return (self.rec.get("analysis") or {}).get("canonical")

    @property
    def fixed_by(self) -> int | None:
        return (self.rec.get("fix_scan") or {}).get("fixed_by")

    @property
    def rationale(self) -> str | None:
        if self._fixed_now():
            return (self.rec.get("fix_scan") or {}).get("rationale")
        return (self.rec.get("analysis") or {}).get("rationale")

    @property
    def gist(self) -> str | None:
        """Plain-language restatement of what the issue is."""
        if self._fixed_now():
            return (self.rec.get("fix_scan") or {}).get("gist")
        return (self.rec.get("analysis") or {}).get("gist")

    @property
    def asks(self) -> list | None:
        return (self.rec.get("analysis") or {}).get("asks")

    @property
    def candidate_prs(self) -> list[dict]:
        return (self.rec.get("links") or {}).get("candidates") or []

    @property
    def cluster_id(self) -> int | None:
        return (self.rec.get("cluster") or {}).get("id")

    @property
    def resolution(self) -> dict | None:
        return self.rec.get("resolution")

    @property
    def fix_scan(self) -> dict | None:
        """The already-fixed-detector verdict, without the freshness bookkeeping
        (`checked_at`, `against_updated_at`) `_stamp` adds to the stored section."""
        sec = self.rec.get("fix_scan")
        if sec is None:
            return None
        return {k: v for k, v in sec.items() if k not in ("checked_at", "against_updated_at")}

    def _persist(self) -> None:
        assert self._store is not None, "a store-less Issue view is read-only"
        self._store.save_issue(self.rec)

    def set_meta(self, meta: dict) -> None:
        _stamp(self.rec, "meta", meta, None)
        self._persist()

    def apply_facts(self, meta: dict, *, summary: dict | None = None,
                    repro: dict | None = None, links: list | None = None) -> None:
        """Stamp the ingest-owned fact sections and persist them in one save.
        `meta` is always set; `summary`, `repro`, and `links` are set only when
        provided. A single write lands an issue's whole ingest atomically
        (mirrors Pr.apply_facts)."""
        _stamp(self.rec, "meta", meta, None)
        if summary is not None:
            _stamp(self.rec, "summary", summary, None)
        if repro is not None:
            _stamp(self.rec, "repro", repro, None)
        if links is not None:
            _stamp(self.rec, "links", {"candidates": links}, None)
        self._persist()

    def set_summary(self, subsystem: str | None, identifiers: list[str], *,
                    updated_at: str | None = None) -> None:
        _stamp(self.rec, "summary", {"subsystem": subsystem, "identifiers": identifiers}, updated_at)
        self._persist()

    def set_repro(self, repro: dict, *, updated_at: str | None = None) -> None:
        _stamp(self.rec, "repro", repro, updated_at)
        self._persist()

    def set_links(self, candidates: list[dict]) -> None:
        _stamp(self.rec, "links", {"candidates": candidates}, None)
        self._persist()

    def route_to(self, disposition: str, rationale: str, *, canonical: int | None = None,
                 fixed_by: int | None = None, asks: list | None = None,
                 gist: str | None = None, updated_at: str | None = None) -> None:
        """Set this issue's analysis disposition. `gist` is a plain-language
        restatement of what the issue is. A close-dup must carry a canonical and a
        close-fixed a fixed_by (both enforced on save)."""
        section: dict = {"disposition": disposition, "rationale": rationale}
        if canonical is not None:
            section["canonical"] = int(canonical)
        if fixed_by is not None:
            section["fixed_by"] = int(fixed_by)
        cleaned = [a for a in (asks or []) if a]
        if cleaned:
            section["asks"] = cleaned
        if gist:
            section["gist"] = gist
        _stamp(self.rec, "analysis", section, updated_at)
        self._persist()

    def record_fixed(self, fixed_by: int, *, rationale: str, gist: str | None = None,
                     upstream_date: str | None = None, title: str = "",
                     updated_at: str | None = None) -> None:
        """Record the fixer in links tagged `how='fix-found'` (a
        detector-discovered, non-explicit fixer) plus the fix_scan evidence. The
        close-fixed route follows from it on read. One persisted write."""
        candidates = list((self.rec.get("links") or {}).get("candidates") or [])
        if not any(c.get("pr") == int(fixed_by) and c.get("how") == "fix-found"
                   for c in candidates):
            candidates.append({"pr": int(fixed_by), "how": "fix-found", "title": title})
        _stamp(self.rec, "links", {"candidates": candidates}, None)
        scan: dict = {"status": "fixed", "fixed_by": int(fixed_by)}
        if upstream_date:
            scan["upstream_date"] = upstream_date
        if gist:
            scan["gist"] = gist
        if rationale:
            scan["rationale"] = rationale
        _stamp(self.rec, "fix_scan", scan, updated_at)
        self._persist()

    def record_fix_scan(self, status: str, *, gist: str | None = None,
                        rationale: str | None = None,
                        updated_at: str | None = None) -> None:
        """Record a non-closing fix-scan outcome (likely-fixed / not-fixed) without
        touching the disposition — drives the tier-2 review lane and re-run skip."""
        scan: dict = {"status": status}
        if gist:
            scan["gist"] = gist
        if rationale:
            scan["rationale"] = rationale
        _stamp(self.rec, "fix_scan", scan, updated_at)
        self._persist()

    def clear_cluster(self) -> None:
        self.rec.pop("cluster", None)
        self._persist()

    def stage_cluster(self, cid: int) -> None:
        """Stage the cluster backref in memory without persisting — the caller
        saves many staged issues in one bulk write (IssueStore.save_issues_many)."""
        _stamp(self.rec, "cluster", {"id": int(cid)}, None)

    def record_live_state(self, state: str, state_reason: str | None = None) -> None:
        """Persist GitHub-owned open/closed state and its reason into the shared
        store. The executor calls this right after it changes an issue upstream, so
        every operator sees the result at once — without waiting for the next INGEST
        closure-reconciliation sweep. The issue-side analog of Pr.record_live_state."""
        meta = dict(self.section("meta") or {})
        meta["state"] = state
        meta["state_reason"] = state_reason
        _stamp(self.rec, "meta", meta, None)
        self._persist()


class IssueCluster:
    """A store-bound, auto-saving wrapper around one issue-cluster record. Owns the
    two-way membership link (cluster.members <-> each member's issue.cluster.id)."""

    def __init__(self, store: IssueStore, rec: dict):
        self._store = store
        self.rec = rec

    @property
    def raw(self) -> dict:
        return self.rec

    @property
    def id(self) -> int:
        return self.rec["id"]

    @property
    def title(self) -> str | None:
        return self.rec.get("title")

    @property
    def subsystem(self) -> str | None:
        return self.rec.get("subsystem")

    @property
    def members(self) -> list[int]:
        return self.rec.get("members") or []

    @property
    def canonical(self) -> int | None:
        return self.rec.get("canonical")

    @property
    def pain(self) -> float | None:
        return self.rec.get("pain")

    @property
    def needs_review(self) -> bool:
        return bool(self.rec.get("needs_review"))

    @property
    def curation(self) -> dict | None:
        return self.rec.get("curation")

    def _persist(self) -> None:
        self.rec["checked_at"] = storekit.now()
        self._store.save_issue_cluster(self.rec)

    def add_member(self, n: int) -> None:
        """Add issue `n` to this cluster and point its backref here. Idempotent."""
        n = int(n)
        if n not in self.rec["members"]:
            self.rec["members"] = sorted(set(self.rec["members"]) | {n})
            self._persist()
        iss = self._store.edit_issue(n)
        _stamp(iss.rec, "cluster", {"id": self.id}, None)
        iss._persist()

    def remove_member(self, n: int) -> None:
        """Remove issue `n` and clear its backref, but only if the backref still
        points here (the issue may have moved elsewhere)."""
        n = int(n)
        if n in self.rec["members"]:
            self.rec["members"] = [x for x in self.rec["members"] if x != n]
            self._persist()
        iss = self._store.load_issue(n)
        if iss and iss.cluster_id == self.id:
            iss.raw.pop("cluster", None)
            self._store.save_issue(iss)

    def set_members(self, members: list[int]) -> None:
        """Reconcile membership to exactly `members`: add the missing (wiring each
        backref), remove the departed (clearing the backref iff it still points
        here)."""
        want = {int(m) for m in members}
        current = {int(x) for x in self.rec.get("members") or []}
        for n in sorted(want - current):
            self.add_member(n)
        for n in sorted(current - want):
            self.remove_member(n)

    def set_pain(self, pain: float) -> None:
        self.rec["pain"] = pain
        self._persist()

    def set_needs_review(self, flag: bool) -> None:
        self.rec["needs_review"] = bool(flag)
        self._persist()

    def record_curation(self, curation: dict) -> None:
        """Store the /diagnose-issue-cluster verdict. If it names a canonical, mirror
        it to the cluster's canonical field (the survivor issue)."""
        self.rec["curation"] = curation
        if curation.get("canonical") is not None:
            self.rec["canonical"] = int(curation["canonical"])
        self._persist()
