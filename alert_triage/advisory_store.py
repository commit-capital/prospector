"""Canonical advisory store — the ONLY accessor for the advisories table.

One SQL row per GitHub repository security advisory, keyed by
`advisory_id(ghsa_id)`: the twelve GHSA symbols read as one base-21 integer, a
bijection, so a key decodes back to its id. Fact sections (`links`,
`fix_scan`) are stamped checked_at + against_updated_at. Validates on write.
Mirrors alert_triage/alert_store.py over the shared storekit core.
"""
from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import schema
from pipeline import storekit
from pipeline.storekit import Collection, ValidationError

if TYPE_CHECKING:
    from alert_triage import advisory_model

DEFAULT_ROOT = Path(__file__).resolve().parent / "store"

# GitHub's fixed GHSA symbol set, in value order.
GHSA_ALPHABET = "23456789cfghjkmpqrvwx"
_GHSA = re.compile(rf"^GHSA(?:-[{GHSA_ALPHABET}]{{4}}){{3}}$")
_VALUE = {ch: i for i, ch in enumerate(GHSA_ALPHABET)}

ADVISORY_STATES = {"triage", "draft", "published", "closed"}
ADVISORY_SEVERITIES = {"critical", "high", "medium", "low", "unknown"}
ADVISORY_VERDICTS = {"fixed", "likely-fixed", "not-fixed", "duplicate"}
VERDICT_BY = {"deterministic", "agent"}
ADVISORY_SECTIONS = ("meta", "links", "fix_scan")


def advisory_id(ghsa: str) -> int:
    """The store key for a GHSA id. Raises ValueError on a malformed id."""
    if not _GHSA.match(ghsa or ""):
        raise ValueError(f"not a GHSA id: {ghsa!r}")
    n = 0
    for ch in ghsa[5:].replace("-", ""):
        n = n * 21 + _VALUE[ch]
    return n


def ghsa_of(i: int) -> str:
    digits = []
    for _ in range(12):
        i, r = divmod(i, 21)
        digits.append(GHSA_ALPHABET[r])
    body = "".join(reversed(digits))
    return f"GHSA-{body[:4]}-{body[4:8]}-{body[8:]}"


def validate_advisory(rec: dict) -> None:
    if not isinstance(rec.get("id"), int):
        raise ValidationError("id: required int")
    meta = rec.get("meta")
    if not isinstance(meta, dict):
        raise ValidationError("meta: required section")
    try:
        expected = advisory_id(meta.get("ghsa_id") or "")
    except ValueError as e:
        raise ValidationError(f"meta.ghsa_id: {e}") from e
    if rec["id"] != expected:
        raise ValidationError("id: must equal advisory_id(meta.ghsa_id)")
    if meta.get("state") not in ADVISORY_STATES:
        raise ValidationError(
            f"meta.state: {meta.get('state')!r} not in {sorted(ADVISORY_STATES)}")
    if meta.get("severity") not in ADVISORY_SEVERITIES:
        raise ValidationError(
            f"meta.severity: {meta.get('severity')!r} not in {sorted(ADVISORY_SEVERITIES)}")
    for field in ("updated_at", "html_url"):
        if not meta.get(field):
            raise ValidationError(f"meta.{field}: required")
    for key in rec:
        if key != "id" and key not in ADVISORY_SECTIONS:
            storekit.warn_unknown_section("advisories", key)
    fs = rec.get("fix_scan")
    if fs:
        verdict = fs.get("verdict")
        if verdict not in ADVISORY_VERDICTS:
            raise ValidationError(
                f"fix_scan.verdict: {verdict!r} not in {sorted(ADVISORY_VERDICTS)}")
        if fs.get("by") not in VERDICT_BY:
            raise ValidationError(f"fix_scan.by: {fs.get('by')!r} not in {sorted(VERDICT_BY)}")
        target = fs.get("duplicate_of")
        if verdict == "duplicate":
            if not target or not _GHSA.match(target):
                raise ValidationError("fix_scan.duplicate_of: a GHSA id is required "
                                      "for a duplicate verdict")
        elif target:
            raise ValidationError("fix_scan.duplicate_of: only a duplicate verdict names one")
        commit = fs.get("fix_commit")
        if verdict == "fixed" and not (isinstance(commit, str) and len(commit) >= 7):
            raise ValidationError("fix_scan.fix_commit: a fixed verdict names the commit")
    ln = rec.get("links")
    if ln and not isinstance(ln.get("candidates"), list):
        raise ValidationError("links.candidates: required list")


class AdvisoryStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = storekit.get_engine(storekit.resolve_url(root, DEFAULT_ROOT))
        with storekit.bound_session(self.engine):
            storekit.ensure_schema(self.engine)
            storekit.refresh_schema_guard(self.engine)
        self._advisories: Collection[advisory_model.Advisory] = Collection(
            self.engine, schema.advisories, "id", validate_advisory, self._view,
            schema.mirror_advisory)

    def _view(self, rec: dict) -> advisory_model.Advisory:
        from alert_triage import advisory_model
        return advisory_model.Advisory(self, rec)

    @contextmanager
    def batch(self) -> Generator[None]:
        with storekit.bound_session(self.engine):
            yield

    def load_advisory(self, i: int) -> advisory_model.Advisory | None:
        return self._advisories.load(i)

    def save_advisory(self, rec: dict) -> None:
        self._advisories.save(rec)

    def edit_advisory(self, i: int) -> advisory_model.Advisory:
        """A typed, auto-saving handle; KeyError when `i` is not stored."""
        return self._advisories.edit(i)

    def all_advisories(self) -> dict[int, advisory_model.Advisory]:
        return self._advisories.all()

    def advisories_since(self, watermark: str | None
                         ) -> tuple[dict[int, advisory_model.Advisory], str | None]:
        return self._advisories.since(watermark)

    def append_run(self, record: dict) -> None:
        """Append to the alert family's runs ledger (kind "alert"), validated."""
        from sqlalchemy import insert
        storekit.parse_run(record)
        storekit.assert_writable(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(schema.runs).values(
                kind="alert", data=record, ts=record.get("ts") or storekit.now()))
