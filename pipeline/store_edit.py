"""Sanctioned bulk-edit path for the store: run a transform function over every
record of one table, dry-run by default, with a mandatory pre-image snapshot
and a runs-ledger entry when applied live. The one tool for destructive or
structural edits to the shared store — write the transform as a named function,
dry-run it, then apply with --live.

CLI:
    uv run python -m pipeline.store_edit mypkg.mymod:my_transform
    uv run python -m pipeline.store_edit mypkg.mymod:my_transform --live
    uv run python -m pipeline.store_edit ... --table clusters --store /dir

The transform receives a deep copy of one record dict at a time and returns:
    a dict  -> saved (validated) in place of the record
    None    -> record left untouched
    DELETE  -> record deleted
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline import storekit
from pipeline.store import DEFAULT_ROOT, Store
from pipeline.storekit import ValidationError

__all__ = ["DELETE", "EditReport", "ValidationError", "plan_edit", "apply_edit", "main"]


class _Delete:
    """Sentinel a transform returns to delete the record."""


DELETE = _Delete()

Transform = Callable[[dict], dict | None | _Delete]

BACKUP_DIR = DEFAULT_ROOT / "backups"


@dataclass
class EditReport:
    """What a transform would do (plan_edit) and, after apply_edit, what it did."""
    table: str
    examined: int = 0
    changed: dict[int, dict] = field(default_factory=dict)     # id -> new record
    deleted: list[int] = field(default_factory=list)
    pre_images: dict[int, dict] = field(default_factory=dict)  # id -> original record
    sections: dict[str, int] = field(default_factory=dict)     # top-level key -> records changed
    backup: Path | None = None
    applied: bool = False


def _collection(store: Store, table: str) -> storekit.Collection:
    if table == "prs":
        return store._prs
    if table == "clusters":
        return store._clusters
    raise ValueError(f"unknown table {table!r}")


def plan_edit(store: Store, transform: Transform, table: str) -> EditReport:
    """Run `transform` over every full record of `table` (no meta.body strip —
    a bulk edit must never save body-less records back) and collect, without
    applying, what would change. Each transformed record is validated here so a
    bad transform fails in the dry run, not mid-apply."""
    coll = _collection(store, table)
    report = EditReport(table=table)
    for i, view in sorted(coll.all().items()):
        report.examined += 1
        before = view.raw
        out = transform(copy.deepcopy(before))
        if isinstance(out, _Delete):
            report.deleted.append(i)
            report.pre_images[i] = before
            continue
        if out is None or out == before:
            continue
        coll.validate(out)
        if out.get(coll.id_field) != i:
            raise ValueError(f"transform changed {coll.id_field} of record {i}")
        report.changed[i] = out
        report.pre_images[i] = before
        for key in set(before) | set(out):
            if before.get(key) != out.get(key):
                report.sections[key] = report.sections.get(key, 0) + 1
    return report


def _write_backup(report: EditReport, name: str, backup_dir: Path) -> Path:
    """Write the pre-images of every to-be-changed/deleted record to a JSON file
    and return its path."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"store-edit-{name}-{ts}.json"
    payload = {"table": report.table, "transform": name,
               "records": [report.pre_images[i] for i in sorted(report.pre_images)]}
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    return path


def apply_edit(store: Store, report: EditReport, name: str,
               backup_dir: Path = BACKUP_DIR) -> EditReport:
    """Apply a planned edit: snapshot pre-images to `backup_dir`, save/delete
    the records, and append a runs-ledger entry. A report with nothing to do
    returns unchanged (no backup, no ledger entry). The store's write guard is
    consulted before the snapshot, so a refused edit leaves no backup file."""
    if not report.changed and not report.deleted:
        return report
    coll = _collection(store, report.table)
    storekit.assert_writable(store.engine)
    report.backup = _write_backup(report, name, backup_dir)
    with store.batch():
        if report.changed:
            coll.save_many(list(report.changed.values()))
        if report.deleted:
            coll.delete_many(report.deleted)
    store.append_run({
        "action": "store-edit", "transform": name, "table": report.table,
        "examined": report.examined, "changed": sorted(report.changed),
        "deleted": sorted(report.deleted), "backup": str(report.backup),
        "ts": storekit.now(),
    })
    report.applied = True
    return report


def _load_transform(spec: str) -> tuple[str, Transform]:
    if ":" not in spec:
        raise SystemExit("transform must be 'module.path:function'")
    mod_name, fn_name = spec.split(":", 1)
    fn = getattr(importlib.import_module(mod_name), fn_name)
    return fn_name, fn


def _print_report(report: EditReport) -> None:
    print(f"{report.table}: examined {report.examined}, "
          f"changed {len(report.changed)}, deleted {len(report.deleted)}")
    for section, count in sorted(report.sections.items()):
        print(f"  {section}: {count} record(s)")


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(
        prog="store_edit",
        description="Bulk-edit store records via a named transform; dry-run by default.")
    ap.add_argument("transform", help="module.path:function")
    ap.add_argument("--table", choices=("prs", "clusters"), default="prs")
    ap.add_argument("--live", action="store_true",
                    help="apply the edit (snapshots pre-images first)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the typed confirmation (scripted use)")
    ap.add_argument("--store", default="@env",
                    help="store directory, or @env for TRIAGE_STORE_URL (default)")
    ap.add_argument("--backup-dir", type=Path, default=BACKUP_DIR)
    args = ap.parse_args(argv)
    name, fn = _load_transform(args.transform)
    store = Store() if args.store == "@env" else Store(args.store)
    report = plan_edit(store, fn, args.table)
    _print_report(report)
    if not args.live:
        print("dry run — nothing written. Re-run with --live to apply.")
        return
    if not report.changed and not report.deleted:
        print("nothing to apply.")
        return
    if not args.yes:
        answer = input(f"type the transform name ({name!r}) to apply: ")
        if answer.strip() != name:
            raise SystemExit("confirmation mismatch — aborted")
    report = apply_edit(store, report, name, backup_dir=args.backup_dir)
    print(f"applied. backup: {report.backup}")


if __name__ == "__main__":
    main(sys.argv[1:])
