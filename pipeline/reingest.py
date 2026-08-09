"""Reingest ONE PR against its current head SHA: refresh its facts, then bring
only the sections that went stale back to the current head. Stops at ANALYZE.

Deterministic first: `ingest.refresh_prs` re-fetches signals / CI / Greptile and
re-stamps signals + drift at the live head — on its own enough to clear a
resubmitted PR's drift block for the human merge gate (`gates.merge_eligibility`
does not require analysis freshness). When that still leaves `summary` or
`analysis` pinned to an older head, the agentic tail brings them current:
re-summarize the PR (headless), then re-analyze the cluster(s) it belongs to
(headless, per-cluster) or — for a standalone PR — disposition it deterministically.

A no-op when every SHA-bound section is already current (e.g. the head has not
moved since the last full pass): the refresh runs, finds nothing stale, and stops
without spending an agent. Scoped to one PR — never a full-corpus re-cluster.

  uv run python pipeline/reingest.py --pr N [--store DIR]

Progress is printed to stdout one line per step; the app streams it as SSE.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pipeline import analyze_driver
from pipeline import cluster_driver
from pipeline import diff_cache
from pipeline import headless_agent
from pipeline import ingest
from pipeline import redundancy
from pipeline import reformat_rationales
from pipeline import settings
from pipeline.analyze_driver import ANALYZE_FENCED_TAIL, ANALYZE_PROMPT
from pipeline.cluster_driver import SUMMARIZE_FENCED_TAIL, SUMMARIZE_PROMPT
from pipeline.freshness import is_current
from pipeline.settings import REPO_ROOT
from pipeline.store import Store
from pipeline.storekit import now as _now

# The decision criteria are the canonical ANALYZE_PROMPT / SUMMARIZE_PROMPT owned by
# the drivers — consumed here, never restated. This single-PR path only differs from
# triage_cluster in scope (one PR's facts, not a whole cluster's members).


def _say(msg: str) -> None:
    print(msg, flush=True)


def _agent_progress(ev) -> None:
    if ev[0] == "tool":
        inp = ev[2] if len(ev) > 2 else {}
        _say(f"    · {headless_agent.tool_summary(ev[1], inp)}")


def _threat_rescan(pr: int) -> None:
    """Deterministic threat backstop for one PR's current head. Best-effort: a
    scanner hiccup is surfaced but never aborts the reingest (a malicious verdict
    is sticky and would already be on the record from an earlier scan)."""
    res = subprocess.run(
        [sys.executable, str(REPO_ROOT / "pipeline" / "threat_scan.py"), "--only", str(pr)],
        cwd=str(REPO_ROOT), capture_output=True, text=True)
    for line in (res.stdout + res.stderr).splitlines():
        if line.strip():
            _say(f"    {line}")


def _resummarize(store: Store, pr: int, head_sha: str, title: str | None) -> None:
    """Re-summarize one PR headlessly at its current head, then commit through the
    validated accessor. Reuses the canonical SUMMARIZE_PROMPT + commit path."""
    batch = [{"pr": pr, "head_sha": head_sha, "title": title,
              "diff_path": str(diff_cache.DIFFS / f"{head_sha}.diff")}]
    bp = Path(f"/tmp/reingest-summarize-{pr}.json")
    bp.write_text(json.dumps(batch))
    text = headless_agent.run_agent(
        SUMMARIZE_PROMPT.replace("__BATCH_PATH__", str(bp)) + SUMMARIZE_FENCED_TAIL,
        allow_gh=False, cwd=str(REPO_ROOT), on_event=_agent_progress)
    items = headless_agent.extract_json(text).get("items", [])
    ok, errs = cluster_driver.commit_summaries(store, items)
    _say(f"    summaries written: {ok}; errors: {len(errs)}")
    for e in errs:
        _say(f"    ! {e}")


def _analyze_cluster(store: Store, cid: int) -> list[str]:
    """Re-analyze one cluster headlessly and commit; returns validation errors
    (empty = committed). Reuses the canonical ANALYZE_PROMPT + commit/validate path,
    so a moved member re-dispositions the whole cluster consistently."""
    bundle = analyze_driver.bundle(store, cid, master=redundancy.MasterTree())
    bp = Path(f"/tmp/reingest-analyze-{cid}.json")
    bp.write_text(json.dumps(bundle))
    text = headless_agent.run_agent(
        ANALYZE_PROMPT.replace("__BUNDLE_PATH__", str(bp))
                      .replace("__BRANCH__", settings.default_branch()) + ANALYZE_FENCED_TAIL,
        allow_gh=True, cwd=str(REPO_ROOT), on_event=_agent_progress)
    payload = headless_agent.extract_json(text)
    return analyze_driver.commit_analysis(store, payload)


def _reformat_cluster(store: Store, cid: int) -> None:
    """Format a just-committed cluster rationale for app display. Derived,
    presentation-only: an unavailable model or a formatter failure must not undo
    the authoritative analysis."""
    try:
        c = store.load_cluster(cid)
        source = c.rationale if c is not None else None
        if not source:
            return
        formatted = reformat_rationales.reformat_one(source)
        store.edit_cluster(cid).record_reformat(formatted["body"], formatted["summary"])
    except Exception as exc:  # noqa: BLE001 — display polish, never fatal to triage
        _say(f"    ! rationale formatting unavailable; keeping raw display: {exc}")


def run(store: Store, pr: int) -> int:
    started = _now()
    rec = store.load_pr(pr)
    old_sha = rec.head_sha if rec is not None else None
    rc = 1
    try:
        if rec is None:
            _say(f"✗ PR #{pr} is not in the store (ingest it first, or `gh pr view {pr}`)")
        else:
            try:
                rc = _run(store, pr)
            except (RuntimeError, ValueError) as e:
                _say(f"✗ reingest failed: {e}")
    finally:
        after = store.load_pr(pr)
        new_sha = after.head_sha if after is not None else None
        store.append_run({
            "phase": "reingest",
            "pr": pr,
            "started": started,
            "finished": _now(),
            "stats": {
                "status": "ok" if rc == 0 else "error",
                "moved": old_sha != new_sha,
                "old_sha": old_sha,
                "new_sha": new_sha,
            },
        })
    return rc


def _run(store: Store, pr: int) -> int:
    _say(f"▶ Reingesting PR #{pr}")

    # 1. Refresh facts from GitHub (signals/CI/Greptile → signals+drift re-stamped
    #    at the live head). Deterministic; a moved head auto-stales summary/analysis.
    _say("① Refreshing the PR from GitHub…")
    r = ingest.refresh_prs(store, [pr])[0]
    old = (r["old_sha"] or "?")[:7]
    _say(f"    head {old}→{r['new_sha'][:7]} " + ("(changed)" if r["moved"] else "(unchanged)"))
    rec = store.load_pr(pr)
    assert rec is not None

    if rec.state != "open":
        _say(f"✓ PR #{pr} is {rec.state}; refreshed its meta, nothing to re-analyze.")
        return 0

    needs_summary = not is_current(rec, "summary")
    needs_analysis = not is_current(rec, "analysis")
    if not needs_summary and not needs_analysis:
        _say(f"✓ already current at {(rec.head_sha or '?')[:7]} — signals/drift refreshed; "
             f"summary + analysis unchanged. Nothing to reingest.")
        return 0

    # 2. The agentic tail reads the diff and must judge the current head, so make
    #    both the cached diff and the deterministic threat verdict track it.
    sha = rec.head_sha or ""
    _say("② Caching the diff for the current head…")
    diff_cache.fetch_diff(pr, sha, store=store)
    _say("③ Threat-rescanning the current head…")
    _threat_rescan(pr)

    # 3. Re-summarize if the summary went stale (mechanism-level, feeds ANALYZE).
    if needs_summary:
        _say("④ Re-summarizing the PR…")
        _resummarize(store, pr, sha, rec.title)
        rec = store.load_pr(pr)
        assert rec is not None

    # 4. Re-analyze if the disposition went stale: per-cluster for a clustered PR,
    #    deterministic for a confirmed standalone.
    if needs_analysis:
        cids = rec.cluster_ids
        if cids:
            _say(f"⑤ Re-analyzing cluster(s) {cids}…")
            for cid in cids:
                errs = _analyze_cluster(store, cid)
                if errs:
                    _say(f"✗ cluster {cid} analysis failed validation (left unchanged):")
                    for e in errs:
                        _say(f"    ! {e}")
                    return 1
                _reformat_cluster(store, cid)
            _say("    re-analyzed")
        else:
            _say("⑤ Re-dispositioning the standalone PR…")
            if rec.section("cluster") is not None:
                # A prior CLUSTER pass considered this PR and left it out of every
                # cluster (#116); reingest deliberately never re-clusters, so the
                # moved head just needs that same standalone verdict re-stamped —
                # otherwise disposition_orphans requires a current cluster stamp
                # and would silently skip this PR forever (#585).
                store.edit_pr(pr).mark_standalone()
            n = analyze_driver.disposition_orphans(store)
            _say(f"    standalone PRs dispositioned: {n}")

    # 5. Report which sections now track the current head.
    rec = store.load_pr(pr)
    assert rec is not None
    _say(f"✓ PR #{pr} reingested at {(rec.head_sha or '?')[:7]}:")
    for sec in ("signals", "drift", "summary", "analysis"):
        _say(f"    {sec}: {'current' if is_current(rec, sec) else 'stale/absent'}")
    _say(f"    disposition: {rec.disposition}")
    if rec.section("security") and not is_current(rec, "security"):
        _say("    ⚠ security verdict is now stale (head moved) — it does not block the "
             "human merge gate, but re-run SECURITY before the pipeline recommends merge.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--store", default=None)
    args = ap.parse_args(argv)
    store = Store(args.store) if args.store else Store()
    return run(store, args.pr)


if __name__ == "__main__":
    sys.exit(main())
