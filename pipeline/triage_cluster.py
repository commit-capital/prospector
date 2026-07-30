"""Triage a single cluster end-to-end (existing members only): refresh member
facts -> cache diffs for moved heads -> threat-rescan moved heads -> re-summarize
stale members -> analyze -> format the rationale for display -> report. Stops at
ANALYZE. Security is NOT run; it auto-invalidates via SHA-bound freshness when a
head moved (reported below).

  uv run python triage_cluster.py --cluster N [--store DIR]

Progress is printed to stdout one line per step; the app streams it as SSE.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import analyze_driver
from pipeline import cluster_driver
from pipeline import diff_cache
from pipeline import headless_agent
from pipeline import ingest
from pipeline import redundancy
from pipeline import reformat_rationales
from pipeline import settings
from pipeline.analyze_driver import ANALYZE_FENCED_TAIL, ANALYZE_PROMPT
from pipeline.cluster_driver import SUMMARIZE_FENCED_TAIL, summarize_prompt
from pipeline.freshness import is_current
from pipeline.settings import REPO_ROOT
from pipeline.store import Store

if TYPE_CHECKING:
    from pipeline.model import Cluster

# The decision criteria are the canonical ANALYZE_PROMPT / summarize_prompt() owned
# by the drivers and shipped to the workflows via index.json — consumed here, never
# restated. This headless path only differs in the per-call bundle/batch path (the
# `__BUNDLE_PATH__` / `__BATCH_PATH__` placeholder) and the fenced-block output tail.


def _say(msg: str) -> None:
    print(msg, flush=True)


def _agent_progress(ev) -> None:
    if ev[0] == "tool":
        inp = ev[2] if len(ev) > 2 else {}
        _say(f"    · {headless_agent.tool_summary(ev[1], inp)}")


def run(store: Store, cid: int) -> int:
    cluster = store.load_cluster(cid)
    if cluster is None:
        _say(f"✗ cluster {cid} not found in store")
        return 1
    try:
        return _run(store, cid, cluster)
    except (RuntimeError, ValueError) as e:
        _say(f"✗ triage failed: {e}")
        return 1


def _run(store: Store, cid: int, cluster: Cluster) -> int:
    members = [int(p) for p in cluster.prs]
    _say(f"▶ Triaging cluster {cid}: {cluster.root_problem}")
    _say(f"  members: {members}")

    # 1. Refresh member facts (moved heads auto-stale summary/analysis/security).
    _say("① Refreshing member PRs from GitHub…")
    refreshed = ingest.refresh_prs(store, members)
    moved = [r["pr"] for r in refreshed if r["moved"]]
    for r in refreshed:
        _say(f"    PR #{r['pr']}: "
             + (f"head {r['old_sha'][:7]}→{r['new_sha'][:7]} (changed)"
                if r["moved"] else "unchanged"))

    # 2. Cache diffs for moved heads (summarize + analyze read diff_path).
    if moved:
        _say("② Caching diffs for moved heads…")
        for n in moved:
            rec = store.load_pr(n)
            if rec is not None:
                diff_cache.fetch_diff(n, rec.head_sha or "")

    # 3. Threat re-scan moved heads (deterministic backstop; sticky on malicious).
    if moved:
        _say("③ Threat-rescanning moved heads…")
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "pipeline" / "threat_scan.py"),
             "--only", ",".join(str(n) for n in moved)],
            cwd=str(REPO_ROOT), capture_output=True, text=True)
        for line in (res.stdout + res.stderr).splitlines():
            if line.strip():
                _say(f"    {line}")

    # 4. Re-summarize members whose summary went stale.
    stale = [n for n in members
             if (r := store.load_pr(n)) and not is_current(r, "summary")]
    if stale:
        _say(f"④ Re-summarizing {len(stale)} stale member(s)…")
        batch = []
        for n in stale:
            pr = store.load_pr(n)
            if pr is None:
                continue
            sha = pr.head_sha or ""
            batch.append({"pr": n, "head_sha": sha, "title": pr.title,
                          "diff_path": str(diff_cache.DIFFS / f"{sha}.diff")})
        bp = Path(f"/tmp/triage-summarize-{cid}.json")
        bp.write_text(json.dumps(batch))
        text = headless_agent.run_agent(
            summarize_prompt().replace("__BATCH_PATH__", str(bp)) + SUMMARIZE_FENCED_TAIL,
            allow_gh=False, cwd=str(REPO_ROOT), on_event=_agent_progress)
        items = headless_agent.extract_json(text).get("items", [])
        ok, errs = cluster_driver.commit_summaries(store, items)
        _say(f"    summaries written: {ok}; errors: {len(errs)}")
        for e in errs:
            _say(f"    ! {e}")
    else:
        _say("④ No stale summaries — skipping re-summarize.")

    # 5. Force re-analyze (bypass the staleness-gated pending(); always re-run).
    _say("⑤ Re-classifying the cluster…")
    bundle = analyze_driver.bundle(store, cid, master=redundancy.MasterTree())
    bp = Path(f"/tmp/triage-analyze-{cid}.json")
    bp.write_text(json.dumps(bundle))
    text = headless_agent.run_agent(
        ANALYZE_PROMPT.replace("__BUNDLE_PATH__", str(bp))
                      .replace("__BRANCH__", settings.default_branch()) + ANALYZE_FENCED_TAIL,
        allow_gh=True, cwd=str(REPO_ROOT), on_event=_agent_progress)
    payload = headless_agent.extract_json(text)
    errs = analyze_driver.commit_analysis(store, payload)
    if errs:
        _say("✗ analysis failed validation (cluster left unchanged):")
        for e in errs:
            _say(f"    ! {e}")
        return 1

    # 6. Format the just-committed rationale for app display. This is derived,
    # presentation-only data: an unavailable model or an unexpected formatter
    # failure must not undo or fail the authoritative triage result.
    _say("⑥ Formatting the rationale for display…")
    try:
        fresh_cluster = store.load_cluster(cid)
        source = fresh_cluster.rationale if fresh_cluster is not None else None
        if not source:
            _say("    ! no rationale was committed — keeping raw display")
        else:
            formatted = reformat_rationales.reformat_one(source)
            store.edit_cluster(cid).record_reformat(
                formatted["body"], formatted["summary"])
            if formatted["tier"] == "raw":
                _say("    ! formatter kept the raw rationale (no verified reformat)")
            else:
                _say(f"    ✓ formatted with {formatted['tier']}; TL;DR written")
    except Exception as exc:
        _say(f"    ! rationale formatting unavailable; keeping raw display: {exc}")

    # 7. Report.
    c = store.load_cluster(cid)
    outcome = c.outcome if c is not None else None
    _say(f"✓ outcome: {outcome}")
    for n in members:
        rec = store.load_pr(n)
        disp = rec.disposition if rec is not None else "?"
        _say(f"    PR #{n}: {disp}")
        if rec is not None and rec.section("security") and not is_current(rec, "security"):
            _say(f"      ⚠ security verdict is now stale (head moved) — "
                 f"merge-blocked until a fresh security review "
                 f"(security_review.py --pr {n}, or the ↻ Run button in the app)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cluster", type=int, required=True)
    ap.add_argument("--store", default=None)
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()
    return run(store, args.cluster)


if __name__ == "__main__":
    sys.exit(main())
