"""Build the issue-fix evaluation set: the past bugs a lane run can be scored
against, each qualified on a base built for its own period of history, frozen
as a manifest (`MANIFEST`) the replay runs against.

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
(`issue_fix_replay.qualify_instances`), and the manifest records every verdict.
A build resumes: a group whose epoch the manifest already names is skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from issue_triage import link_prs
from pipeline import gh, profile, prove, settings, storekit, verify_driver, verify_gc
from pipeline.evals import issue_fix_replay as replay
from pipeline.profile import RepoProfile
from pipeline.store import Store

MANIFEST = Path(__file__).resolve().parent / "data" / "issue_fix_eval_set.json"

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


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {"version": 1, "repo": settings.repo(), "tier": TIER, "bases": []}


def _save_manifest(manifest: dict) -> None:
    manifest["fair"] = sum(1 for b in manifest["bases"] for i in b["instances"]
                           if i["verdict"] == "fair")
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n")


def _fixes(members: list[replay.Instance]) -> int:
    return len({m.pr for m in members})


def build(*, target: int, min_fixes: int, refresh: bool, concurrency: int,
          dry_run: bool) -> int:
    """Add bases to the manifest, most distinct fixes first, until it holds
    `target` fair bugs or no group of at least `min_fixes` fixes is left.
    `dry_run` prints each group's plan and builds nothing."""
    prof = profile.active()
    pin = prove.pinned(Store())
    raw = harvest(refresh=refresh)
    cands = union(candidates(raw), replay.candidates_from_store(limit=None))
    instances, discards = replay._screen(cands, pin.clone, pin.sha, profile=prof,
                                         lane_logins=replay._lane_logins())
    groups = replay.group_by_deps(instances, pin.clone, prof)
    print(f"harvested {len(raw['issues'])} closed issues and {len(raw['prs'])} merged PRs; "
          f"{len(cands)} name a fix with the store's, {len(instances)} pass the screen "
          f"({', '.join(f'{r} {c}' for r, c in discards.most_common())})", flush=True)

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
        verify_gc.hold(sha)
        try:
            base = prove.held(sha, TIER)
        except prove.NoBase:
            try:
                verify_driver.build_base_image(sha, tier=TIER)
                base = prove.held(sha, TIER)
            except (verify_driver.BuildFailure, prove.NoBase) as e:
                print(f"  base {sha[:12]} did not build: {str(e)[-300:]}", flush=True)
                continue
        rows = replay.qualify_instances(base, sha, members, profile=prof,
                                        concurrency=concurrency)
        manifest["bases"].append({
            "base_sha": sha, "span": span, "fixes": _fixes(members),
            "qualified_at": storekit.now(),
            "instances": [{"issue": q.issue, "pr": q.pr, "verdict": q.verdict,
                           "detail": q.detail} for q in rows]})
        _save_manifest(manifest)
        built.add(sha)
        gained = sum(1 for q in rows if q.verdict == "fair")
        fair += gained
        print(f"  {gained} fair of {len(rows)}; the set holds {fair}", flush=True)
    print(f"the evaluation set holds {fair} fair bug(s) over "
          f"{len(manifest['bases'])} base(s): {MANIFEST}", flush=True)
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="python -m pipeline.evals.eval_set",
        description="Build the issue-fix evaluation set: harvest past bugs, build a held "
                    "base per period, and qualify each bug on it without an agent.")
    ap.add_argument("--target", type=int, default=40, help="fair bugs to stop at")
    ap.add_argument("--min-fixes", type=int, default=2,
                    help="skip dependency groups with fewer distinct fixes")
    ap.add_argument("--refresh", action="store_true", help="re-harvest from GitHub")
    ap.add_argument("--concurrency", type=int, default=2, help="R6 runs in flight")
    ap.add_argument("--dry-run", action="store_true",
                    help="print each group's plan and build nothing")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        return build(target=args.target, min_fixes=args.min_fixes, refresh=args.refresh,
                     concurrency=args.concurrency, dry_run=args.dry_run)
    except prove.NoBase as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
