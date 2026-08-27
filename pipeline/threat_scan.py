"""Phase 0.5 — THREAT SCAN (deterministic, cheap, run after INGEST).

Scans each PR's cached diff for the attack signatures in threats.py and checks
its author against the durable actor blocklist, then stamps a `threat` section
on the record. A 'malicious' verdict makes the PR un-mergeable forever via
gates.pr_clean (fail-closed, no staleness exemption) and, on first detection,
adds the author to the blocklist and logs the incident.

Repository maintainers — the profile's trusted_authors — are never flagged:
their PRs always stamp `clear`, and neither the attack signatures nor the
blocklist apply to them (a secret-leak still raises a rotate-secret action
item; the leaked key must rotate no matter who pushed it).

The sweep visits open PRs only: a closed PR's verdict gates nothing, and the
incident of one caught while open already sits in the durable registry.
`--only` names PRs verbatim, open or closed, so an operator can rescan a
specific PR while investigating an incident.

Diffs live in the machine-local cache (diff_cache.py). Before scanning, the
run fetches the current-head diff of every open PR that has none cached
(diff_cache.fetch_diff: bounded, read-only gh reads), so scan coverage never
depends on a CLUSTER run having populated this machine's cache. Genuine
dependency bumps from the profile's automation authors are
exempt: their diffs are deliberately never fetched or scanned
(gates.is_dependabot_bump). --no-fetch skips the fetch step and scans only
what is already cached. Either way, PRs left without a diff are reported as
`uncached` in the run ledger and left unscanned.

The scan is idempotent: re-running re-derives the same verdicts and the
registry merges rather than duplicates. The scan loop runs on one bound store
connection, and a PR whose stored stamp already carries the derived verdict at
its current head is left untouched — a re-run over the corpus reads and writes
only the records whose verdict or head changed.

Usage:
  uv run python threat_scan.py [--store DIR] [--only N[,N...]] [--no-fetch]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import actions
from pipeline import diff_cache
from pipeline import gates
from pipeline import profile
from pipeline import threats
from pipeline.store import Store
from pipeline.wire import DiffManifestItem

if TYPE_CHECKING:
    from pipeline.model import Pr


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def fetch_missing_diffs(prs: dict[int, Pr], diffs_dir: Path, workers: int = 8,
                        store: Store | None = None) -> tuple[dict[str, int], set[int]]:
    """Fetch the current-head diff of every open PR in `prs` that has none
    cached — parallel, read-only against GitHub (diff_cache.fetch_diff), with
    heads already in the shared store's diff cache pulled in bulk first and
    fresh fetches pushed back (diff_cache.fetch_diffs). Genuine dependency
    bumps from the profile's automation authors are left unfetched; their
    diffs are out of scanning scope (gates.is_dependabot_bump — the
    change-shape check needs the paths, so for an uncached automation PR it
    reads the per-file listing). Returns the fetch counters and the set of
    exempt PR numbers."""
    bots = profile.active().automation_bots
    manifest: list[DiffManifestItem] = []
    exempt: set[int] = set()
    for n, rec in sorted(prs.items()):
        head = rec.head_sha
        if rec.state != "open" or not head or (diffs_dir / f"{head}.diff").exists():
            continue
        if rec.author in bots and gates.is_dependabot_bump(
                rec.author, diff_cache.changed_paths(n, head, diffs_dir)):
            exempt.add(n)
            continue
        manifest.append(DiffManifestItem.for_pr(n, rec, diffs_dir))
    fetched, failed = diff_cache.fetch_diffs(manifest, workers=workers, store=store,
                                             diffs_dir=diffs_dir)
    return {"fetched": fetched, "fetch_failed": failed, "bump_exempt": len(exempt)}, exempt


def already_stamped(rec: Pr, result: dict) -> bool:
    """Whether `rec`'s stored threat stamp already carries `result` at the
    record's current head — verdict, signatures, and detail all equal. Such a
    record needs no write: threat currency is a head comparison, so re-stamping
    the same verdict at the same head changes nothing a reader can observe."""
    sec = rec.section("threat")
    if not sec or sec.get("against_head_sha") != rec.head_sha:
        return False
    return all(sec.get(k) == result.get(k) for k in ("verdict", "signatures", "detail"))


def scan_record(rec: Pr, registry: dict, diffs_dir: Path = diff_cache.DIFFS) -> dict | None:
    """Compute the threat verdict for one PR record. Returns the `threat`
    payload (unstamped) or None if the diff isn't cached and the author is
    clean (nothing to assert). A blocked author yields a verdict even with no
    diff, since blocklist membership alone is disqualifying.

    A repository maintainer (the profile's trusted_authors) is never flagged:
    the verdict is always `clear` and neither the attack signatures nor the
    actor blocklist apply. A secret-leak signature is kept on the clear stamp —
    a leaked credential must rotate no matter who pushed it, and the
    rotate-secret action item keys off the signature."""
    author = rec.author
    head = rec.head_sha
    diff_path = diffs_dir / f"{head}.diff"

    result = {"verdict": "clear", "signatures": [], "detail": {}}
    if diff_path.exists():
        sig = rec.signals or {}
        ds = sig.get("diffstat", {}) or {}
        result = threats.scan_diff(
            diff_path.read_text(errors="replace"),
            additions=ds.get("additions"), deletions=ds.get("deletions"),
        )

    if author in profile.active().trusted_authors:
        kept = [s for s in result["signatures"] if s == "secret-leak"]
        result = {"verdict": "clear", "signatures": kept,
                  "detail": {k: v for k, v in result["detail"].items()
                             if k == "secret-leak"}}
        if not kept and not diff_path.exists():
            return None  # nothing observed and nothing on file — don't stamp
        return result

    blocked = threats.is_blocked_actor(registry, author)
    if blocked and result["verdict"] != "malicious":
        result = {"verdict": "malicious",
                  "signatures": sorted(set(result["signatures"]) | {"blocked-actor"}),
                  "detail": {**result["detail"], "blocked-actor": author}}

    if result["verdict"] == "clear" and not diff_path.exists():
        return None  # nothing observed and nothing on file — don't stamp
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=None, help="store root override (tests)")
    ap.add_argument("--only", default=None, help="comma-separated PR numbers")
    ap.add_argument("--diffs", default=None, help="diff cache dir override (tests)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="scan only already-cached diffs (no gh reads)")
    args = ap.parse_args(argv)

    store = Store(args.store) if args.store else Store()
    diffs_dir = Path(args.diffs) if args.diffs else diff_cache.DIFFS
    registry = store.load_threats()
    action_items = store.load_action_items()
    today = _today()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    prs = store.all_prs()
    if args.only:
        want = {int(x) for x in args.only.split(",")}
        prs = {n: r for n, r in prs.items() if n in want}
    else:
        prs = {n: r for n, r in prs.items() if r.state == "open"}

    fetch_stats: dict[str, int] = {}
    exempt: set[int] = set()
    if not args.no_fetch:
        fetch_stats, exempt = fetch_missing_diffs(prs, diffs_dir, store=store)
        print(f"fetch: {fetch_stats}", flush=True)

    malicious: list[int] = []
    suspicious: list[int] = []
    uncached = 0
    restamped = 0
    # one bound connection for the whole loop: each per-PR read or write is a
    # single round-trip on it, with no per-statement connect handshake
    with store.batch():
        for n, rec in sorted(prs.items()):
            result = scan_record(rec, registry, diffs_dir=diffs_dir)
            if result is None:
                head = rec.head_sha
                if n not in exempt and not (diffs_dir / f"{head}.diff").exists():
                    uncached += 1
                continue
            if not already_stamped(rec, result):
                # stamped through a fresh read: the scan runs long, and by write time
                # the startup snapshot's copy no longer carries other phases' writes
                store.edit_pr(n).set_threat(result)
                restamped += 1
            if result["verdict"] == "malicious":
                malicious.append(n)
                author = rec.author
                if author:
                    threats.block_actor(
                        registry, author,
                        reason=f"Malicious PR(s): {', '.join(result['signatures'])}",
                        added=today, incidents=[n])
                threats.record_incident(
                    registry, n, author, rec.head_sha,
                    result["signatures"], noticed=today)
            elif result["verdict"] == "suspicious":
                suspicious.append(n)

            # A potential leaked credential is operationally urgent regardless of
            # the PR's disposition — emit an action item for a human to confirm or
            # dismiss (upsert: re-running the scan never reopens one already closed).
            if "secret-leak" in result["signatures"]:
                actions.upsert(action_items, actions.make_item(
                    "rotate-secret", pr=n, created=today,
                    summary=f"Potential secret leaked in PR #{n} ({rec.author or '?'})",
                    evidence=result["detail"].get("secret-leak", ""),
                    detail="A live-looking credential was committed in this PR's diff. "
                           "Confirm it: if real, rotate the key at its provider and notify "
                           "upstream — closing/merging the PR does not invalidate an "
                           "already-pushed secret. Dismiss if it's a false positive "
                           "(e.g. a public record id, not a credential)."))

    store.save_threats(registry)
    store.save_action_items(action_items)
    secret_leaks = sum(1 for it in action_items["items"] if it["kind"] == "rotate-secret")
    stats = {"scanned": len(prs), "restamped": restamped, "malicious": len(malicious),
             "suspicious": len(suspicious), "uncached": uncached,
             **fetch_stats,
             "blocked_actors": len(registry.get("actors", {})),
             "rotate_secret_items": secret_leaks}
    store.append_run({"phase": "threat-scan", "started": started,
                      "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "stats": stats, "malicious_prs": sorted(malicious)})
    print(f"done: {stats}")
    if malicious:
        print(f"  MALICIOUS: {sorted(malicious)}")
    if suspicious:
        print(f"  suspicious: {sorted(suspicious)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
