"""One-time, reversible migration between the JSON-files store layout and the SQL
backing store. `import_pr_store` reads a directory tree (prs/, clusters/,
runs.jsonl, threats.json, action_items.json) into a Store; `dump_pr_store` writes
a Store back out to that layout (the escape hatch). Idempotent: re-importing
upserts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pipeline import storekit
from pipeline.store import Store


def import_pr_store(src: Path | str, store: Store) -> dict[str, int]:
    src = Path(src)
    counts = {"prs": 0, "clusters": 0, "runs": 0}
    pr_dir = src / "prs"
    if pr_dir.exists():
        recs = [json.loads(p.read_text()) for p in sorted(pr_dir.glob("*.json"))]
        store._prs.save_many(recs)
        counts["prs"] = len(recs)
    cl_dir = src / "clusters"
    if cl_dir.exists():
        recs = [json.loads(p.read_text()) for p in sorted(cl_dir.glob("*.json"))]
        store._clusters.save_many(recs)
        counts["clusters"] = len(recs)
    runs = src / "runs.jsonl"
    if runs.exists():
        for line in runs.read_text().splitlines():
            if line.strip():
                store.append_run(json.loads(line))
                counts["runs"] += 1
    threats = src / "threats.json"
    if threats.exists():
        store.save_threats(json.loads(threats.read_text()))
    items = src / "action_items.json"
    if items.exists():
        store.save_action_items(json.loads(items.read_text()))
    return counts


def dump_pr_store(store: Store, dst: Path | str) -> None:
    dst = Path(dst)
    (dst / "prs").mkdir(parents=True, exist_ok=True)
    (dst / "clusters").mkdir(parents=True, exist_ok=True)
    for n, pr in store.all_prs().items():
        storekit.atomic_write(dst / "prs" / f"{n}.json", pr.raw)
    for cid, cl in store.all_clusters().items():
        storekit.atomic_write(dst / "clusters" / f"{cid:03d}.json", cl.raw)
    with (dst / "runs.jsonl").open("w") as f:
        for rec in store.runs():
            f.write(json.dumps(rec.raw, sort_keys=True) + "\n")
    storekit.atomic_write(dst / "threats.json", store.load_threats())
    storekit.atomic_write(dst / "action_items.json", store.load_action_items())


def dest_store(target: str) -> Store:
    """The Store a migration targets: the env-configured shared store when
    `target` is "@env" (TRIAGE_STORE_URL), otherwise a local SQLite store under
    the given directory path."""
    return Store() if target == "@env" else Store(target)


def main(argv: list[str]) -> None:
    if len(argv) != 3 or argv[0] not in ("import", "dump"):
        print("usage: store_migrate.py import <src_dir> <db_root|@env>\n"
              "       store_migrate.py dump   <db_root|@env> <dst_dir>\n"
              "  @env targets the store configured by TRIAGE_STORE_URL.",
              file=sys.stderr)
        raise SystemExit(2)
    cmd, a, b = argv
    if cmd == "import":
        print(import_pr_store(a, dest_store(b)))
    else:
        dump_pr_store(dest_store(a), b)


if __name__ == "__main__":
    main(sys.argv[1:])
