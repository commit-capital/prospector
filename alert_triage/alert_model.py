"""Domain model over the alert store: typed, invariant-owning mutation of alert
records. These objects are the ONLY way alert facts should change — each method
mutates, stamps, and persists in one call, mirroring issue_triage/issue_model.py.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import storekit
from alert_triage.alert_freshness import UPDATED_BOUND

if TYPE_CHECKING:
    from alert_triage.alert_store import AlertStore


def _stamp(rec: dict, section: str, payload: dict, against_updated_at: str | None) -> None:
    """Stage one section, stamping checked_at and (for UPDATED_BOUND sections)
    the updated_at the fact was computed against. Does not persist — typed
    methods stage then persist once, so a multi-section mutation is a single
    write."""
    token_field = "against_updated_at" if section in UPDATED_BOUND else None
    token_value = (against_updated_at or rec["meta"]["updated_at"]) if token_field else None
    storekit.stamp(rec, section, payload, token_field, token_value)


class Alert:
    """A store-bound, auto-saving wrapper around one alert record."""

    def __init__(self, store: AlertStore | None, rec: dict):
        self._store = store
        self.rec = rec

    @property
    def raw(self) -> dict:
        return self.rec

    @property
    def id(self) -> int:
        return self.rec["id"]

    def section(self, name: str) -> dict | None:
        return self.rec.get(name)

    def _meta(self) -> dict:
        return self.rec.get("meta") or {}

    @property
    def source(self) -> str:
        return self._meta().get("source") or ""

    @property
    def number(self) -> int:
        return self._meta().get("number") or 0

    @property
    def state(self) -> str | None:
        return self._meta().get("state")

    @property
    def raw_state(self) -> str | None:
        return self._meta().get("raw_state")

    @property
    def severity(self) -> str | None:
        return self._meta().get("severity")

    @property
    def rule_id(self) -> str | None:
        return self._meta().get("rule_id")

    @property
    def package(self) -> str | None:
        return self._meta().get("package")

    @property
    def secret_type(self) -> str | None:
        return self._meta().get("secret_type")

    @property
    def path(self) -> str | None:
        return self._meta().get("path")

    @property
    def updated_at(self) -> str | None:
        return self._meta().get("updated_at")

    @property
    def created_at(self) -> str | None:
        return self._meta().get("created_at")

    @property
    def html_url(self) -> str | None:
        return self._meta().get("html_url")

    @property
    def title(self) -> str | None:
        """The display identity, by source: the code-scanning rule description
        (falling back to the rule id), the vulnerable package, or the secret
        type's display name."""
        m = self._meta()
        if self.source == "code-scanning":
            return m.get("rule_description") or m.get("rule_name") or m.get("rule_id")
        if self.source == "dependabot":
            return m.get("summary") or m.get("package")
        return m.get("secret_type_display_name") or m.get("secret_type")

    @property
    def verdict(self) -> str | None:
        return (self.rec.get("fix_scan") or {}).get("verdict")

    @property
    def action(self) -> str | None:
        return (self.rec.get("fix_scan") or {}).get("action")

    @property
    def fix_scan(self) -> dict | None:
        """The fixed-detector verdict, without the freshness bookkeeping
        (`checked_at`, `against_updated_at`) `_stamp` adds to the stored
        section."""
        sec = self.rec.get("fix_scan")
        if sec is None:
            return None
        return {k: v for k, v in sec.items()
                if k not in ("checked_at", "against_updated_at")}

    @property
    def candidates(self) -> list[dict]:
        return (self.rec.get("links") or {}).get("candidates") or []

    def _persist(self) -> None:
        assert self._store is not None, "a store-less Alert view is read-only"
        self._store.save_alert(self.rec)

    def set_meta(self, meta: dict) -> None:
        _stamp(self.rec, "meta", meta, None)
        self._persist()

    def apply_facts(self, meta: dict, *, links: list[dict] | None = None) -> None:
        """Stamp the ingest-owned fact sections and persist them in one save.
        `meta` is always set; `links` only when provided."""
        _stamp(self.rec, "meta", meta, None)
        if links is not None:
            _stamp(self.rec, "links", {"candidates": links}, None)
        self._persist()

    def record_links(self, candidates: list[dict]) -> None:
        _stamp(self.rec, "links", {"candidates": candidates}, None)
        self._persist()

    def record_fix_scan(self, verdict: str, *, action: str | None = None,
                        evidence: str | None = None, by: str = "agent",
                        links: list[dict] | None = None) -> None:
        """Record a fixed-pass verdict. `links` merge into the stored candidate
        list (deduped by kind+number) so agent-found fixers surface next to the
        deterministic ones. One persisted write."""
        if links:
            merged = list(self.candidates)
            seen = {(c.get("kind"), c.get("number")) for c in merged}
            for link in links:
                key = (link.get("kind"), link.get("number"))
                if key not in seen:
                    merged.append(link)
                    seen.add(key)
            _stamp(self.rec, "links", {"candidates": merged}, None)
        scan: dict = {"verdict": verdict, "by": by}
        if action:
            scan["action"] = action
        if evidence:
            scan["evidence"] = evidence
        _stamp(self.rec, "fix_scan", scan, None)
        self._persist()

    def record_live_state(self, state: str, raw_state: str | None = None) -> None:
        """Persist a GitHub-owned state change into the shared store right after
        the executor changes the alert upstream, so every operator sees the
        result at once."""
        meta = dict(self.section("meta") or {})
        meta["state"] = state
        if raw_state is not None:
            meta["raw_state"] = raw_state
        _stamp(self.rec, "meta", meta, None)
        self._persist()
