"""Reversible JSON↔DB migration for the issue store. `import_issue_store` reads
issues/, issue_clusters/, and runs.jsonl into an IssueStore; `dump_issue_store`
writes an IssueStore back to that layout. Idempotent: re-importing upserts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pipeline import storekit
from issue_triage.issue_store import IssueStore


def import_issue_store(src: Path | str, store: IssueStore) -> dict[str, int]:
    src = Path(src)
    counts: dict[str, int] = {"issues": 0, "issue_clusters": 0, "runs": 0}
    issues_dir = src / "issues"
    if issues_dir.exists():
        recs = [json.loads(p.read_text()) for p in sorted(issues_dir.glob("*.json"))]
        store._issues.save_many(recs)
        counts["issues"] = len(recs)
    cl_dir = src / "issue_clusters"
    if cl_dir.exists():
        recs = [json.loads(p.read_text()) for p in sorted(cl_dir.glob("*.json"))]
        store._clusters.save_many(recs)
        counts["issue_clusters"] = len(recs)
    runs = src / "runs.jsonl"
    if runs.exists():
        for line in runs.read_text().splitlines():
            if line.strip():
                store.append_run(json.loads(line))
                counts["runs"] += 1
    return counts


def dump_issue_store(store: IssueStore, dst: Path | str) -> None:
    dst = Path(dst)
    (dst / "issues").mkdir(parents=True, exist_ok=True)
    (dst / "issue_clusters").mkdir(parents=True, exist_ok=True)
    for n, issue in store.all_issues().items():
        storekit.atomic_write(dst / "issues" / f"{n}.json", issue.raw)
    for cid, cl in store.all_issue_clusters().items():
        storekit.atomic_write(dst / "issue_clusters" / f"{cid:03d}.json", cl.raw)
    with (dst / "runs.jsonl").open("w") as f:
        for rec in store.runs():
            f.write(json.dumps(rec.raw, sort_keys=True) + "\n")


def dest_store(target: str) -> IssueStore:
    """The IssueStore a migration targets: the env-configured shared store when
    `target` is "@env" (TRIAGE_STORE_URL), otherwise a local SQLite store under
    the given directory path."""
    return IssueStore() if target == "@env" else IssueStore(target)


def main(argv: list[str]) -> None:
    if len(argv) != 3 or argv[0] not in ("import", "dump"):
        print("usage: issue_store_migrate.py import <src_dir> <db_root|@env>\n"
              "       issue_store_migrate.py dump   <db_root|@env> <dst_dir>\n"
              "  @env targets the store configured by TRIAGE_STORE_URL.",
              file=sys.stderr)
        raise SystemExit(2)
    cmd, a, b = argv
    if cmd == "import":
        print(import_issue_store(a, dest_store(b)))
    else:
        dump_issue_store(dest_store(a), b)


if __name__ == "__main__":
    main(sys.argv[1:])
