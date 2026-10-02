"""Phase 0.5 — THREAT SCAN (deterministic, cheap, run after INGEST).

Scans each PR's whole diff for the attack signatures in threats.py and checks
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
exempt: their diffs are deliberately never fetched or signature-scanned
(gates.is_dependabot_bump), and the run stamps their head `clear` with
EXEMPT's `detail`, so they meet the merge gate's demand for a verdict at the
current head (gates.pr_clean). --no-fetch skips the fetch step and scans
only what is already cached. Either way, PRs left without a diff are reported
as `uncached` in the run ledger and left unscanned.

The cache holds each diff capped at diff_cache.MAX_DIFF_BYTES, with every file
past the cap and the profile's artifact files reduced to a one-line stub, so a
payload placed there is absent from it. The scan judges the whole diff: a
cached copy that is not whole is judged on the whole diff read from GitHub for
this scan alone (judge_diff), and a file GitHub itself returns no whole patch
for reads as `unscannable-diff` — suspicious, never clear. When that read is
off (--no-fetch) or fails, the capped copy decides only a malicious verdict;
otherwise the PR is reported as `incomplete` and its stamp left as it was.

The scan is idempotent: re-running re-derives the same verdicts and the
registry merges rather than duplicates. The scan loop runs on one bound store
connection, and a PR whose stored stamp already carries the derived verdict at
its current head is left untouched — a re-run over the corpus reads and writes
only the records whose verdict or head changed. A verdict is stamped only while
the PR's stored head is still the one scanned; a head that moved mid-run waits
for the next run. The blocks, incidents, and action items a run produces are
applied to a fresh read of their registry when the run ends, so a registry
edit made while the run scanned is kept.

`scan` is the run itself. This CLI runs it over every open PR (or `--only`'s);
a worker machine runs it on a cadence over the PRs `unscanned` names, so a
head INGEST records is scanned within minutes
(prospector_app/backend/threat_refresh.py).

Usage:
  uv run python threat_scan.py [--store DIR] [--only N[,N...]] [--no-fetch]
"""
from __future__ import annotations

import argparse
import copy
import functools
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pipeline import actions
from pipeline import diff_cache
from pipeline import gates
from pipeline import profile
from pipeline import progress
from pipeline import storekit
from pipeline import threats
from pipeline.store import Store
from pipeline.wire import DiffManifestItem

if TYPE_CHECKING:
    from pipeline.model import Pr


# The verdict stamped on a dependency bump the scan exempts.
EXEMPT = {"verdict": "clear", "signatures": [], "detail": {"exempt": "dependency-bump"}}


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
    uncached = [(n, rec) for n, rec in sorted(prs.items())
                if rec.state == "open" and rec.head_sha
                and not (diffs_dir / f"{rec.head_sha}.diff").exists()]
    if uncached:
        progress.say(f"fetching diffs for {len(uncached):,} {'PR' if len(uncached) == 1 else 'PRs'} "
                     "with none cached at the head…")
    listing = progress.Progress("listing the files of",
                                sum(1 for _, rec in uncached if rec.author in bots),
                                "automation PRs", one="automation PR")
    for n, rec in uncached:
        if rec.author in bots:
            bump = gates.is_dependabot_bump(
                rec.author, diff_cache.changed_paths(n, rec.head_sha, diffs_dir))
            listing.advance()
            if bump:
                exempt.add(n)
                continue
        manifest.append(DiffManifestItem.for_pr(n, rec, diffs_dir))
    listing.finish(f"{len(exempt):,} dependency-only bumps, left unfetched")
    fetched, failed = diff_cache.fetch_diffs(manifest, workers=workers, store=store,
                                             diffs_dir=diffs_dir)
    return {"fetched": fetched, "fetch_failed": failed, "bump_exempt": len(exempt)}, exempt


def stamped_at_head(rec: Pr) -> bool:
    """Whether `rec` carries a threat stamp computed against its current head.
    Threat currency is this head comparison alone: `threat` is not
    freshness-governed, since a malicious verdict gates at any head."""
    sec = rec.section("threat")
    return bool(sec) and sec.get("against_head_sha") == rec.head_sha


def unscanned(prs: dict[int, Pr], registry: dict) -> list[int]:
    """Open PRs whose current head has no verdict the scan would keep, lowest
    number first: no threat stamp at that head (a moved head or a new
    arrival), or an author `registry` blocks under a stamp that does not read
    malicious. A maintainer's PR never reads malicious, so the blocklist
    clause passes over the profile's trusted authors."""
    trusted = profile.active().trusted_authors
    out: list[int] = []
    for n, rec in sorted(prs.items()):
        if rec.state != "open" or not rec.head_sha:
            continue
        if not stamped_at_head(rec):
            out.append(n)
        elif (rec.threat_verdict != "malicious" and rec.author not in trusted
              and threats.is_blocked_actor(registry, rec.author)):
            out.append(n)
    return out


def already_stamped(rec: Pr, result: dict) -> bool:
    """Whether `rec`'s stored threat stamp already carries `result` at the
    record's current head — verdict, signatures, and detail all equal. Such a
    record needs no write: re-stamping the same verdict at the same head
    changes nothing a reader can observe."""
    if not stamped_at_head(rec):
        return False
    sec = rec.section("threat") or {}
    return all(sec.get(k) == result.get(k) for k in ("verdict", "signatures", "detail"))


@dataclass(frozen=True)
class DiffScan:
    """What the scan judged of one PR's diff at its head. `result` is
    threats.scan_diff's payload, or None when the scan holds no judgment of
    the whole diff. `source` is what it read: `cached` (a whole cached diff),
    `fetched` (the whole diff, read from GitHub for this scan), `partial` (a
    capped cached copy alone, judged only when what it holds is already
    malicious), or `uncached` (nothing)."""
    result: dict | None
    source: Literal["cached", "fetched", "partial", "uncached"]


def judge_diff(rec: Pr, diffs_dir: Path = diff_cache.DIFFS, *, fetch: bool = True) -> DiffScan:
    """Scan the whole diff at `rec`'s head. A cached diff is judged as it
    stands when it is whole (diff_cache.is_complete); a capped copy is judged
    on the whole diff read from GitHub for this scan alone (never cached),
    with every file GitHub returned no whole patch for passed to the scan as
    unread. With `fetch` off or GitHub unreachable, the capped copy decides
    only a malicious verdict — more lines can add signatures but never clear
    one — and otherwise leaves the PR unjudged."""
    path = diffs_dir / f"{rec.head_sha}.diff"
    if not path.exists():
        return DiffScan(None, "uncached")
    ds = (rec.signals or {}).get("diffstat", {}) or {}

    def scan_text(text: str, unread: tuple[str, ...] = ()) -> dict:
        return threats.scan_diff(text, additions=ds.get("additions"),
                                 deletions=ds.get("deletions"), unread=unread)

    text = path.read_text(errors="replace")
    if diff_cache.is_complete(text):
        return DiffScan(scan_text(text), "cached")
    whole = diff_cache.fetch_complete(rec.number) if fetch else None
    if whole is not None:
        return DiffScan(scan_text(whole.text, whole.unread), "fetched")
    seen = scan_text(text)
    return DiffScan(seen if seen["verdict"] == "malicious" else None, "partial")


def judge_diffs(prs: dict[int, Pr], diffs_dir: Path, *, fetch: bool,
                workers: int = 8) -> dict[int, DiffScan]:
    """judge_diff over every PR in `prs`, in parallel: the whole-diff reads
    are GitHub round-trips, made before the scan's store loop binds its
    connection."""
    ordered = sorted(prs.items())
    scanning = progress.Progress("scanning the diffs of", len(ordered), "PRs", one="PR")
    judged: dict[int, DiffScan] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        scans = ex.map(lambda item: judge_diff(item[1], diffs_dir, fetch=fetch), ordered)
        for (n, _), diff in zip(ordered, scans):
            judged[n] = diff
            scanning.advance()
    fetched = sum(1 for d in judged.values() if d.source == "fetched")
    scanning.finish(f"{fetched:,} whole {'diff' if fetched == 1 else 'diffs'} read from GitHub")
    return judged


def scan_record(rec: Pr, registry: dict, diffs_dir: Path = diff_cache.DIFFS,
                diff: DiffScan | None = None) -> dict | None:
    """Compute the threat verdict for one PR record. Returns the `threat`
    payload (unstamped) or None when the scan holds no judgment of the diff
    and the author is clean (nothing to assert). A blocked author yields a
    verdict even with no diff, since blocklist membership alone is
    disqualifying. `diff` is the PR's judged diff; judge_diff reads it when
    none is given.

    A repository maintainer (the profile's trusted_authors) is never flagged:
    the verdict is always `clear` and neither the attack signatures nor the
    actor blocklist apply. A secret-leak signature is kept on the clear stamp —
    a leaked credential must rotate no matter who pushed it, and the
    rotate-secret action item keys off the signature."""
    author = rec.author
    if diff is None:
        diff = judge_diff(rec, diffs_dir)
    result = diff.result or {"verdict": "clear", "signatures": [], "detail": {}}

    if author in profile.active().trusted_authors:
        kept = [s for s in result["signatures"] if s == "secret-leak"]
        result = {"verdict": "clear", "signatures": kept,
                  "detail": {k: v for k, v in result["detail"].items()
                             if k == "secret-leak"}}
        if not kept and diff.result is None:
            return None  # nothing observed and nothing judged — don't stamp
        return result

    blocked = threats.is_blocked_actor(registry, author)
    if blocked and result["verdict"] != "malicious":
        result = {"verdict": "malicious",
                  "signatures": sorted(set(result["signatures"]) | {"blocked-actor"}),
                  "detail": {**result["detail"], "blocked-actor": author}}

    if result["verdict"] == "clear" and diff.result is None:
        return None  # nothing observed and nothing judged — don't stamp
    return result


@dataclass
class Scan:
    """One scan run: its ledger stats and its PRs by outcome. `unstamped` names
    the PRs it left without a verdict at their head — no diff and nothing on
    file, a capped copy whose whole diff it could not read, or a head that
    moved mid-run."""
    stats: dict[str, int]
    malicious: list[int] = field(default_factory=list)
    suspicious: list[int] = field(default_factory=list)
    unstamped: list[int] = field(default_factory=list)


def _commit(load: Callable[[], dict], save: Callable[[dict], None],
            ops: list[Callable[[dict], dict]]) -> dict:
    """Apply `ops` to a fresh read of a registry, save it when they changed it,
    and return it as stored."""
    reg = load()
    before = json.dumps(reg, sort_keys=True)
    for op in ops:
        op(reg)
    if json.dumps(reg, sort_keys=True) != before:
        save(reg)
    return reg


def scan(store: Store, prs: dict[int, Pr], diffs_dir: Path | None = None, *,
         fetch: bool = True) -> Scan:
    """Scan every PR in `prs` and record what it finds: stamp each verdict,
    block a malicious PR's author and log the incident, and raise a
    rotate-secret action item for a leaked credential. With `fetch`, the
    current-head diff of every open PR that has none cached is fetched first
    (fetch_missing_diffs). `diffs_dir` None is the canonical diff cache."""
    diffs_dir = diffs_dir or diff_cache.DIFFS
    fetch_stats: dict[str, int] = {}
    exempt: set[int] = set()
    if fetch:
        fetch_stats, exempt = fetch_missing_diffs(prs, diffs_dir, store=store)
        progress.say(f"fetch: {fetch_stats}")
    progress.say("loading the threat registry…")
    registry = store.load_threats()
    today = storekit.utc_day()

    judged = judge_diffs(prs, diffs_dir, fetch=fetch)

    threat_ops: list[Callable[[dict], dict]] = []
    item_ops: list[Callable[[dict], dict]] = []
    out = Scan(stats={})
    uncached = 0
    incomplete = 0
    restamped = 0
    moved = 0
    stamping = progress.Progress("stamping", len(prs), "PRs", one="PR")
    # one bound connection for the whole loop: each per-PR read or write is a
    # single round-trip on it, with no per-statement connect handshake
    with store.batch():
        for n, rec in sorted(prs.items()):
            diff = judged[n]
            result = scan_record(rec, registry, diffs_dir=diffs_dir, diff=diff)
            stamping.advance()
            if result is None and n in exempt:
                result = copy.deepcopy(EXEMPT)
            if result is None:
                if diff.source == "partial":
                    incomplete += 1
                else:
                    uncached += 1
                out.unstamped.append(n)
                continue
            if not already_stamped(rec, result):
                # stamped through a fresh read, and only while its head is the one
                # scanned: the scan runs long, and by write time the startup
                # snapshot's copy may lack other phases' writes or name a head
                # INGEST has since moved past
                current = store.edit_pr(n)
                if current.head_sha == rec.head_sha:
                    current.set_threat(result)
                    restamped += 1
                else:
                    moved += 1
                    out.unstamped.append(n)
            if result["verdict"] == "malicious":
                out.malicious.append(n)
                author = rec.author
                ops: list[Callable[[dict], dict]] = []
                if author:
                    ops.append(functools.partial(
                        threats.block_actor, author=author,
                        reason=f"Malicious PR(s): {', '.join(result['signatures'])}",
                        added=today, incidents=[n]))
                ops.append(functools.partial(
                    threats.record_incident, pr=n, author=author, head_sha=rec.head_sha,
                    signatures=result["signatures"], noticed=today))
                for op in ops:
                    op(registry)  # later PRs in this run see the author blocked
                threat_ops.extend(ops)
            elif result["verdict"] == "suspicious":
                out.suspicious.append(n)

            # A potential leaked credential is operationally urgent regardless of
            # the PR's disposition — emit an action item for a human to confirm or
            # dismiss (upsert: re-running the scan never reopens one already closed).
            if "secret-leak" in result["signatures"]:
                evidence = result["detail"].get("secret-leak", "")
                item_ops.append(functools.partial(actions.upsert, item=actions.make_item(
                    "rotate-secret", pr=n, created=today,
                    summary=f"Potential secret leaked in PR #{n} ({rec.author or '?'})",
                    evidence=evidence,
                    fixture=actions.likely_fixture(evidence),
                    detail="A live-looking credential was committed in this PR's diff. "
                           "Confirm it: if real, rotate the key at its provider and notify "
                           "upstream — closing/merging the PR does not invalidate an "
                           "already-pushed secret. Dismiss if it's a false positive "
                           "(e.g. a public record id, not a credential).")))
    stamping.finish(f"{restamped:,} restamped, {len(out.malicious):,} malicious, "
                    f"{len(out.suspicious):,} suspicious, {uncached:,} without a diff, "
                    f"{incomplete:,} without the whole diff"
                    + (f", {moved:,} moved mid-run" if moved else ""))

    progress.say("saving the threat registry and action items…")
    registry = _commit(store.load_threats, store.save_threats, threat_ops)
    action_items = _commit(store.load_action_items, store.save_action_items, item_ops)
    secret_leaks = sum(1 for it in action_items["items"] if it["kind"] == "rotate-secret")
    out.stats = {"scanned": len(prs), "restamped": restamped, "malicious": len(out.malicious),
                 "suspicious": len(out.suspicious), "uncached": uncached, "moved": moved,
                 "incomplete": incomplete,
                 "complete_fetched": sum(1 for d in judged.values() if d.source == "fetched"),
                 **fetch_stats,
                 "blocked_actors": len(registry.get("actors", {})),
                 "rotate_secret_items": secret_leaks}
    return out


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
    started = storekit.now()

    print("loading every stored PR to select the ones to scan…", flush=True)
    prs = store.all_prs()
    if args.only:
        want = {int(x) for x in args.only.split(",")}
        prs = {n: r for n, r in prs.items() if n in want}
    else:
        prs = {n: r for n, r in prs.items() if r.state == "open"}

    result = scan(store, prs, diffs_dir, fetch=not args.no_fetch)
    store.append_run({"phase": "threat-scan", "started": started,
                      "finished": storekit.now(),
                      "stats": result.stats, "malicious_prs": sorted(result.malicious)})
    print(f"done: {result.stats}")
    if result.malicious:
        print(f"  MALICIOUS: {sorted(result.malicious)}")
    if result.suspicious:
        print(f"  suspicious: {sorted(result.suspicious)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
