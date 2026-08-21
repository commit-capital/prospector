"""Greptile's on-PR verdict, read directly from GitHub.

Greptile posts a scored issue-level summary comment (`Confidence Score: N/5`,
`Last reviewed commit: …/commit/<sha>`) plus inline review comments. These
functions read that data via the gh transport and parse out the score, the
reviewed commit SHA, and displayable review text — the gate input.
`backfill_greptile_data` stamps the reviewed SHA onto open scored PRs whose
stored SHA is missing or anchored to a commit other than the current head."""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from pipeline import settings
from pipeline.gh import gh_list

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.store import Store


def prs_needing_greptile_data(store: Store, prs: dict[int, Pr] | None = None) -> list[int]:
    """Open, scored PRs whose reviewed SHA is missing or anchored to a commit
    other than the current head — the fact recovered from GitHub's
    review/comment data. A head that moved after the SHA was captured selects
    the PR for a re-read, so a Greptile re-review at the new head becomes
    visible without a targeted refresh. Sorted ascending so a backfill
    processes them deterministically. `prs` lets a caller that already loaded
    the corpus this run (e.g. a full ingest) reuse that snapshot instead of a
    second full scan; it defaults to loading the corpus itself."""
    corpus = prs if prs is not None else store.all_prs()
    return sorted(
        n for n, pr in corpus.items()
        if pr.state == "open" and pr.greptile is not None
        and (not pr.greptile_reviewed_sha or pr.greptile_reviewed_sha != pr.head_sha)
    )


def fetch_greptile_review_data(n: int) -> tuple[str | None, list[dict], list[dict]]:
    """Greptile's reviews + inline comments for PR n (filtered to Greptile's own
    logins), plus the reviewed SHA. `sha` is the most recent Greptile review's
    commit_id, or an inline comment's original_commit_id when the review reports
    none. Returns (None, [], []) when Greptile has not reviewed or GitHub is
    unreachable."""
    reviews = gh_list(f"repos/{settings.repo()}/pulls/{n}/reviews?per_page=100")
    if reviews is None:
        return None, [], []
    greptile_reviews = [r for r in reviews if _is_greptile(r)]
    if not greptile_reviews:
        return None, [], []
    comments = gh_list(f"repos/{settings.repo()}/pulls/{n}/comments?per_page=100") or []
    greptile_comments = [c for c in comments if _is_greptile(c)]
    sha = None
    for r in reversed(greptile_reviews):
        if r.get("commit_id"):
            sha = r["commit_id"]
            break
    if sha is None:
        sha = _sha_from_comments(greptile_comments)
    return sha, greptile_reviews, greptile_comments


_SCORE_RE = re.compile(r"Confidence Score:\s*(\d)\s*/\s*5")
_SUMMARY_SHA_RE = re.compile(r"[Ll]ast reviewed commit:[^\n]*?/commit/([0-9a-f]{7,40})")


def _latest_scored_summary(
    comments: list[dict],
) -> tuple[dict | None, int | None, str | None]:
    """The most recent Greptile summary comment carrying a `Confidence Score: N/5`,
    paired with its score and reviewed-commit footer SHA (`Last reviewed commit:
    …/commit/<sha>`). Callers pass comments already filtered to Greptile's own.
    Returns (None, None, None) when no comment carries a score."""
    for c in reversed(comments):
        body = c.get("body") or ""
        m = _SCORE_RE.search(body)
        if not m:
            continue
        sha_m = _SUMMARY_SHA_RE.search(body)
        return c, int(m.group(1)), (sha_m.group(1) if sha_m else None)
    return None, None, None


def parse_confidence_score(body: str | None) -> int | None:
    """The confidence score (`Confidence Score: N/5`) from any Greptile comment
    or review body, or None when it carries none."""
    m = _SCORE_RE.search(body or "")
    return int(m.group(1)) if m else None


def _parse_greptile_summary(comments: list[dict]) -> tuple[int | None, str | None]:
    """The confidence score (`Confidence Score: N/5`) and reviewed commit SHA
    (`Last reviewed commit: …/commit/<sha>` footer) from Greptile's issue-level
    summary comment. Callers pass comments already filtered to Greptile's own; the
    most recent comment carrying a score wins. Returns (None, None) when no
    summary carries a score."""
    _c, score, sha = _latest_scored_summary(comments)
    return score, sha


def fetch_greptile_summary(n: int) -> tuple[int | None, str | None]:
    """Greptile's confidence score and reviewed SHA for PR n, read from its
    issue-level summary comment (`repos/settings.repo()/issues/{n}/comments`) — the surface
    that holds the numeric score and the authoritative 'last reviewed commit'.
    Returns (None, None) when Greptile posted no scored summary or GitHub is
    unreachable."""
    comments = gh_list(f"repos/{settings.repo()}/issues/{n}/comments?per_page=100")
    if not comments:
        return None, None
    return _parse_greptile_summary([c for c in comments if _is_greptile(c)])


def fetch_greptile_summary_text(n: int) -> tuple[str | None, str | None]:
    """The body of Greptile's latest scored summary comment on PR n and the
    reviewed SHA its footer names — the prose behind the score, which names the
    defects Greptile weighed even when it left no inline comment. Returns
    (None, None) when no scored summary exists or GitHub is unreachable."""
    comments = gh_list(f"repos/{settings.repo()}/issues/{n}/comments?per_page=100")
    if not comments:
        return None, None
    c, _score, sha = _latest_scored_summary([c for c in comments if _is_greptile(c)])
    if c is None:
        return None, None
    return str(c.get("body") or ""), sha


def fetch_greptile_verdict(n: int) -> tuple[int | None, str | None]:
    """Greptile's own (score, reviewed_sha) for PR n, direct from GitHub — the
    gate input. The score and authoritative reviewed SHA come from the
    issue-level summary comment; when that comment
    carries no SHA (or no summary was posted) the reviewed SHA falls back to a
    formal review's / inline comment's commit. Returns (None, None) when Greptile
    has not reviewed."""
    score, footer_sha = fetch_greptile_summary(n)
    if footer_sha is not None:
        return score, footer_sha
    review_sha, _revs, _cmts = fetch_greptile_review_data(n)
    return score, review_sha


def _strip_html(s: str) -> str:
    """Greptile's summary comment as displayable text: unwrap the HTML tags it
    embeds (`<h3>`, `<sub>`, `<li>`, `<br>`) and decode entities, leaving the
    Markdown a review card renders cleanly."""
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"</(p|h\d|li|ul|ol)>", "\n", s)
    s = re.sub(r"<li>", "• ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)  # decode literal \uXXXX
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def fetch_greptile_feedback(n: int) -> dict | None:
    """Greptile's on-PR verdict for PR n read directly from GitHub's issue-level
    summary comment — the single-PR display surface. Returns
    `{score, body, commit_id}` (body as displayable text), or None when Greptile
    posted no scored summary or GitHub is unreachable."""
    comments = gh_list(f"repos/{settings.repo()}/issues/{n}/comments?per_page=100")
    if not comments:
        return None
    c, score, sha = _latest_scored_summary([x for x in comments if _is_greptile(x)])
    if c is None:
        return None
    body = c.get("body") or ""
    # Greptile may edit its existing summary in place or post a new one.  The
    # app uses this opaque version to notice either shape after a manual
    # re-trigger; include a body digest as a final backstop for fixtures/older
    # GitHub payloads that omit timestamps.
    version = ":".join([
        str(c.get("id") or ""),
        str(c.get("updated_at") or c.get("created_at") or ""),
        hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:12],
    ])
    return {"score": score, "body": _strip_html(body), "commit_id": sha,
            "updated_at": c.get("updated_at") or c.get("created_at"), "version": version}


def _sha_from_comments(greptile_comments: list[dict]) -> str | None:
    """The commit a Greptile inline review comment anchors to, recovered when the
    review itself reports no `commit_id`. `original_commit_id` is the commit the
    comment was first placed against — the head Greptile reviewed — so it is
    preferred over the outdatable `commit_id`. Callers pass comments already
    filtered to Greptile's own."""
    for c in reversed(greptile_comments):
        sha = c.get("original_commit_id") or c.get("commit_id")
        if sha:
            return sha
    return None


def _is_greptile(actor: dict) -> bool:
    """Whether a review or comment was authored by Greptile (any of its bot
    logins — e.g. `greptile-apps[bot]`, `greptileai[bot]`)."""
    return "greptile" in (actor.get("user") or {}).get("login", "").lower()


def backfill_greptile_data(store: Store, prs: dict[int, Pr] | None = None) -> dict:
    """Stamp the Greptile-reviewed commit SHA — and correct the confidence score
    to Greptile's own — onto open scored PRs whose reviewed SHA is missing or
    off-head, so staleness becomes known and a re-review at the current head is
    picked up. Each PR is
    independent: one whose verdict can't be fetched (404, no Greptile review, gh
    hiccup) is skipped, never fatal; one whose fetched verdict matches the
    stored one (the head moved but Greptile has not re-reviewed) is skipped
    without a write. Persists per PR, so an interrupted run resumes by
    re-selecting only the PRs still missing or stale. `prs` is passed
    through to `prs_needing_greptile_data` to reuse an already-loaded corpus
    snapshot. Returns {candidates, stamped, skipped}."""
    numbers = prs_needing_greptile_data(store, prs)
    stamped = skipped = 0
    for i, n in enumerate(numbers, 1):
        score, sha = fetch_greptile_verdict(n)
        if sha:
            pr = store.edit_pr(n)
            sig = dict(pr.rec.get("signals") or {})
            if (sig.get("greptile_reviewed_sha") == sha
                    and (score is None or sig.get("greptile") == score)):
                skipped += 1
            else:
                sig["greptile_reviewed_sha"] = sha
                if score is not None:
                    sig["greptile"] = score
                pr.set_signals(sig)
                stamped += 1
        else:
            skipped += 1
        if i % 100 == 0 or i == len(numbers):
            print(f"  {i}/{len(numbers)} ({stamped} stamped, {skipped} skipped)", flush=True)
    return {"candidates": len(numbers), "stamped": stamped, "skipped": skipped}
