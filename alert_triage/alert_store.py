"""Canonical alert store — the ONLY accessor for the alerts table.

One SQL row per GitHub repository-security alert (code scanning / Dependabot /
secret scanning), keyed by the synthetic `alert_id(source, number)` so the three
per-source number spaces never collide. Fact sections (`links`, `fix_scan`) are
stamped checked_at + against_updated_at. Everything validates on write so reads
never need defensive parsing. Mirrors issue_triage/issue_store.py over the
shared storekit core.
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
    from alert_triage import alert_model

DEFAULT_ROOT = Path(__file__).resolve().parent / "store"

ALERT_SOURCES = {"code-scanning", "dependabot", "secret-scanning"}
ALERT_STATES = {"open", "dismissed", "fixed"}
ALERT_SEVERITIES = {"critical", "high", "medium", "low"}
FIX_SCAN_STATES = {"fixed", "likely-fixed", "not-fixed"}
ALERT_ACTIONS = {"dismiss-fixed", "needs-fix", "needs-human"}
ALERT_SECTIONS = ("meta", "links", "fix_scan")

# Per-source id offsets keeping the three alert-number spaces disjoint in one
# integer key space. Alert numbers are per-repo sequential, far below 10M.
SOURCE_ORDINAL = {"code-scanning": 1, "dependabot": 2, "secret-scanning": 3}


def alert_id(source: str, number: int) -> int:
    """The stable store key for one alert: `(source, number)` as one int."""
    return SOURCE_ORDINAL[source] * 10_000_000 + int(number)


def validate_alert(rec: dict) -> None:
    if not isinstance(rec.get("id"), int):
        raise ValidationError("id: required int")
    meta = rec.get("meta")
    if not isinstance(meta, dict):
        raise ValidationError("meta: required section")
    if meta.get("source") not in ALERT_SOURCES:
        raise ValidationError(
            f"meta.source: {meta.get('source')!r} not in {sorted(ALERT_SOURCES)}")
    if not isinstance(meta.get("number"), int):
        raise ValidationError("meta.number: required int")
    if rec["id"] != alert_id(meta["source"], meta["number"]):
        raise ValidationError("id: must equal alert_id(meta.source, meta.number)")
    if meta.get("state") not in ALERT_STATES:
        raise ValidationError(
            f"meta.state: {meta.get('state')!r} not in {sorted(ALERT_STATES)}")
    if meta.get("severity") not in ALERT_SEVERITIES:
        raise ValidationError(
            f"meta.severity: {meta.get('severity')!r} not in {sorted(ALERT_SEVERITIES)}")
    for field in ("updated_at", "html_url"):
        if not meta.get(field):
            raise ValidationError(f"meta.{field}: required")
    if "secret" in meta:
        raise ValidationError("meta.secret: secret values must never be stored")
    for key in rec:
        if key != "id" and key not in ALERT_SECTIONS:
            storekit.warn_unknown_section("alerts", key)
    fs = rec.get("fix_scan")
    if fs:
        if fs.get("verdict") not in FIX_SCAN_STATES:
            raise ValidationError(
                f"fix_scan.verdict: {fs.get('verdict')!r} not in {sorted(FIX_SCAN_STATES)}")
        if fs.get("action") is not None and fs["action"] not in ALERT_ACTIONS:
            raise ValidationError(
                f"fix_scan.action: {fs['action']!r} not in {sorted(ALERT_ACTIONS)}")
    ln = rec.get("links")
    if ln and not isinstance(ln.get("candidates"), list):
        raise ValidationError("links.candidates: required list")


class AlertStore:
    def __init__(self, root: Path | str | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self.engine = storekit.get_engine(storekit.resolve_url(root, DEFAULT_ROOT))
        with storekit.bound_session(self.engine):
            storekit.ensure_schema(self.engine)
            storekit.refresh_schema_guard(self.engine)
        self._alerts: Collection[alert_model.Alert] = Collection(
            self.engine, schema.alerts, "id", validate_alert, self._alert_view,
            schema.mirror_alert)

    def _alert_view(self, rec: dict) -> alert_model.Alert:
        from alert_triage import alert_model
        return alert_model.Alert(self, rec)

    @contextmanager
    def batch(self) -> Generator[None]:
        """Run a block of store operations over one reused connection. Each
        write still commits in its own transaction — durable if interrupted —
        so this is a pure speed-up for a loop of per-record reads/writes
        against a networked pooler."""
        with storekit.bound_session(self.engine):
            yield

    def load_alert(self, i: int) -> alert_model.Alert | None:
        return self._alerts.load(i)

    def save_alert(self, rec: dict) -> None:
        self._alerts.save(rec)

    def edit_alert(self, i: int) -> alert_model.Alert:
        """A typed, auto-saving handle for mutating alert `i`. Raises KeyError
        if the alert is not in the store."""
        return self._alerts.edit(i)

    def save_alerts_many(self, recs: list[dict]) -> None:
        """Persist many alert records in one validated bulk UPSERT."""
        self._alerts.save_many(recs)

    def all_alerts(self) -> dict[int, alert_model.Alert]:
        return self._alerts.all()

    def alerts_since(self, watermark: str | None
                     ) -> tuple[dict[int, alert_model.Alert], str | None]:
        return self._alerts.since(watermark)

    # -- Runs ledger --------------------------------------------------------
    def append_run(self, record: dict) -> None:
        """Append one ledger record, validated: it must parse as one of the
        ledger's two shapes (storekit.parse_run) or the append raises."""
        from sqlalchemy import insert
        storekit.parse_run(record)
        storekit.assert_writable(self.engine)
        with self.engine.begin() as conn:
            conn.execute(insert(schema.runs).values(
                kind="alert", data=record, ts=record.get("ts") or storekit.now()))

    def runs(self) -> list[storekit.RunRecord]:
        """The alert ledger's records, typed (PhaseRun | StoreEdit), oldest first."""
        from sqlalchemy import select
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(schema.runs.c.data)
                .where(schema.runs.c.kind == "alert")
                .order_by(schema.runs.c.rowid)).all()
        return [storekit.parse_run(r[0]) for r in rows]
