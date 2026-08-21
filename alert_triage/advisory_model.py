"""Domain model over the advisory store: typed, invariant-owning mutation of
advisory records — each method mutates, stamps, and persists in one call.
Mirrors alert_triage/alert_model.py.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import storekit
from alert_triage.alert_freshness import UPDATED_BOUND

if TYPE_CHECKING:
    from alert_triage.advisory_store import AdvisoryStore


def _stamp(rec: dict, section: str, payload: dict) -> None:
    token_field = "against_updated_at" if section in UPDATED_BOUND else None
    token_value = rec["meta"]["updated_at"] if token_field else None
    storekit.stamp(rec, section, payload, token_field, token_value)


class Advisory:
    """A store-bound, auto-saving wrapper around one advisory record."""

    def __init__(self, store: AdvisoryStore | None, rec: dict):
        self._store = store
        self.rec = rec

    @property
    def id(self) -> int:
        return self.rec["id"]

    def section(self, name: str) -> dict | None:
        return self.rec.get(name)

    def _meta(self) -> dict:
        return self.rec.get("meta") or {}

    @property
    def ghsa_id(self) -> str:
        return self._meta().get("ghsa_id") or ""

    @property
    def state(self) -> str | None:
        return self._meta().get("state")

    @property
    def severity(self) -> str | None:
        return self._meta().get("severity")

    @property
    def summary(self) -> str | None:
        return self._meta().get("summary")

    @property
    def reporter(self) -> str | None:
        return self._meta().get("reporter")

    @property
    def cve_id(self) -> str | None:
        return self._meta().get("cve_id")

    @property
    def created_at(self) -> str | None:
        return self._meta().get("created_at")

    @property
    def updated_at(self) -> str | None:
        return self._meta().get("updated_at")

    @property
    def html_url(self) -> str | None:
        return self._meta().get("html_url")

    @property
    def verdict(self) -> str | None:
        return (self.rec.get("fix_scan") or {}).get("verdict")

    @property
    def duplicate_of(self) -> str | None:
        return (self.rec.get("fix_scan") or {}).get("duplicate_of")

    @property
    def fix_commit(self) -> str | None:
        return (self.rec.get("fix_scan") or {}).get("fix_commit")

    @property
    def fix_scan(self) -> dict | None:
        """The verdict without the freshness bookkeeping `_stamp` adds."""
        sec = self.rec.get("fix_scan")
        if sec is None:
            return None
        return {k: v for k, v in sec.items()
                if k not in ("checked_at", "against_updated_at")}

    @property
    def candidates(self) -> list[dict]:
        return (self.rec.get("links") or {}).get("candidates") or []

    def _persist(self) -> None:
        assert self._store is not None, "a store-less Advisory view is read-only"
        self._store.save_advisory(self.rec)

    def apply_facts(self, meta: dict, *, links: list[dict] | None = None) -> None:
        """Stamp the ingest-owned sections and persist once; `links` only when
        provided."""
        _stamp(self.rec, "meta", meta)
        if links is not None:
            _stamp(self.rec, "links", {"candidates": links})
        self._persist()

    def record_fix_scan(self, verdict: str, *, by: str, evidence: str | None = None,
                        duplicate_of: str | None = None, fix_commit: str | None = None,
                        links: list[dict] | None = None) -> None:
        """Record a find-fixed verdict; `links` merge into the stored candidates
        (deduped by kind+number). One persisted write."""
        if links:
            merged = list(self.candidates)
            seen = {(c.get("kind"), c.get("number")) for c in merged}
            for link in links:
                key = (link.get("kind"), link.get("number"))
                if key not in seen:
                    merged.append(link)
                    seen.add(key)
            _stamp(self.rec, "links", {"candidates": merged})
        scan: dict = {"verdict": verdict, "by": by}
        if duplicate_of:
            scan["duplicate_of"] = duplicate_of
        if fix_commit:
            scan["fix_commit"] = fix_commit
        if evidence:
            scan["evidence"] = evidence
        _stamp(self.rec, "fix_scan", scan)
        self._persist()
