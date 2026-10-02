"""One incremental CLUSTER + ANALYZE pass, headless: re-home the PRs whose head
moved since they were clustered, summarize up to `--limit` PRs lacking a current
summary, place up to `--limit` summarized never-clustered PRs into an existing
cluster, a new one, or standalone (existing clusters stay frozen — they only
gain members), then analyze up to `--analyze` pending clusters.

It is the clustering lane's unit of work (prospector_app/backend/cluster_refresh.py
runs it on a cadence under PROSPECTOR_UNATTENDED, so every agent call is gated
by the AI capacity policy) and the Control tab's `cluster-new` job. The SUMMARIZE,
ASSIGN, and ANALYZE instructions are the drivers' canonical prompts; store I/O
stays on the calling thread, and the worker threads only run agents.

A subsystem with more new PRs than ASSIGN_CHUNK is placed over several rounds,
each against the clusters the round before it committed, so PRs that share a
root problem across chunks still land together.

  uv run python pipeline/cluster_pass.py [--limit N] [--analyze N] [--concurrency N]

Exits 0 when the pass finished or a closed capacity gate deferred it,
EXIT_LIMIT when the account's usage limit is spent, EXIT_AGENT_UNAVAILABLE when
the agent CLI could not run, and EXIT_FAULT when a stage started agents and
every one of them failed.
"""
from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from pipeline import agent_wave
from pipeline import analyze_clusters
from pipeline import cluster_driver
from pipeline import diff_cache
from pipeline import headless_agent
from pipeline.store import Store
from pipeline.storekit import now as _now

EXIT_FAULT = 2
EXIT_AGENT_UNAVAILABLE = 3
EXIT_LIMIT = 4

# PRs one summarize agent reads.
SUMMARY_BATCH = 10
# New PRs one assign agent places.
ASSIGN_CHUNK = 40

T = TypeVar("T")

_print_lock = threading.Lock()


def _say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


@dataclass
class Stage:
    attempted: int = 0
    started: int = 0
    errored: int = 0
    stop: agent_wave.Stop | None = None

    @property
    def all_failed(self) -> bool:
        return self.started > 0 and self.errored == self.started


class _Outage:
    """The agent CLI's first outage this pass: once one call reports it, no
    later call starts its agent."""

    def __init__(self) -> None:
        self.reason: str | None = None

    def guard(self, fn: Callable[[], T]) -> T:
        if self.reason is not None:
            raise agent_wave.NotStarted
        try:
            return fn()
        except headless_agent.CapacityExhausted:
            raise
        except headless_agent.AgentUnavailable as e:
            self.reason = self.reason or str(e)
            raise


def _gather(futures: dict[Future[T], str], stage: Stage, wave: agent_wave.Wave,
            commit: Callable[[str, T], None]) -> None:
    """Commit each finished agent's answer on this thread, counting the ones
    that raised and keeping the first capacity stop."""
    for fut in as_completed(futures):
        key = futures[fut]
        try:
            answer = fut.result()
        except agent_wave.NotStarted:
            continue
        except Exception as e:
            stage.started += 1
            stage.errored += 1
            _say(f"    ! {key} failed, continuing: {e}")
            if stage.stop is None and (stop := agent_wave.stop_reason(e, wave.not_started())):
                stage.stop = stop
                _say(stop.line)
            continue
        stage.started += 1
        commit(key, answer)


def _record(store: Store, phase: str, started: str, stats: dict,
            trigger: str | None) -> None:
    record: dict = {"phase": phase, "started": started, "finished": _now(), "stats": stats}
    if trigger:
        record["trigger"] = trigger
    store.append_run(record)


def summarize(store: Store, limit: int, concurrency: int, outage: _Outage,
              trigger: str | None) -> Stage:
    """Summarize up to `limit` open PRs lacking a current summary."""
    stage = Stage()
    started = _now()
    manifest = cluster_driver.wave(store, limit) if limit > 0 else []
    if not manifest:
        _say("  no PR needs a summary.")
        return stage
    _say(f"  {len(manifest)} PR(s) need a summary; fetching any diff not cached…")
    diff_cache.fetch_diffs(manifest, store=store)
    ready = [m.to_dict() for m in manifest
             if (diff_cache.DIFFS / f"{m.head_sha}.diff").exists()]
    batches = [ready[i:i + SUMMARY_BATCH] for i in range(0, len(ready), SUMMARY_BATCH)]
    stage.attempted = len(ready)
    written = errors = 0

    def agent(batch: list[dict]) -> list[dict]:
        def ask() -> list[dict]:
            reply, _ = headless_agent.json_reply(
                lambda: cluster_driver.run_summarize_agent(batch))
            items = reply.get("items")
            return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        return outage.guard(ask)

    def commit(key: str, items: list[dict]) -> None:
        nonlocal written, errors
        ok, errs = cluster_driver.commit_summaries(store, items)
        written += ok
        errors += len(errs)
        _say(f"    ✓ {key}: {ok} summary(ies) written"
             + (f", {len(errs)} rejected" if errs else ""))

    _say(f"  summarizing {len(ready)} PR(s) in {len(batches)} batch(es), "
         f"up to {concurrency} at a time…")
    wave = agent_wave.Wave()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {wave.submit(pool, agent, b): f"PRs #{b[0]['pr']}…#{b[-1]['pr']}"
                   for b in batches}
        _gather(futures, stage, wave, commit)
    _record(store, "cluster:summaries", started,
            {"written": written, "errors": errors, "attempted": stage.attempted}, trigger)
    return stage


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def restrict(payload: dict, unit: dict) -> dict:
    """The agent's assignment held to its unit: joins only of the unit's new
    PRs into the unit's existing clusters, new clusters only of the unit's new
    PRs, standalone stamps only on them."""
    mine = {int(p["pr"]) for p in unit["new_prs"]}
    targets = {int(c["id"]) for c in unit["existing_clusters"]}
    joins = []
    for j in payload.get("joins") or []:
        if not isinstance(j, dict):
            continue
        pr, cid = _int(j.get("pr")), _int(j.get("cluster_id"))
        if pr in mine and cid in targets:
            joins.append({"pr": pr, "cluster_id": cid})
    new_clusters = []
    for nc in payload.get("new_clusters") or []:
        if not isinstance(nc, dict):
            continue
        members = sorted({p for p in (_int(x) for x in nc.get("prs") or []) if p in mine})
        if len(members) >= 2:
            new_clusters.append({"root_problem": str(nc.get("root_problem") or ""),
                                 "prs": members})
    standalone = [p for p in (_int(x) for x in payload.get("standalone") or []) if p in mine]
    return {"joins": joins, "new_clusters": new_clusters, "standalone": standalone}


def assign(store: Store, limit: int, concurrency: int, outage: _Outage,
           trigger: str | None) -> Stage:
    """Place up to `limit` summarized never-clustered PRs, one agent per
    subsystem chunk, in rounds: each round's units are built against the
    clusters the round before committed. A PR offered once is not offered again
    this pass, whatever the agent made of it."""
    stage = Stage()
    started = _now()
    offered: set[int] = set()
    totals = {"joined": 0, "created": 0, "standalone": 0, "errors": 0}

    def agent(unit: dict) -> dict:
        def ask() -> dict:
            reply, _ = headless_agent.json_reply(lambda: cluster_driver.run_assign_agent(unit))
            return restrict(reply, unit)
        return outage.guard(ask)

    def commit(key: str, payload: dict) -> None:
        res = cluster_driver.commit_assignments(store, payload)
        for k in ("joined", "created", "standalone"):
            totals[k] += res[k]
        totals["errors"] += len(res["errors"])
        _say(f"    ✓ {key}: {res['joined']} joined, {res['created']} new cluster(s), "
             f"{res['standalone']} standalone")

    rnd = 0
    while len(offered) < limit and outage.reason is None and stage.stop is None:
        units: list[dict] = []
        room = limit - len(offered)
        for unit in cluster_driver.assign_payloads(store):
            chunk = [p for p in unit["new_prs"]
                     if int(p["pr"]) not in offered][:min(ASSIGN_CHUNK, room)]
            if chunk:
                units.append({**unit, "new_prs": chunk})
                room -= len(chunk)
            if room <= 0:
                break
        if not units:
            break
        rnd += 1
        placing = sum(len(u["new_prs"]) for u in units)
        offered.update(int(p["pr"]) for u in units for p in u["new_prs"])
        stage.attempted += placing
        _say(f"  round {rnd}: placing {placing} PR(s) across {len(units)} subsystem(s)…")
        wave = agent_wave.Wave()
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futures = {wave.submit(pool, agent, u): str(u["subsystem"]) for u in units}
            _gather(futures, stage, wave, commit)
    if not stage.attempted:
        _say("  no summarized PR is waiting for a cluster.")
        return stage
    _record(store, "cluster:assign", started, {**totals, "attempted": stage.attempted},
            trigger)
    return stage


def run(store: Store, *, limit: int, analyze_limit: int, concurrency: int,
        trigger: str | None = None) -> int:
    outage = _Outage()
    _say("① Re-homing PRs whose head moved since they were clustered…")
    reset = cluster_driver.reset_stale_memberships(store)
    if any(reset.values()):
        _record(store, "cluster:reset-stale", _now(),
                {"detached": len(reset["detached"]),
                 "emptied_clusters": len(reset["emptied_clusters"]),
                 "standalone_cleared": len(reset["standalone_cleared"])}, trigger)
    _say(f"  {len(reset['detached'])} detached, {len(reset['standalone_cleared'])} "
         "standalone stamp(s) cleared.")

    stages: list[Stage] = []
    _say(f"② Summarizing up to {limit} PR(s)…")
    stages.append(summarize(store, limit, concurrency, outage, trigger))
    if outage.reason is None and stages[-1].stop is None:
        _say(f"③ Placing up to {limit} new PR(s) into clusters…")
        stages.append(assign(store, limit, concurrency, outage, trigger))
    if outage.reason is None and stages[-1].stop is None and analyze_limit > 0:
        _say(f"④ Analyzing up to {analyze_limit} pending cluster(s)…")
        result = analyze_clusters.run(store, limit=analyze_limit, concurrency=concurrency,
                                      trigger=trigger)
        stages.append(Stage(attempted=result.attempted, started=result.attempted,
                            errored=result.errored, stop=result.stop))

    if outage.reason is not None:
        _say(f"✗ the agent CLI could not run on this machine: {outage.reason}")
        return EXIT_AGENT_UNAVAILABLE
    stop = next((s.stop for s in stages if s.stop is not None), None)
    if stop is not None:
        return EXIT_LIMIT if stop.exit_code else 0
    if any(s.all_failed for s in stages):
        _say("✗ every agent a stage started failed.")
        return EXIT_FAULT
    _say("✓ pass complete.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50,
                    help="max PRs to summarize, and max new PRs to place (default 50)")
    ap.add_argument("--analyze", type=int, default=10,
                    help="max pending clusters to analyze (default 10)")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="agent calls to run at once (default 3)")
    ap.add_argument("--trigger", default=None,
                    help="what started the pass, stamped on its ledger rows")
    ap.add_argument("--store", type=Path, default=None,
                    help="store root (default: the shared store)")
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()
    return run(store, limit=args.limit, analyze_limit=args.analyze,
               concurrency=args.concurrency, trigger=args.trigger)


if __name__ == "__main__":
    sys.exit(main())
