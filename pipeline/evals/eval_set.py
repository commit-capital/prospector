"""Build the issue-fix evaluation set — the past bugs a lane run can be scored
against, each qualified on a base built for its own period of history — and
run the lane over it.

The set is deployment data, so it lives in the store, not the tree: one
`eval-set:base` runs-ledger row per base (`record_base`), read back as the
manifest (`load_manifest`), the latest row per base winning. A base counts
only under the sandbox commands it was qualified with (`sandbox_stamp`): its
tree is history's, but the commands are today's profile's.

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
import hashlib
import json
import subprocess
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Literal, NamedTuple

from issue_triage import link_prs
from issue_triage.issue_store import IssueStore
from pipeline import (diffpaths, gates, gh, profile, prove, settings, storekit, verify_driver,
                      verify_gc)
from pipeline.evals import issue_fix_replay as replay
from pipeline.profile import RepoProfile
from pipeline.store import Store

# The ledger phases the set and its runs are recorded under.
BASE_PHASE = "eval-set:base"
RUN_PHASE = "eval-set:run"
CASE_PHASE = "eval-set:case"

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
        opened = storekit.parse_ts(pr.get("createdAt"))
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
        created = storekit.parse_ts(issue.get("createdAt"))
        if not prs or created is None:
            continue
        edited = storekit.parse_ts(issue.get("lastEditedAt")) or created
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


def sandbox_stamp(prof: RepoProfile) -> str:
    """A hash of the sandbox commands `prof` runs a base under: the `verify`
    section but `e2e`, which no replay runs, so any other edit there sends every
    base back to `build`."""
    verify = asdict(prof.verify)
    del verify["e2e"]
    raw = json.dumps(verify, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def load_manifest(pr_store: Store | None = None) -> dict:
    """`{"bases": [...], "fair": n, "stale": [sha, ...]}`: the set's bases for
    this repository, from the ledger, oldest first, each base's latest row
    winning. `bases` and `fair` hold those qualified under today's sandbox
    commands; `stale` names the rest, which `build` requalifies."""
    pr_store = pr_store or Store()
    bases: dict[str, dict] = {}
    for rec in pr_store.runs(limit=_LEDGER_SCAN, phase=BASE_PHASE):
        if not isinstance(rec, storekit.PhaseRun):
            continue
        stats = rec.raw.get("stats") or {}
        if stats.get("repo") == settings.repo() and stats.get("base_sha"):
            bases.pop(stats["base_sha"], None)
            bases[stats["base_sha"]] = stats
    stamp = sandbox_stamp(profile.active())
    ordered = [b for b in bases.values() if b.get("sandbox") == stamp]
    return {"bases": ordered, "fair": sum(1 for b in ordered for i in b["instances"]
                                          if i["verdict"] == "fair"),
            "stale": [sha for sha, b in bases.items() if b.get("sandbox") != stamp]}


def record_base(base: dict, pr_store: Store | None = None) -> None:
    """Append one base of the set — `{base_sha, span, fixes, qualified_at,
    sandbox, instances}` — to the ledger."""
    stats = {**base, "repo": settings.repo(), "tier": TIER}
    (pr_store or Store()).append_run({
        "phase": BASE_PHASE, "started": base.get("qualified_at") or storekit.now(),
        "finished": storekit.now(), "trigger": "cli", "stats": stats})


@dataclass(frozen=True)
class Case:
    """A bug the set takes from production or from an operator, not from history:
    `rival` when another author's PR beat our fix, `case-study` when an operator
    names it. The reference is the PR's merged fix, made on `merge_sha^1`, or an
    open PR's diff at `head_sha`, judged on the pin (`pre_sha`, `merge_sha` empty):
    an open PR's fork point is older than the pin, and its dependencies rarely
    still match the image's. It carries
    everything a replay needs, so it is never re-screened. `oracle` is `tests`
    when a merged PR's tests qualify as the oracle on `base_sha`, `craft-only`
    when the pre-fix tree's patch applies but there is no such oracle, and
    `unusable` (with `detail`) otherwise."""
    case: str
    source: Literal["rival", "case-study"]
    issue: int
    pr: int
    merge_sha: str
    base_sha: str
    oracle: Literal["tests", "craft-only", "unusable"]
    detail: str
    report_title: str
    report_body: str
    landed_diff: str
    test_files: list[str]
    nontest_files: list[str]
    pre_sha: str = ""
    head_sha: str = ""

    def instance(self) -> replay.Instance:
        return replay.Instance(issue=self.issue, pr=self.pr, merge_sha=self.merge_sha,
                               landed_diff=self.landed_diff, test_files=list(self.test_files),
                               nontest_files=list(self.nontest_files),
                               report_title=self.report_title, report_body=self.report_body,
                               pre_sha=self.pre_sha)


def record_case(case: Case, pr_store: Store | None = None) -> None:
    now = storekit.now()
    (pr_store or Store()).append_run({"phase": CASE_PHASE, "started": now, "finished": now,
                                      "trigger": "cli",
                                      "stats": {**asdict(case), "repo": settings.repo()}})


def load_cases(pr_store: Store | None = None) -> list[Case]:
    """This repository's cases, the latest row per case, by case id."""
    names = {f.name for f in fields(Case)}
    cases: dict[str, Case] = {}
    for rec in (pr_store or Store()).runs(limit=_LEDGER_SCAN, phase=CASE_PHASE):
        stats = (rec.raw.get("stats") or {}) if isinstance(rec, storekit.PhaseRun) else {}
        if stats.get("repo") == settings.repo() and stats.get("case"):
            cases.pop(stats["case"], None)
            cases[stats["case"]] = Case(**{k: v for k, v in stats.items() if k in names})
    return sorted(cases.values(), key=lambda c: c.case)


# Second-opinion verdicts saying a rival's tests catch what our fix misses.
_GAPS = ("gap", "gap-open")


def rival_candidates(issues: IssueStore) -> list[tuple[int, int]]:
    """`(issue, pr)` for each issue whose operator went with another author's PR
    (`fix_choice`) or whose fix a rival's tests found gaps in, sorted."""
    pairs: set[tuple[int, int]] = set()
    for row, (choice, opinions) in issues.issue_rows([("fix_choice",),
                                                      ("fix_run", "second_opinion")]):
        if isinstance(choice, dict) and isinstance(choice.get("pr"), int):
            pairs.add((row["issue"], choice["pr"]))
        for o in opinions or []:
            if isinstance(o, dict) and o.get("verdict") in _GAPS and isinstance(o.get("pr"), int):
                pairs.add((row["issue"], o["pr"]))
    return sorted(pairs)


class OpenPr(NamedTuple):
    head: str
    diff: str


def _open_rival(pr: int) -> OpenPr | str | None:
    """An open PR's diff at its current head, read live as the operator; None
    when it is not open, and why when GitHub refuses a read (a diff too large
    for it included)."""
    raw = gh.fetch_pr(pr)
    if raw is None:
        return f"GitHub refused #{pr}"
    if raw.get("state") != "open":
        return None
    head = raw["head"]["sha"]
    diff = gh.gh_bytes(f"repos/{settings.repo()}/compare/{raw['base']['sha']}...{head}",
                       accept="application/vnd.github.diff")
    if diff is None:
        return f"GitHub refused #{pr}'s diff"
    return OpenPr(head=head, diff=diff.decode("utf-8", "replace"))


def qualify_case(source: Literal["rival", "case-study"], issue: int, pr: int, *,
                 pin: prove.PinnedBase, report: tuple[str, str], prof: RepoProfile,
                 run_r6: bool) -> Case:
    """`issue` with `pr`'s fix as its reference, on the tree before it
    (`pre_patch` over the pin): a merged PR's tree just before it landed, or the
    pin itself for an open PR. `tests` when a merged fix's own tests pass R6
    there, `craft-only` when that tree's `pre_patch` (and an open PR's diff)
    applies and there is no such oracle (an open PR's tests are unaccepted, so
    never one), else `unusable` with why: no report text, neither merged nor
    open, not in the pin, dependencies drifted from the pin's (the image would
    not install the old tree faithfully), or an R6 sandbox fault (re-harvest to
    retry). Without `run_r6` a case with tests is taken as `tests` unproven."""
    case = Case(case=f"{'rival' if source == 'rival' else 'case'}-{issue}-{pr}", source=source,
                issue=issue, pr=pr, merge_sha="", base_sha=pin.sha, oracle="unusable",
                detail="", report_title=report[0], report_body=report[1], landed_diff="",
                test_files=[], nontest_files=[])
    if not (report[0].strip() or report[1].strip()):
        return replace(case, detail="no report text for the issue")
    merged = replay._gh_pr(pr)
    opened = None if merged else _open_rival(pr)
    if isinstance(opened, str):
        return replace(case, detail=f"{opened}; re-harvest to retry")
    if merged:
        case = replace(case, merge_sha=merged["merge_sha"])
    elif opened:
        case = replace(case, pre_sha=pin.sha, head_sha=opened.head, detail=f"#{pr} is open")
    else:
        return replace(case, detail=f"#{pr} is neither merged nor open")
    pre_fix = case.instance().pre_fix
    try:
        landed = (opened.diff if opened else
                  replay._git(pin.clone, "diff", "--binary", pre_fix, case.merge_sha))
        pre_patch = replay.transform_to_p(pin.clone, pre_fix, pin.sha, prof)
        before = replay.dep_declarations(pin.clone, pre_fix, prof)
        now = replay.dep_declarations(pin.clone, pin.sha, prof)
    except subprocess.CalledProcessError as e:
        return replace(case, detail=f"not-in-pin: {(e.stderr or str(e)).strip()[:200]}")
    if before != now:
        drifted = sorted(k for k in set(before) | set(now) if before.get(k) != now.get(k))
        return replace(case, detail=f"deps drifted from the pin's: {', '.join(drifted[:5])}")
    paths = diffpaths.changed_paths(landed)
    case = replace(case, landed_diff=landed,
                   test_files=[p for p in paths if diffpaths.is_test_path(p)],
                   nontest_files=[p for p in paths if not diffpaths.is_test_path(p)])
    if case.test_files and merged:
        if not run_r6:
            return replace(case, oracle="tests", detail="R6 not run (dry run)")
        ok, reason = replay.validate_known_fix(pin, pre_patch, case.instance(),
                                               label=f"case-{issue}")
        if ok:
            return replace(case, oracle="tests")
        if reason == "sandbox":
            return replace(case, detail="R6 hit a sandbox fault; re-harvest to retry")
        case = replace(case, detail=f"its tests do not qualify: r6-{reason}")
    try:
        prove.flatten(pin.clone, pre_patch, *([landed] if opened else []), label=f"case-{issue}")
    except ValueError as e:
        return replace(case, oracle="unusable", detail=f"does not apply to the pin: {e}"[:300])
    return replace(case, oracle="craft-only")


def _case_inputs() -> tuple[prove.PinnedBase, IssueStore,
                            Callable[[int], tuple[str, str] | None]]:
    """This machine's pin, the issue store, and the report reader."""
    from issue_triage import fix_lane
    issues = IssueStore()
    return prove.pinned(Store()), issues, lambda n: fix_lane.reported_text(issues, n)


def _qualify_all(source: Literal["rival", "case-study"], pairs: list[tuple[int, int]], *,
                 dry_run: bool, pin: prove.PinnedBase,
                 reported: Callable[[int], tuple[str, str] | None]) -> int:
    """Qualify `pairs` on the tier-1 base at the pin (the tier `run` replays at;
    a dry run reads the pin as it is and builds nothing) and, unless `dry_run`,
    record each case that changed since its last row, holding the base when a
    usable one was recorded."""
    prof = profile.active()
    base = pin if dry_run else _held_base(pin.sha)
    last = {c.case: c for c in load_cases()}
    usable = 0
    for issue, pr in pairs:
        case = qualify_case(source, issue, pr, pin=base, report=reported(issue) or ("", ""),
                            prof=prof, run_r6=not dry_run)
        print(f"{case.case}: {case.oracle}" + (f" ({case.detail})" if case.detail else ""),
              flush=True)
        if dry_run or case == last.get(case.case):
            continue
        record_case(case)
        usable += case.oracle != "unusable"
    if usable:
        verify_gc.hold(base.sha)
    print(f"{len(pairs)} case(s)" + (" (dry run, nothing recorded)" if dry_run else ""))
    return 0


def harvest_cases(*, dry_run: bool) -> int:
    """Qualify every rival case the issue store names and, unless `dry_run`,
    record each (unusable ones too, with why) and hold the base they run on."""
    pin, issues, reported = _case_inputs()
    return _qualify_all("rival", rival_candidates(issues), dry_run=dry_run, pin=pin,
                        reported=reported)


def add_case(issue: int, pr: int, *, dry_run: bool) -> int:
    """Qualify and record one case an operator names: `issue` with `pr`'s fix
    as the reference."""
    pin, _, reported = _case_inputs()
    return _qualify_all("case-study", [(issue, pr)], dry_run=dry_run, pin=pin,
                        reported=reported)


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


# `docker run`'s own exits: the daemon failed, or the container's command could
# not be invoked or found. The sandbox reports a failing compile as its sentinel.
_LAUNCH_FAULTS = frozenset({125, 126, 127})


def _compile_failure(base: prove.PinnedBase, prof: RepoProfile) -> str | None:
    """Why the profile's compile command fails over `base`'s pristine tree, or
    None when it passes or there is none: a base that fails it fails every
    lane run on it before a fix is judged, a timeout or an out-of-memory kill
    included. Raises `prove.SandboxFault` when docker could not start the
    container."""
    cmd = prof.verify.compile_cmd
    if not cmd:
        return None
    exit_code, tail = verify_driver.run_phase(
        "compile", base.image, tier=base.tier, test_cmd=cmd, base_sha=base.sha,
        head_sha="pristine", pristine=True)
    if exit_code == gates.SENTINEL_PASS:
        return None
    excerpt = verify_driver.error_excerpt(tail)
    if exit_code in _LAUNCH_FAULTS:
        raise prove.SandboxFault(f"the compile phase exited {exit_code}: {excerpt}")
    return f"exit {exit_code}" + (f": {excerpt}" if excerpt else "")


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
        try:
            broken = _compile_failure(base, prof)
        except prove.SandboxFault as e:
            print(f"  base {sha[:12]} could not be checked: {e}", flush=True)
            continue
        if broken:
            print(f"  base {sha[:12]} fails the compile command: {broken[:300]}", flush=True)
            rows = [replay.Qualification(m.issue, m.pr, "base-compile", 0.0, broken[:300])
                    for m in members]
        else:
            rows = replay.qualify_instances(base, sha, members, profile=prof,
                                            concurrency=concurrency)
        record_base({
            "base_sha": sha, "span": span, "fixes": _fixes(members),
            "qualified_at": storekit.now(), "sandbox": sandbox_stamp(prof),
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
    """Record every base a manifest file names that the ledger lacks, or holds
    only stale where the file's row is current."""
    manifest = load_manifest()
    stamp = sandbox_stamp(profile.active())
    current = {b["base_sha"] for b in manifest["bases"]}
    stale = set(manifest["stale"])
    new = [b for b in json.loads(path.read_text())["bases"]
           if b["base_sha"] not in current
           and (b["base_sha"] not in stale or b.get("sandbox") == stamp)]
    for base in new:
        record_base(base)
    print(f"recorded {len(new)} base(s)", flush=True)
    return 0


def _case_of(rec: dict) -> str:
    return str(rec.get("case") or rec["issue"])


def _correct(rec: dict) -> bool:
    """A proposal the hidden oracle accepts, or refuses only on a contract the
    maintainers chose and the report never stated."""
    return rec.get("ending") == "fixed" and bool(
        rec.get("oracle_pass") or rec.get("contract_mismatch"))


def _craft(records: list[dict]) -> dict[str, dict]:
    """Craft per fixer configuration (`<prompt_sha>/<models>`): how the judged
    fixed runs fared against the human fix, the standard's points a loss or tie
    fell short on, and the flags fixed runs raised."""
    out: dict[str, dict] = {}
    for rec in records:
        fixer = rec.get("fixer")
        if not fixer:
            continue
        key = f"{fixer.get('prompt_sha')}/{fixer.get('models')}"
        c = out.setdefault(key, {"judged": 0, "wins": 0, "ties": 0, "losses": 0, "errors": 0,
                                 "rate": None, "losses_by_point": {}, "flags": {}})
        if rec.get("ending") == "fixed":
            for flag in rec.get("flags") or []:
                c["flags"][flag] = c["flags"].get(flag, 0) + 1
        craft = rec.get("craft") or {}
        if craft.get("error"):
            c["errors"] += 1
        result = craft.get("result")
        if result not in ("win", "tie", "loss"):
            continue
        c["judged"] += 1
        c[{"win": "wins", "tie": "ties", "loss": "losses"}[result]] += 1
        if result != "win":
            for point in craft.get("points") or []:
                c["losses_by_point"][point] = c["losses_by_point"].get(point, 0) + 1
    for c in out.values():
        if c["judged"]:
            c["rate"] = round((c["wins"] + c["ties"] / 2) / c["judged"], 3)
    return out


def scorecard(records: list[dict], *, bugs: int) -> dict:
    """The run's numbers over every scored pass of `bugs` bugs: proposals
    (runs ending `fixed`), the correct ones among them, precision and coverage,
    the endings, cost, and craft per fixer configuration (`_craft`). A fault
    ending is the machine's, so it is counted apart and scores nothing. A
    craft-only case has no oracle, so it counts toward craft alone; `bugs` and
    the bug counts are over cases with an oracle, by case."""
    scored = [r for r in records if not replay._is_fault(r.get("ending"))]
    judged = [r for r in scored if not r.get("craft_only")]
    proposed = [r for r in judged if r.get("ending") == "fixed"]
    correct = [r for r in proposed if _correct(r)]
    proposed_bugs = {_case_of(r) for r in proposed}
    return {
        "bugs": bugs, "runs": len(records), "scored": len(scored),
        "faults": len(records) - len(scored),
        "proposed": len(proposed), "correct": len(correct),
        "precision": round(len(correct) / len(proposed), 3) if proposed else None,
        "coverage": round(len(proposed_bugs) / bugs, 3) if bugs else None,
        "bugs_proposed": len(proposed_bugs),
        "bugs_correct": len({_case_of(r) for r in correct}),
        "cases": dict(Counter(str(r.get("source") or "history") for r in records)),
        "endings": dict(Counter(str(r.get("ending")) for r in records).most_common()),
        "agent_runs": sum(r.get("agent_runs") or 0 for r in records),
        "avg_seconds": round(replay._avg_seconds(records), 1),
        "craft": _craft(scored),
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
    from pipeline.evals import craft_judge
    for key, c in card.get("craft", {}).items():
        lines += ["", f"## Craft: `{key}`", "",
                  f"{c['judged']} fixes judged against the human fix: {c['wins']} better, "
                  f"{c['ties']} tied, {c['losses']} worse ({c['errors']} judge errors); "
                  f"craft rate {pct(c['rate'])}."]
        top = sorted(c["losses_by_point"].items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        if top:
            lines.append("Losing on: " + "; ".join(
                f"{n}x point {p} ({craft_judge.POINTS[p - 1][:60]})" for p, n in top
                if 1 <= p <= len(craft_judge.POINTS)) + ".")
        if c["flags"]:
            lines.append("Flags: " + ", ".join(f"{f} {n}" for f, n in sorted(c["flags"].items()))
                         + ".")
    by_case: dict[str, list[dict]] = {}
    for rec in records:
        by_case.setdefault(_case_of(rec), []).append(rec)
    lines += ["", "| case | endings by pass | correct |", "| --- | --- | --- |"]
    for case in sorted(by_case, key=lambda c: (not c.isdigit(), int(c) if c.isdigit() else 0, c)):
        recs = sorted(by_case[case], key=lambda r: r.get("pass") or 0)
        correct = ("craft only" if recs[0].get("craft_only")
                   else str(sum(1 for r in recs if _correct(r))))
        lines.append(f"| {case} | {', '.join(str(r.get('ending')) for r in recs)} | {correct} |")
    return "\n".join(lines) + "\n"


# The lanes a run can measure, by name: the staged lane, the one-agent lane, and
# the cross-tested lane.
LANES: dict[str, replay.LaneEntry] = {"staged": replay._run_lane, "solo": replay._run_solo,
                                      "cross": replay._run_cross}


@dataclass(frozen=True)
class _Job:
    """One replay of a run: a history bug (`case` its issue number) or a case.
    `reference` is the commit whose fix the run is judged against."""
    case: str
    source: str
    instance: replay.Instance
    base_sha: str
    oracle: bool
    reference: str


def plan_jobs(pr_store: Store, manifest: dict, *, refresh: bool, issues: set[int] | None,
              only: set[str] | None, cases: bool) -> list[_Job]:
    """The replays a run makes: each fair bug screened in today, then each
    usable case, narrowed to `issues` (by issue number) and `only` (by case id)."""
    fair = {i["issue"]: b["base_sha"] for b in manifest["bases"] for i in b["instances"]
            if i["verdict"] == "fair" and (issues is None or i["issue"] in issues)
            and (only is None or str(i["issue"]) in only)}
    usable = [c for c in (load_cases(pr_store) if cases else [])
              if c.oracle != "unusable" and (issues is None or c.issue in issues)
              and (only is None or c.case in only)]
    by_issue: dict[int, replay.Instance] = {}
    if fair:
        pin = prove.pinned(pr_store)
        by_issue = {inst.issue: inst for inst in _screened(profile.active(), pin, refresh=refresh)
                    if inst.issue in fair}
        missing = sorted(set(fair) - set(by_issue))
        if missing:
            print(f"no longer screened in, left out: {missing}", flush=True)
    return ([_Job(str(n), "history", inst, fair[n], True, inst.merge_sha)
             for n, inst in sorted(by_issue.items())]
            + [_Job(c.case, c.source, c.instance(), c.base_sha, c.oracle == "tests",
                    c.merge_sha or c.head_sha) for c in usable])


def run(*, name: str, passes: int, concurrency: int, refresh: bool,
        issues: set[int] | None, resume: bool, lane: str = "staged",
        cases: bool = True, only: set[str] | None = None) -> int:
    """Replay every fair bug of the set, and unless not `cases` every usable rival
    and case-study case, through the lane `passes` times, pass by pass, each pass
    recorded as replay run `<name>-p<k>`. Re-invoking continues a run: recorded
    replays are kept, by case, and `resume` re-runs the faulted ones. Writes
    `<verify scratch>/replay/<name>/scorecard.md` and one `eval-set:run` ledger
    row."""
    from pipeline.evals import craft_judge
    prof = profile.active()
    fixer = craft_judge.fixer_config(lane)  # type: ignore[arg-type]
    pr_store = Store()
    manifest = load_manifest(pr_store)
    if manifest["stale"]:
        print(f"left out {len(manifest['stale'])} base(s) qualified under another sandbox "
              f"profile; run eval_set build to requalify: "
              f"{' '.join(sha[:12] for sha in manifest['stale'])}", flush=True)
    planned = plan_jobs(pr_store, manifest, refresh=refresh, issues=issues, only=only,
                        cases=cases)
    if not planned:
        print("the evaluation set holds no fair bug or usable case to run", file=sys.stderr)
        return 2
    bases: dict[str, prove.PinnedBase] = {}
    for sha in sorted({job.base_sha for job in planned}):
        try:
            bases[sha] = _held_base(sha)
        except (prove.NoBase, verify_driver.BuildFailure) as e:
            # A case's base is its own; one that cannot be built drops its cases, not the run.
            if any(job.base_sha == sha and job.source == "history" for job in planned):
                raise
            dropped = [job.case for job in planned if job.base_sha == sha]
            print(f"base {sha[:12]} cannot be held ({e}); left out: {dropped}", flush=True)
    planned = [job for job in planned if job.base_sha in bases]
    n_bugs = sum(1 for job in planned if job.source == "history")

    out_dir = settings.verify_scratch() / "replay" / name
    started = storekit.now()
    results: list[dict] = []
    jobs: list[tuple[int, _Job]] = []
    for k in range(1, passes + 1):
        recorded = replay._recorded(pr_store, f"{name}-p{k}")
        for job in planned:
            prior = recorded.get(job.case)
            if prior is not None and not (resume and replay._is_fault(prior.get("ending"))):
                results.append({**prior, "pass": k})
            else:
                jobs.append((k, job))
    print(f"run {name}: {n_bugs} bugs and {len(planned) - n_bugs} cases x {passes} passes; "
          f"{len(jobs)} to run, {len(results)} already recorded", flush=True)

    # Set by the first run whose agent can serve nothing (a spent usage limit,
    # a lost login): every run after it would fail the same way, so a job that
    # starts once it is set does nothing, and is left for a --resume.
    unavailable = threading.Event()

    def one(k: int, job: _Job) -> dict | None:
        if unavailable.is_set():
            return None
        base = bases[job.base_sha]
        rec = replay.run_instance(
            job.instance, base=base, base_sha=base.sha, profile=prof,
            workdir=out_dir / f"p{k}" / job.case, run_lane=LANES[lane],
            judge_contract=replay._judge_contract, judge_craft=craft_judge.score_craft,
            fixer=fixer, oracle=job.oracle)
        if rec.get("ending") == "agent-unavailable" or rec.get("craft_unavailable"):
            unavailable.set()
        return {**rec, "case": job.case, "source": job.source, "reference": job.reference}

    done = 0
    halted = ""
    left = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(one, k, job): (k, job) for k, job in jobs}
        for fut in as_completed(futures):
            k, job = futures[fut]
            inst = job.instance
            try:
                got = fut.result()
            except Exception:
                print(f"{job.case} pass {k} crashed:", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                got = {"issue": inst.issue, "pr": inst.pr, "ending": replay._CRASH_ENDING,
                       "seconds": 0.0, "agent_runs": 0, "case": job.case,
                       "source": job.source, "reference": job.reference}
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
                                                   base_sha=bases[job.base_sha].sha),
                          "eval": name, "eval_lane": lane, "pass": k}})
            print(f"[{done}/{len(jobs)}] {job.case} pass {k}: {rec.get('ending')} "
                  f"({replay._cell(rec.get('seconds'))}s) {rec.get('detail') or ''}"[:300],
                  flush=True)
            if (rec.get("ending") == "agent-unavailable" or rec.get("craft_unavailable")) \
                    and not halted:
                halted = str(rec.get("detail") if rec.get("ending") == "agent-unavailable"
                             else (rec.get("craft") or {}).get("error")
                             or "the agent is unavailable")
                print(f"halting: {halted}", flush=True)
    if halted:
        print(f"{left} run(s) left for --resume", flush=True)

    bugs = sum(1 for job in planned if job.oracle)
    card = {**scorecard(results, bugs=bugs), "halted": halted or None}
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
    r.add_argument("--only", help="run only these case ids (an issue number for a history "
                                  "bug), comma-separated")
    r.add_argument("--resume", action="store_true", help="re-run the faulted instances")
    r.add_argument("--refresh", action="store_true", help="re-harvest from GitHub")
    r.add_argument("--lane", choices=sorted(LANES), default="staged",
                   help="the staged, one-agent, or cross-tested lane")
    r.add_argument("--no-cases", action="store_true",
                   help="replay the fair bugs only, not the rival and case-study cases")
    i = sub.add_parser("import", help="record the bases a manifest file names")
    i.add_argument("path", type=Path)
    c = sub.add_parser("cases", help="rival and case-study cases: harvest or add")
    cs = c.add_subparsers(dest="cases_command", required=True)
    h = cs.add_parser("harvest", help="qualify every rival case the issue store names")
    h.add_argument("--dry-run", action="store_true", help="print, skip R6, record nothing")
    a = cs.add_parser("add", help="add a case study: an issue and the PR to judge by")
    a.add_argument("--issue", type=int, required=True)
    a.add_argument("--pr", type=int, required=True)
    a.add_argument("--dry-run", action="store_true", help="print, skip R6, record nothing")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "import":
            return import_file(args.path)
        if args.command == "cases":
            if args.cases_command == "harvest":
                return harvest_cases(dry_run=args.dry_run)
            return add_case(args.issue, args.pr, dry_run=args.dry_run)
        if args.command == "run":
            issues = ({int(n) for n in args.issues.split(",") if n.strip()}
                      if args.issues else None)
            return run(name=args.name, passes=args.passes, concurrency=args.concurrency,
                       refresh=args.refresh, issues=issues, resume=args.resume,
                       lane=args.lane, cases=not args.no_cases,
                       only=set(args.only.split(",")) if args.only else None)
        return build(target=args.target, min_fixes=args.min_fixes, refresh=args.refresh,
                     concurrency=args.concurrency, dry_run=args.dry_run)
    except prove.NoBase as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
