"""Per-PR suggestion — a one-click rendering of the store's analysis section.

No synthesis here: ANALYZE already decided the disposition; gates.py already
decided whether a merge is allowed. This module only translates {analysis,
gates} into the suggestion card the UI shows.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from pipeline import freshness  # pipeline modules (path set up by data import)
from pipeline import gates
from pipeline import settings
from pipeline.settings import BOT_LOGIN
from app.backend import models

if TYPE_CHECKING:
    from pipeline.model import Pr


def _asks_comment(asks: list | None) -> str:
    items = [a for a in (asks or []) if a]
    if not items:
        return "Thanks for the contribution! A few things need addressing before this can merge."
    return ("Thanks for the contribution! Before this can merge, please address:\n"
            + "\n".join(f"- {a}" for a in items))


# A secret-NAMED token (KEY/SECRET/TOKEN/...). We only ever echo the *name* of a
# leaked credential, never its value, so the comment can't re-leak the secret.
_SECRET_NAME = re.compile(r"(?i)KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH")


def _looks_like_value(tok: str) -> bool:
    """A high-entropy hex/base64 token — i.e. the credential value itself, which
    must never be echoed even if it slipped through as the leading identifier."""
    return len(tok) >= 24 and (bool(re.fullmatch(r"[0-9a-fA-F]{24,}", tok))
                               or (any(c.isdigit() for c in tok) and len(tok) >= 32))


def _secret_leak_location(rec: Pr) -> str | None:
    """A safe '`VAR` in `file`' description of a committed credential, parsed
    from the threat evidence (`<file>: <VAR><sep><value>`) WITHOUT echoing the
    value. Falls back to 'a credential in `file`' when no safe name is present."""
    if "secret-leak" not in rec.threat_signatures:
        return None
    threat = rec.section("threat") or {}
    ev = (threat.get("detail") or {}).get("secret-leak") or ""
    file, _, rest = ev.partition(": ")
    file = file.strip().strip("?") or None
    m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", rest.lstrip("+- \t"))
    ident = m.group(0) if m else None
    name = ident if ident and _SECRET_NAME.search(ident) and not _looks_like_value(ident) else None
    if name and file:
        return f"`{name}` in `{file}`"
    if file:
        return f"a credential in `{file}`"
    if name:
        return f"`{name}`"
    return None


def _secret_leak_comment(location: str) -> str:
    """The auto-bounce body for a committed credential — names the exact secret
    (never its value) and asks the author to rotate + scrub + force-push. Editable
    per-PR in the Disposition panel before it's posted."""
    return (
        "👋 Thanks for the contribution — but this PR commits what looks like a "
        "**live credential**, so we're requesting changes automatically, before "
        "any code review.\n\n"
        f"Detected in the diff:\n- {location}\n\n"
        "Since this PR is public, treat that value as **already compromised**:\n"
        "1. **Rotate it at its provider now** — deleting it from the code doesn't "
        "un-leak it; it's already in this PR's history.\n"
        "2. Move it out of the committed files into an environment variable / "
        "secret manager.\n"
        "3. **Force-push** the cleaned branch so it's gone from the PR's history.\n\n"
        "This is an automated secret-detection bounce — we haven't reviewed the "
        "change on its merits yet. Once you push the fix, it'll go into normal review.\n\n"
        "*Flagged in error? Reply here and a maintainer will take a look.*"
    )


def _bot_comment(s: dict) -> str | None:
    """The exact text the configured bot will post for this suggestion — the same
    function the executor calls, so the UI shows what actually gets posted.
    None when the action posts no comment (merge / no-op)."""
    acc = s.get("accept")
    if acc is None:
        return None
    if acc.kind == "review":          # request-changes
        return acc.body
    if acc.kind == "close":
        from app.backend import decisions
        return decisions.default_comment(models.CloseAction(
            action=acc.action, canonical=acc.canonical,
            upstream_pr=acc.upstream_pr, upstream_commit=acc.upstream_commit,
            upstream_date=acc.upstream_date, merge_prs=acc.merge_prs))
    return None                               # merge posts no comment


def _reversible(s: dict) -> bool:
    """Closes can be reopened and comments deleted; a merge is permanent."""
    acc = s.get("accept")
    return acc is None or acc.kind != "merge"


def _needs_verify(s: dict) -> str | None:
    """A one-line reason the operator should eyeball this before firing it —
    surfaces where the AI's evidence is thin so a generic action isn't trusted
    blindly. None when the suggestion stands on solid, cited evidence."""
    acc = s.get("accept")
    if acc is None or acc.kind != "close":
        return None
    if acc.action == "CLOSE_FIXED" and not acc.upstream_pr:
        return ("The AI marked this already-fixed but didn't cite an upstream PR — "
                "confirm it's genuinely redundant before closing.")
    if acc.action == "CLOSE_DUP" and not acc.canonical:
        return "No canonical PR cited — confirm this is really a duplicate before closing."
    return None


def _cluster_merge_prs(rec: Pr) -> list[int]:
    """Open PRs sharing any cluster with this PR that are routed to merge —
    referenced in a close-oversized comment so the author doesn't resubmit the
    work we're keeping (#183). Empty when the PR is clusterless or nothing in
    its clusters merges."""
    from app.backend import data
    cids = set(data.pr_to_clusters().get(rec.number) or [])
    if not cids:
        return []
    return sorted(n for n, other in data.prs().items()
                  if n != rec.number
                  and cids & set(data.pr_to_clusters().get(n) or [])
                  and other.disposition == "merge" and other.state == "open")


def suggest_for_record(rec: Pr, disposition: str | None = None,
                       human_merge: dict | None = None) -> dict:
    """Build the suggestion card. When `disposition` is given, render the
    suggestion (and its exact bot_comment) as if that were the PR's disposition
    — used to keep the comment in sync when the operator changes the action in
    the UI (#13). `human_merge` is the CODEOWNERS verdict (codeowners.human_merge)
    when the caller has one: a merge-disposition card then says that the bot
    cannot merge and a code owner must, so clearing the stated blocker is never
    presented as the whole path to landing the PR."""
    s = _suggest_inner(rec, disposition, human_merge)
    s["bot_comment"] = _bot_comment(s)
    s["reversible"] = _reversible(s)
    s["needs_verify"] = _needs_verify(s)
    return s


def _unanalyzed_card(rec: Pr) -> dict:
    """A PR with no current analysis, never analyzed. ANALYZE runs per-cluster,
    so the right next step — and the right copy — depends on whether a clustering
    pass has placed, considered, or never reached this PR. The old card always
    said "run ANALYZE on this PR's cluster", a dead end for a clusterless PR."""
    if rec.cluster_ids:
        return {"action": "ANALYZE", "label": "Not yet analyzed", "tone": "muted",
                "rationale": "Run the ANALYZE phase on its cluster.", "accept": None}
    if freshness.is_current(rec, "cluster"):
        # a clustering pass considered this PR and left it standalone (no cluster)
        return {"action": "STANDALONE", "label": "Standalone — no cluster", "tone": "muted",
                "rationale": ("A clustering pass left this PR on its own. ANALYZE runs "
                              "per-cluster, so there's no automated disposition yet — "
                              "merge it directly if the checks are green."),
                "accept": None}
    # no clustering pass has reached this PR (or its head moved since)
    return {"action": "CLUSTER", "label": "Not yet clustered", "tone": "muted",
            "rationale": ("No clustering pass has reached this PR yet — run CLUSTER "
                          "(it'll be grouped or marked standalone), then ANALYZE."),
            "accept": None}


def _suggest_inner(rec: Pr, disposition: str | None = None,
                   human_merge: dict | None = None) -> dict:
    # Dependency bumps are out of the pipeline's scope (gates.is_dependabot_bump):
    # their supply-chain risk is in the upgraded package — reviewed on the PR by
    # Socket — not the diff, which is all the agent can see. There's no cluster,
    # disposition, or triage action to offer here; the author lands it upstream.
    if disposition is None and gates.is_dependabot_bump(
            rec.author, (rec.section("summary") or {}).get("paths")):
        return {"action": "OUT_OF_SCOPE", "label": "Out of scope — dependency bump",
                "tone": "muted",
                "rationale": ("Dependabot dependency bump. The pipeline defers these: the "
                              "supply-chain risk is in the upgraded package (reviewed on the "
                              "PR by Socket), not the diff, so there's no automated cluster, "
                              "disposition, or triage action. Merge it upstream once Socket "
                              "and CI are green."),
                "accept": None}
    if disposition is None:
        if not rec.section("analysis") or not freshness.is_current(rec, "analysis"):
            if rec.section("analysis"):     # had an analysis; the head moved since
                return {"action": "ANALYZE", "label": "Re-analyze", "tone": "muted",
                        "rationale": "The PR head moved since analysis — re-run ANALYZE on its cluster.",
                        "accept": None}
            return _unanalyzed_card(rec)
        d = rec.disposition
    else:
        d = disposition
    if d == "merge":
        ok, reason = gates.merge_allowed(rec)
        if ok:
            return {"action": "MERGE", "label": "Merge", "tone": "green",
                    "rationale": rec.rationale or "Canonical fix — gates clean, security GREEN.",
                    "accept": models.MergeAccept(method="squash", tags=["clean", "agent-suggested"])}
        # A CODEOWNERS-gated PR can't be merged by the bot even once the
        # stated blocker clears — say so, or "re-run SECURITY" reads as the whole
        # path to landing it when the terminal step is a human code-owner merge.
        if human_merge and human_merge.get("required"):
            owners = " + ".join(human_merge.get("owners", []))
            reason += (f". Even with that cleared, {BOT_LOGIN} can't merge this — "
                       f"CODEOWNERS requires a code-owner merge ({owners}).")
        return {"action": "BLOCKED", "label": "Merge blocked", "tone": "yellow",
                "rationale": reason, "accept": None,
                "blocker": "security" if gates.blocked_on_security(rec) else None}

    if d == "request-changes":
        loc = _secret_leak_location(rec)
        body = _secret_leak_comment(loc) if loc else _asks_comment(rec.asks)
        return {"action": "REQUEST_CHANGES", "label": "Request changes", "tone": "yellow",
                "rationale": rec.rationale or "Good direction; needs author fixes first.",
                "comment": body,
                "accept": models.ReviewAccept(event="request-changes", body=body,
                                              tags=["needs-revision", "agent-suggested"])}

    if d == "close-dup":
        canon = rec.canonical
        return {"action": "CLOSE_DUP", "label": "Close as duplicate", "tone": "muted",
                "rationale": rec.rationale or (f"Duplicate of #{canon}." if canon else "Duplicate."),
                "accept": models.CloseAccept(action="CLOSE_DUP", canonical=canon,
                                             tags=["duplicate", "agent-suggested"])}

    if d == "close-fixed":
        return {"action": "CLOSE_FIXED", "label": "Close — already fixed", "tone": "muted",
                "rationale": rec.rationale or
                    f"An equivalent fix already landed on {settings.default_branch()}.",
                "accept": models.CloseAccept(action="CLOSE_FIXED", upstream_pr=rec.upstream_pr,
                                             upstream_commit=rec.upstream_commit,
                                             upstream_date=rec.upstream_date,
                                             tags=["already-fixed", "agent-suggested"])}

    if d == "close-stale":
        return {"action": "CLOSE_STALE", "label": "Close — stale", "tone": "muted",
                "rationale": rec.rationale or "Drifted/abandoned with no clear path.",
                "accept": models.CloseAccept(action="CLOSE_STALE", tags=["stale", "agent-suggested"])}

    if d == "close-oversized":
        return {"action": "CLOSE_OVERSIZED", "label": "Close — too big (break up)", "tone": "muted",
                "rationale": rec.rationale or "Bundles too much for one PR — ask the author to split it into smaller PRs.",
                "accept": models.CloseAccept(action="CLOSE_OVERSIZED", merge_prs=_cluster_merge_prs(rec),
                                             tags=["oversized", "agent-suggested"])}

    return {"action": "REVIEW", "label": "Needs human routing", "tone": "muted",
            "rationale": rec.rationale or "No automated path — route by hand.",
            "accept": None}
