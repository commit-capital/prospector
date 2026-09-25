"""Build the issue-fix evaluation set — the past bugs a lane run can be scored
against, each qualified on a base built for its own period of history — and
run the lane over it.

The set is deployment data, so it lives in the store, not the tree: one
`eval-set:base` runs-ledger row per base (`record_base`), read back as the
manifest (`load_manifest`), the latest row per base winning.

A bug is a closed issue with a merged fixing PR — the PR GitHub records as
closing it, or a merged PR whose title or body says "fixes #N". The corpus is
harvested from GitHub, so it reaches issues closed before this deployment
watched the repository, and joined with the issue store's own candidates,
whose links reach further (an issue naming its PR, a fix the pipeline found);
the harvest is cached. The screen and the grouping are the replay's own
(`issue_fix_replay._screen`, `group_by_deps`).

Each dependency group gets one base, built at its epoch: the first commit after
the group's last fixing merge whose dependency declarations still match. The
base holds every fix in the group and equals none of them, so each bug's
pre-fix tree differs from it. The base is held against the verify sweep
(`verify_gc.hold`), its bugs are qualified without an agent
(`issue_fix_replay.qualify_instances`), and its row records every verdict. A
build resumes: a group whose epoch the manifest already names is skipped.

`run` replays every fair bug through a lane — the staged lane (`fix_lane`) or
the one-agent lane (`solo_lane`) — PASSES times, each pass its own replay run
id, and writes a scorecard: how many runs proposed a fix (ended
`fixed`), how many of those the hidden oracle accepted — precision — and how
many bugs got a proposal at all — coverage.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

from issue_triage import link_prs
from pipeline import gh, profile, prove, settings, storekit, verify_driver, verify_gc
from pipeline.evals import issue_fix_replay as replay
from pipeline.profile import RepoProfile
from pipeline.store import Store

# The ledger phases the set and its runs are recorded under.
BASE_PHASE = "eval-set:base"
RUN_PHASE = "eval-set:run"

# Ledger rows scanned for the set's bases, newest first.
_LEDGER_SCAN = 50_000

# Every eval base is built at tier 1, the tier the replay proves at.
TIER = 1

# Commits after a group's last merge searched for one with the group's
# dependency declarations.
EPOCH_SCAN = 40

# Page sizes for the harvest: issue bodies run long, so issue pages are smaller.
_ISSUE_PAGE = 50
_PR_PAGE = 100
_MAX_PAGES = 200

_ISSUES_QUERY = """query {{ repository(owner: "{owner}", name: "{name}") {{
  issues(states: CLOSED, first: {page}{after}, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ number stateReason title body createdAt lastEditedAt author {{ login }}
      timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {{ nodes {{ ... on ClosedEvent {{
        closer {{ __typename
          ... on PullRequest {{ number }}
          ... on Commit {{ associatedPullRequests(first: 1) {{ nodes {{ number }} }} }}
        }} }} }} }} }} }} }} }}"""

_PRS_QUERY = """query {{ repository(owner: "{owner}", name: "{name}") {{
  pullRequests(states: MERGED, first: {page}{after}, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ number title body createdAt author {{ login }} mergeCommit {{ oid }} }} }} }} }}"""


def _pages(template: str, key: str, page: int) -> list[dict]:
    """Every node of the repository connection `key`, paged through `template`."""
    owner, name = settings.repo().split("/", 1)
    nodes: list[dict] = []
    after = ""
    for _ in range(_MAX_PAGES):
        data = gh.gh_graphql(template.format(owner=owner, name=name, page=page, after=after),
                             timeout=120)
        conn = (((data or {}).get("data") or {}).get("repository") or {}).get(key)
        if not isinstance(conn, dict):
            raise RuntimeError(f"GitHub did not answer the {key} harvest")
        nodes.extend(n for n in conn.get("nodes") or [] if isinstance(n, dict))
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes
        after = f', after: "{info["endCursor"]}"'
    raise RuntimeError(f"the {key} harvest ran past {_MAX_PAGES} pages")


def _cache_path() -> Path:
    return settings.verify_scratch() / "replay" / "eval-harvest.json"


def harvest(*, refresh: bool = False) -> dict:
    """`{"harvested_at", "issues", "prs"}`: every closed issue and merged PR of
    the repository, raw, read from the cache unless `refresh` or it is absent."""
    path = _cache_path()
    if not refresh:
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    raw = {"harvested_at": storekit.now(),
           "issues": _pages(_ISSUES_QUERY, "issues", _ISSUE_PAGE),
           "prs": _pages(_PRS_QUERY, "pullRequests", _PR_PAGE)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw))
    return raw


def _closer(issue: dict) -> int | None:
    """The PR number GitHub records as closing `issue`, directly or through a
    commit that belongs to one."""
    events = (issue.get("timelineItems") or {}).get("nodes") or []
    closer = (events[-1] if events else {}).get("closer") or {}
    if closer.get("__typename") == "PullRequest":
        return closer.get("number")
    prs = (closer.get("associatedPullRequests") or {}).get("nodes") or []
    return prs[0].get("number") if prs else None


def candidates(raw: dict) -> list[replay.Candidate]:
    """The harvested closed issues that name at least one merged fixing PR, as
    screening candidates."""
    merged: dict[int, dict] = {}
    for pr in raw["prs"]:
        opened = replay._parse_dt(pr.get("createdAt"))
        sha = (pr.get("mergeCommit") or {}).get("oid")
        if opened is not None and sha:
            merged[pr["number"]] = {"sha": sha, "opened": opened,
                                    "author": (pr.get("author") or {}).get("login") or "",
                                    "text": f"{pr.get('title') or ''}\n{pr.get('body') or ''}"}
    fixers: dict[int, set[int]] = {}
    for number, pr in merged.items():
        for issue in link_prs.parse_issue_refs(pr["text"]):
            fixers.setdefault(issue, set()).add(number)
    out: list[replay.Candidate] = []
    for issue in raw["issues"]:
        n = issue["number"]
        prs = set(fixers.get(n, set()))
        closer = _closer(issue)
        if closer in merged:
            prs.add(closer)
        created = replay._parse_dt(issue.get("createdAt"))
        if not prs or created is None:
            continue
        edited = replay._parse_dt(issue.get("lastEditedAt")) or created
        out.append(replay.Candidate(
            issue=n, closed_completed=issue.get("stateReason") == "COMPLETED",
            closing_prs=[replay.ClosingPr(number=p, merged=True, merge_sha=merged[p]["sha"],
                                          author=merged[p]["author"],
                                          opened_at=merged[p]["opened"])
                         for p in sorted(prs)],
            reporter=(issue.get("author") or {}).get("login") or "",
            created_at=created, updated_at=edited,
            report_title=issue.get("title") or "", report_body=issue.get("body") or ""))
    return out


def union(*sources: list[replay.Candidate]) -> list[replay.Candidate]:
    """One candidate per issue across `sources`: the first source's report and
    dates, and every source's merged fixing PRs."""
    by_issue: dict[int, replay.Candidate] = {}
    for source in sources:
        for cand in source:
            prior = by_issue.get(cand.issue)
            if prior is None:
                by_issue[cand.issue] = cand
                continue
            prs = {p.number: p for p in cand.closing_prs} | {p.number: p for p in prior.closing_prs}
            by_issue[cand.issue] = replace(prior, closing_prs=[prs[n] for n in sorted(prs)])
    return [by_issue[n] for n in sorted(by_issue)]


def epoch(clone: Path, members: list[replay.Instance], key: frozenset[tuple[str, str]],
          head: str, prof: RepoProfile) -> str:
    """The commit to build `members`' base at: the first on the first-parent
    line after their last merge whose dependency declarations equal `key`, or
    that last merge when none within EPOCH_SCAN does."""
    line = replay._git(clone, "rev-list", "--first-parent", head).split()
    merges = {m.merge_sha for m in members}
    last = next((sha for sha in line if sha in merges), members[-1].merge_sha)
    after = replay._git(clone, "rev-list", "--first-parent", "--reverse",
                        f"{last}..{head}").split()
    for sha in after[:EPOCH_SCAN]:
        if frozenset(replay.dep_declarations(clone, sha, prof).items()) == key:
            return sha
    return last


def load_manifest(pr_store: Store | None = None) -> dict:
    """`{"bases": [...], "fair": n}`: the set's bases for this repository, from
    the ledger, oldest first, each base's latest row winning."""
    pr_store = pr_store or Store()
    bases: dict[str, dict] = {}
    for rec in pr_store.runs(limit=_LEDGER_SCAN):
        if not isinstance(rec, storekit.PhaseRun) or rec.phase != BASE_PHASE:
            continue
        stats = rec.raw.get("stats") or {}
        if stats.get("repo") == settings.repo() and stats.get("base_sha"):
            bases.pop(stats["base_sha"], None)
            bases[stats["base_sha"]] = stats
    ordered = list(bases.values())
    return {"bases": ordered, "fair": sum(1 for b in ordered for i in b["instances"]
                                          if i["verdict"] == "fair")}


def record_base(base: dict, pr_store: Store | None = None) -> None:
    """Append one base of the set — `{base_sha, span, fixes, qualified_at,
    instances}` — to the ledger."""
    stats = {**base, "repo": settings.repo(), "tier": TIER}
    (pr_store or Store()).append_run({
        "phase": BASE_PHASE, "started": base.get("qualified_at") or storekit.now(),
        "finished": storekit.now(), "trigger": "cli", "stats": stats})


def _fixes(members: list[replay.Instance]) -> int:
    return len({m.pr for m in members})


def _screened(prof: RepoProfile, pin: prove.PinnedBase, *, refresh: bool
              ) -> list[replay.Instance]:
    """Every harvested and stored candidate that passes the replay's screen."""
    raw = harvest(refresh=refresh)
    cands = union(candidates(raw), replay.candidates_from_store(limit=None))
    instances, discards = replay._screen(cands, pin.clone, pin.sha, profile=prof,
                                         lane_logins=replay._lane_logins())
    print(f"harvested {len(raw['issues'])} closed issues and {len(raw['prs'])} merged PRs; "
          f"{len(cands)} name a fix with the store's, {len(instances)} pass the screen "
          f"({', '.join(f'{r} {c}' for r, c in discards.most_common())})", flush=True)
    return instances


def _held_base(sha: str) -> prove.PinnedBase:
    """The base at `sha`, held, built first when this machine lacks it."""
    verify_gc.hold(sha)
    try:
        return prove.held(sha, TIER)
    except prove.NoBase:
        verify_driver.build_base_image(sha, tier=TIER)
        return prove.held(sha, TIER)


def build(*, target: int, min_fixes: int, refresh: bool, concurrency: int,
          dry_run: bool) -> int:
    """Add bases to the set, most distinct fixes first, until it holds
    `target` fair bugs or no group of at least `min_fixes` fixes is left.
    `dry_run` prints each group's plan and builds nothing."""
    prof = profile.active()
    pin = prove.pinned(Store())
    instances = _screened(prof, pin, refresh=refresh)
    groups = replay.group_by_deps(instances, pin.clone, prof)

    manifest = load_manifest()
    built = {b["base_sha"] for b in manifest["bases"]}
    fair = sum(1 for b in manifest["bases"] for i in b["instances"] if i["verdict"] == "fair")
    ordered = sorted(groups.items(), key=lambda kv: (_fixes(kv[1]), len(kv[1])), reverse=True)
    for key, members in ordered:
        if fair >= target or _fixes(members) < min_fixes:
            break
        sha = epoch(pin.clone, members, key, pin.sha, prof)
        span = replay._date_span(pin.clone, members)
        print(f"group {span}: {_fixes(members)} fix(es), epoch {sha[:12]}"
              f"{' (in the manifest)' if sha in built else ''}", flush=True)
        if sha in built or dry_run:
            continue
        try:
            base = _held_base(sha)
        except (verify_driver.BuildFailure, prove.NoBase) as e:
            print(f"  base {sha[:12]} did not build: {str(e)[-300:]}", flush=True)
            continue
        rows = replay.qualify_instances(base, sha, members, profile=prof,
                                        concurrency=concurrency)
        record_base({
            "base_sha": sha, "span": span, "fixes": _fixes(members),
            "qualified_at": storekit.now(),
            "instances": [{"issue": q.issue, "pr": q.pr, "verdict": q.verdict,
                           "detail": q.detail} for q in rows]})
        built.add(sha)
        gained = sum(1 for q in rows if q.verdict == "fair")
        fair += gained
        print(f"  {gained} fair of {len(rows)}; the set holds {fair}", flush=True)
    print(f"the evaluation set holds {fair} fair bug(s) over {len(built)} base(s)",
          flush=True)
    return 0


def import_file(path: Path) -> int:
    """Record every base a manifest file names that the ledger lacks."""
    have = {b["base_sha"] for b in load_manifest()["bases"]}
    bases = json.loads(path.read_text())["bases"]
    for base in bases:
        if base["base_sha"] not in have:
            record_base(base)
    print(f"recorded {sum(1 for b in bases if b['base_sha'] not in have)} of "
          f"{len(bases)} base(s)", flush=True)
    return 0


def _correct(rec: dict) -> bool:
    """A proposal the hidden oracle accepts, or refuses only on a contract the
    maintainers chose and the report never stated."""
    return rec.get("ending") == "fixed" and bool(
        rec.get("oracle_pass") or rec.get("contract_mismatch"))


def scorecard(records: list[dict], *, bugs: int) -> dict:
    """The run's numbers over every scored pass of `bugs` bugs: proposals
    (runs ending `fixed`), the correct ones among them, precision and coverage,
    the endings, and cost. A fault ending is the machine's, so it is counted
    apart and scores nothing."""
    scored = [r for r in records if not replay._is_fault(r.get("ending"))]
    proposed = [r for r in scored if r.get("ending") == "fixed"]
    correct = [r for r in proposed if _correct(r)]
    proposed_bugs = {r["issue"] for r in proposed}
    return {
        "bugs": bugs, "runs": len(records), "scored": len(scored),
        "faults": len(records) - len(scored),
        "proposed": len(proposed), "correct": len(correct),
        "precision": round(len(correct) / len(proposed), 3) if proposed else None,
        "coverage": round(len(proposed_bugs) / bugs, 3) if bugs else None,
        "bugs_proposed": len(proposed_bugs),
        "bugs_correct": len({r["issue"] for r in correct}),
        "endings": dict(Counter(str(r.get("ending")) for r in records).most_common()),
        "agent_runs": sum(r.get("agent_runs") or 0 for r in records),
        "avg_seconds": round(replay._avg_seconds(records), 1),
    }


def _scorecard_md(name: str, card: dict, records: list[dict]) -> str:
    def pct(x: float | None) -> str:
        return "-" if x is None else f"{x:.0%}"

    lines = [f"# Evaluation run `{name}`", "",
             f"{card['bugs']} bugs, {card['runs']} runs ({card['faults']} machine faults).", "",
             "| measure | value |", "| --- | --- |",
             f"| proposed | {card['proposed']} of {card['scored']} scored runs |",
             f"| correct | {card['correct']} |",
             f"| precision | {pct(card['precision'])} |",
             f"| coverage | {pct(card['coverage'])} ({card['bugs_proposed']} bugs) |",
             f"| agent runs | {card['agent_runs']} |",
             f"| avg lane seconds | {card['avg_seconds']} |", "",
             "| ending | runs |", "| --- | --- |"]
    lines += [f"| {e} | {n} |" for e, n in card["endings"].items()]
    by_bug: dict[int, list[dict]] = {}
    for rec in records:
        by_bug.setdefault(rec["issue"], []).append(rec)
    lines += ["", "| issue | endings by pass | correct |", "| --- | --- | --- |"]
    for issue in sorted(by_bug):
        recs = sorted(by_bug[issue], key=lambda r: r.get("pass") or 0)
        lines.append(f"| {issue} | {', '.join(str(r.get('ending')) for r in recs)} | "
                     f"{sum(1 for r in recs if _correct(r))} |")
    return "\n".join(lines) + "\n"


# The lanes a run can measure, by name: the staged lane, the one-agent lane, and
# the cross-tested lane.
LANES: dict[str, replay.LaneEntry] = {"staged": replay._run_lane, "solo": replay._run_solo,
                                      "cross": replay._run_cross}


def run(*, name: str, passes: int, concurrency: int, refresh: bool,
        issues: set[int] | None, resume: bool, lane: str = "staged") -> int:
    """Replay every fair bug of the set through the lane `passes` times, pass by
    pass, each pass recorded as replay run `<name>-p<k>`. Re-invoking continues
    a run: recorded instances are kept, and `resume` re-runs the faulted ones.
    Writes `<verify scratch>/replay/<name>/scorecard.md` and one `eval-set:run`
    ledger row."""
    prof = profile.active()
    pr_store = Store()
    manifest = load_manifest(pr_store)
    fair = {i["issue"]: b["base_sha"] for b in manifest["bases"] for i in b["instances"]
            if i["verdict"] == "fair" and (issues is None or i["issue"] in issues)}
    if not fair:
        print("the evaluation set holds no fair bug to run", file=sys.stderr)
        return 2
    pin = prove.pinned(pr_store)
    by_issue = {inst.issue: inst for inst in _screened(prof, pin, refresh=refresh)
                if inst.issue in fair}
    missing = sorted(set(fair) - set(by_issue))
    if missing:
        print(f"no longer screened in, left out: {missing}", flush=True)
    bases = {sha: _held_base(sha) for sha in sorted({fair[i] for i in by_issue})}

    out_dir = settings.verify_scratch() / "replay" / name
    started = storekit.now()
    results: list[dict] = []
    jobs: list[tuple[int, replay.Instance]] = []
    for k in range(1, passes + 1):
        recorded = replay._recorded(pr_store, f"{name}-p{k}")
        for issue in sorted(by_issue):
            prior = recorded.get(issue)
            if prior is not None and not (resume and replay._is_fault(prior.get("ending"))):
                results.append({**prior, "pass": k})
            else:
                jobs.append((k, by_issue[issue]))
    print(f"run {name}: {len(by_issue)} bugs x {passes} passes; {len(jobs)} to run, "
          f"{len(results)} already recorded", flush=True)

    # Set by the first run whose agent can serve nothing (a spent usage limit,
    # a lost login): every run after it would fail the same way, so a job that
    # starts once it is set does nothing, and is left for a --resume.
    unavailable = threading.Event()

    def one(k: int, inst: replay.Instance) -> dict | None:
        if unavailable.is_set():
            return None
        base = bases[fair[inst.issue]]
        rec = replay.run_instance(
            inst, base=base, base_sha=base.sha, profile=prof,
            workdir=out_dir / f"p{k}" / f"issue-{inst.issue}", run_lane=LANES[lane],
            judge_contract=replay._judge_contract)
        if rec.get("ending") == "agent-unavailable":
            unavailable.set()
        return rec

    done = 0
    halted = ""
    left = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(one, k, inst): (k, inst) for k, inst in jobs}
        for fut in as_completed(futures):
            k, inst = futures[fut]
            try:
                got = fut.result()
            except Exception:
                print(f"issue {inst.issue} pass {k} crashed:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                got = {"issue": inst.issue, "pr": inst.pr, "ending": replay._CRASH_ENDING,
                       "seconds": 0.0, "agent_runs": 0}
            if got is None:
                left += 1
                continue
            rec = got
            rec["pass"] = k
            results.append(rec)
            done += 1
            pr_store.append_run({
                "phase": "replay:instance", "started": started, "finished": storekit.now(),
                "trigger": "cli",
                "stats": {**replay._instance_stats(rec, run_id=f"{name}-p{k}",
                                                   base_sha=bases[fair[inst.issue]].sha),
                          "eval": name, "lane": lane, "pass": k}})
            print(f"[{done}/{len(jobs)}] issue {inst.issue} pass {k}: {rec.get('ending')} "
                  f"({replay._cell(rec.get('seconds'))}s) {rec.get('detail') or ''}"[:300],
                  flush=True)
            if rec.get("ending") == "agent-unavailable" and not halted:
                halted = str(rec.get("detail") or "the agent is unavailable")
                print(f"halting: {halted}", flush=True)
    if halted:
        print(f"{left} run(s) left for --resume", flush=True)

    card = {**scorecard(results, bugs=len(by_issue)), "halted": halted or None}
    out_dir.mkdir(parents=True, exist_ok=True)
    md = _scorecard_md(name, card, results)
    (out_dir / "scorecard.md").write_text(md)
    print(md, flush=True)
    pr_store.append_run({"phase": RUN_PHASE, "started": started, "finished": storekit.now(),
                         "trigger": "cli",
                         "stats": {"eval": name, "lane": lane, "passes": passes,
                                   "concurrency": concurrency,
                                   "host": settings.worker_id(), **card}})
    return 3 if halted else 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.evals.eval_set",
        description="Build the issue-fix evaluation set, and run the lane over it.")
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="harvest past bugs, build a held base per period, "
                                     "and qualify each bug on it without an agent")
    b.add_argument("--target", type=int, default=40, help="fair bugs to stop at")
    b.add_argument("--min-fixes", type=int, default=2,
                   help="skip dependency groups with fewer distinct fixes")
    b.add_argument("--refresh", action="store_true", help="re-harvest from GitHub")
    b.add_argument("--concurrency", type=int, default=2, help="R6 runs in flight")
    b.add_argument("--dry-run", action="store_true",
                   help="print each group's plan and build nothing")
    r = sub.add_parser("run", help="replay every fair bug through the lane and score it")
    r.add_argument("--name", required=True, help="the run's name; passes are <name>-p<k>")
    r.add_argument("--passes", type=int, default=3)
    r.add_argument("--concurrency", type=int, default=3, help="instances in flight")
    r.add_argument("--issues", help="run only these issue numbers, comma-separated")
    r.add_argument("--resume", action="store_true", help="re-run the faulted instances")
    r.add_argument("--refresh", action="store_true", help="re-harvest from GitHub")
    r.add_argument("--lane", choices=sorted(LANES), default="staged",
                   help="the staged, one-agent, or cross-tested lane")
    i = sub.add_parser("import", help="record the bases a manifest file names")
    i.add_argument("path", type=Path)
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "import":
            return import_file(args.path)
        if args.command == "run":
            issues = ({int(n) for n in args.issues.split(",") if n.strip()}
                      if args.issues else None)
            return run(name=args.name, passes=args.passes, concurrency=args.concurrency,
                       refresh=args.refresh, issues=issues, resume=args.resume,
                       lane=args.lane)
        return build(target=args.target, min_fixes=args.min_fixes, refresh=args.refresh,
                     concurrency=args.concurrency, dry_run=args.dry_run)
    except prove.NoBase as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
