"""Fold one worker name into another across the store.

A worker stamps its name on every claim, pin, heartbeat, and kept worktree. A
machine that appears under two names (a hostname that followed the network,
then a configured TRIAGE_WORKER_ID) leaves half its records invisible to
itself. This renames the stamps on PR records (`fix_request`, `verify_request`,
`security_run`, and any nested `host` under them) and merges the three
host-keyed registries, keeping the newer record where both names hold one.

Dry run by default; `--live` applies through store_edit, which snapshots the
pre-images and writes a runs-ledger entry.

    uv run python -m pipeline.rename_worker OLD NEW [--live] [--store DIR]
"""
from __future__ import annotations

import argparse
import copy
import sys

from pipeline import store_edit
from pipeline.store import Store

SECTIONS = ("fix_request", "verify_request", "security_run")


def _rename_in(node: object, old: str, new: str) -> bool:
    """Rename every `host` key equal to `old` under `node`, in place. Returns
    whether anything changed."""
    changed = False
    if isinstance(node, dict):
        if node.get("host") == old:
            node["host"] = new
            changed = True
        for v in node.values():
            changed = _rename_in(v, old, new) or changed
    elif isinstance(node, list):
        for v in node:
            changed = _rename_in(v, old, new) or changed
    return changed


def rename_record(rec: dict, old: str, new: str) -> dict | None:
    """The PR record with `old` stamps under SECTIONS renamed to `new`, or None
    when it carries none."""
    out = copy.deepcopy(rec)
    changed = False
    for section in SECTIONS:
        if section in out:
            changed = _rename_in(out[section], old, new) or changed
    return out if changed else None


def _newer(a: dict, b: dict, key: str) -> dict:
    return b if str(b.get(key) or "") >= str(a.get(key) or "") else a


def plan_registries(store: Store, old: str, new: str) -> dict[str, dict]:
    """Each registry's merged `hosts` map after the rename, only for registries
    that hold a record under `old`."""
    plans: dict[str, dict] = {}
    pins: dict[str, dict] = {h: dict(p) for h, p in store.load_verify_base_hosts().items()}
    if old in pins:
        moved = pins.pop(old)
        moved["host"] = new
        pins[new] = _newer(pins[new], moved, "pinned_at") if new in pins else moved
        plans["verify_base"] = pins
    for name, loader in (("verify_worker", store.load_verify_worker),
                         ("fix_worker", store.load_fix_worker)):
        hosts = dict(loader()["hosts"])
        if old in hosts:
            moved = dict(hosts.pop(old))
            moved["host"] = new
            hosts[new] = _newer(dict(hosts[new]), moved, "last_beat") if new in hosts else moved
            plans[name] = hosts
    return plans


def apply_registries(store: Store, plans: dict[str, dict]) -> None:
    for name, hosts in plans.items():
        store._save_registry(name, {"hosts": hosts})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--live", action="store_true", help="apply (default: dry run)")
    ap.add_argument("--store", default=None, help="store directory (default: the configured store)")
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()

    report = store_edit.plan_edit(
        store, lambda rec: rename_record(rec, args.old, args.new), "prs")
    plans = plan_registries(store, args.old, args.new)
    mode = "live" if args.live else "dry run"
    print(f"{mode}: {args.old!r} -> {args.new!r}")
    print(f"  prs: {report.examined} examined, {len(report.changed)} to change: "
          f"{sorted(report.changed)[:40]}{' …' if len(report.changed) > 40 else ''}")
    for name, hosts in plans.items():
        print(f"  {name}: {args.old!r} folded into {args.new!r}; hosts now {sorted(hosts)}")
    if not plans and not report.changed:
        print("  nothing stamped with the old name")
        return 0
    if not args.live:
        print("re-run with --live to apply")
        return 0
    store_edit.apply_edit(store, report, "rename_worker")
    apply_registries(store, plans)
    print(f"applied; pre-images at {report.backup}" if report.backup else "applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
