"""The ONE policy for another author's pull request on an issue the factory
fixed: evidence our fix must answer, never code it takes.

Once the cross lane ends `fixed`, `rivals` names the open pull requests by
others that claim to fix the issue (`related_prs.search`), at most MAX_RIVALS
with tests, passing over any whose author is on the threat blocklist, whose
whole diff cannot be read, or whose diff the threat scan reads malicious.
`judge` runs a rival's test files in the sandbox three ways: on the base, where
they must fail twice; with the rival's own change, where they must pass twice;
and with our fix. Tests that clear the first two and fail with ours are a
`gap`. `notes` words the gaps for one revision of our fix: the rival's test
files and code, quoted as data, and our run's output — never the rival's
change. `recheck` runs the same tests over the revised fix: `gap-closed` or
`gap-open`. `credit` names the authors whose tests changed our fix, and the
proposal credits each as a co-author (`co_author`); `flag` holds a fix that
leaves a gap open for an operator.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from issue_triage import related_prs
from pipeline import diff_cache, diffpaths, gates, gh, prove, settings, threats, verify_driver

MAX_RIVALS = 3
# The pull requests looked at for MAX_RIVALS with tests; each costs a diff read.
MAX_EXAMINED = 10
TEST_QUOTE_MAX = 6000
OUTPUT_MAX = 2000


@dataclass(frozen=True)
class Rival:
    pr: int
    author: str | None
    title: str
    tests: str
    fix: str
    test_paths: list[str]


def _ours(login: str | None) -> bool:
    if not login:
        return False
    bot = settings.bot_login().removesuffix("[bot]")
    return login == settings.push_login() or (bool(bot) and login.removesuffix("[bot]") == bot)


def _entry(pr: int, author: str | None, verdict: str, why: str, **extra: str) -> dict:
    return {"pr": pr, "author": author, "verdict": verdict, "why": why, **extra}


def rivals(issue: int, *, exclude: set[int], registry: dict) -> tuple[list[Rival], list[dict]]:
    """The rivals whose tests judge our fix of `issue`, and an entry for each
    pull request passed over on the way. `registry` is the store's threat
    registry."""
    found = related_prs.search(issue, exclude=exclude) or []
    claims = sorted((r for r in found if r["state"] == "open" and r["closes"]
                     and not _ours(r["author"])), key=lambda r: r["number"])
    out: list[Rival] = []
    skipped: list[dict] = []
    for r in claims[:MAX_EXAMINED]:
        if len(out) == MAX_RIVALS:
            break
        n, author = r["number"], r["author"]
        if threats.is_blocked_actor(registry, author):
            skipped.append(_entry(n, author, "skipped", "its author is on the threat blocklist"))
            continue
        diff = diff_cache.fetch_complete(n)
        if diff is None or diff.unread:
            skipped.append(_entry(n, author, "skipped", "its whole diff could not be read"))
            continue
        if threats.scan_diff(diff.text)["verdict"] == "malicious":
            skipped.append(_entry(n, author, "skipped", "the threat scan reads it malicious"))
            continue
        tests = diffpaths.filter_diff(diff.text, diffpaths.is_test_path)
        if not tests:
            skipped.append(_entry(n, author, "skipped", "it adds no tests"))
            continue
        out.append(Rival(pr=n, author=author, title=r["title"], tests=tests,
                         fix=diffpaths.filter_diff(diff.text,
                                                   lambda p: not diffpaths.is_test_path(p)),
                         test_paths=diffpaths.changed_paths(tests)))
    return out, skipped


def _twice(legs: prove.Legs, want: int) -> bool:
    return legs.get("exit") == want and legs.get("exit_confirm") == want


def _ours_with(base: prove.PinnedBase, rival: Rival, patch: str, cmd: str,
               label: str) -> prove.Legs | str:
    """Our `patch` with the rival's tests, or why they cannot run beside it."""
    try:
        composed = prove.compose(label, patch, rival.tests)
    except ValueError as e:
        return f"its tests touch files our fix changes: {e}"
    return prove.green_legs(base, patch=composed, test_cmd=cmd, label=label)


def judge(base: prove.PinnedBase, rival: Rival, patch: str, *, label: str) -> dict:
    """The rival's entry for our fix `patch`: `covered`, `gap` (with our run's
    `output`), or `skipped` with the reason its tests prove nothing here."""
    entry = _entry(rival.pr, rival.author, "skipped", "")
    cmd = verify_driver.derive_test_command(rival.test_paths)
    if not cmd:
        return {**entry, "why": "its tests name no file the test runner runs"}
    red = prove.red_legs(base, patch=prove.compose(label, rival.tests), test_cmd=cmd,
                         label=label)
    if not _twice(red, gates.SENTINEL_TEST_FAIL):
        return {**entry, "why": "its tests do not fail on our base"}
    try:
        own = prove.compose(label, rival.tests, rival.fix)
    except ValueError as e:
        return {**entry, "why": f"its tests and change share files: {e}"}
    if not _twice(prove.green_legs(base, patch=own, test_cmd=cmd, label=label),
                  gates.SENTINEL_PASS):
        return {**entry, "why": "its tests do not pass with its own change on our base"}
    ours = _ours_with(base, rival, patch, cmd, label)
    if isinstance(ours, str):
        return {**entry, "why": ours}
    if _twice(ours, gates.SENTINEL_PASS):
        return {**entry, "verdict": "covered", "why": "our fix passes its tests"}
    return {**entry, "verdict": "gap", "why": f"our fix fails {', '.join(rival.test_paths)}",
            "output": str(ours.get("output_tail") or "")[-OUTPUT_MAX:]}


def recheck(base: prove.PinnedBase, rival: Rival, patch: str, entry: dict, *,
            label: str) -> dict:
    """`entry`, a gap, judged again over the revised fix `patch`."""
    cmd = verify_driver.derive_test_command(rival.test_paths) or ""
    ours = _ours_with(base, rival, patch, cmd, label)
    if not isinstance(ours, str) and _twice(ours, gates.SENTINEL_PASS):
        return {**entry, "verdict": "gap-closed", "why": "the revised fix passes its tests"}
    why = (ours if isinstance(ours, str)
           else f"the revised fix still fails {', '.join(rival.test_paths)}")
    return {**entry, "verdict": "gap-open", "why": why}


def notes(gaps: list[tuple[Rival, dict]]) -> str:
    """The revision's notes for `gaps`: what fails and the tests that show it."""
    parts = ["Another author's pull request on this issue carries tests this fix does not "
             "pass. Decide whether the report asks for the behavior they check; where it does, "
             "change the fix so that it holds, and write your own test for it. Do not copy "
             "their test files into the change."]
    for rival, entry in gaps:
        parts += [f"\n#{rival.pr} ({', '.join(rival.test_paths)}). The fix's run of these tests "
                  f"ended:\n{entry.get('output') or '(no output)'}",
                  f"Their tests:\n```diff\n{rival.tests[:TEST_QUOTE_MAX]}\n```"]
    return "\n".join(parts)


def credit(entries: list[dict] | None) -> list[dict]:
    """The pull requests, with their authors, whose tests changed our fix."""
    return [{"pr": e["pr"], "author": e["author"]} for e in entries or []
            if e.get("verdict") == "gap-closed" and e.get("author")]


def co_author(login: str) -> str | None:
    """`login`'s Co-authored-by value, at the noreply address GitHub credits to
    the account, or None when GitHub does not name its id."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", login):
        return None
    uid = (gh.gh_json(f"users/{login}") or {}).get("id")
    return f"{login} <{uid}+{login}@users.noreply.github.com>" if isinstance(uid, int) else None


def flag(entries: list[dict] | None) -> str | None:
    """Why a fix that leaves a rival's tests failing waits for an operator."""
    open_ = [e for e in entries or [] if e.get("verdict") == "gap-open"]
    if not open_:
        return None
    return "another pull request's tests fail with this fix: " + "; ".join(
        f"#{e['pr']} ({e['why']})" for e in open_[:3])
