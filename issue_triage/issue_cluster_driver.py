"""CLUSTER (deterministic half): group issues by subsystem + shared identifiers
into issue-clusters, wire two-way membership, compute pain, and propagate the
oversized needs_review flag. Agentic curation (/diagnose-issue-cluster) refines
these candidates and writes the cluster `curation` section separately.

The deterministic cluster ids (assigned by enumeration in cluster_issues) are not
stable across corpora, so run() rebuilds the cluster set from scratch each pass —
the same regenerate-wholesale semantics the legacy clusters.json had. Per-issue
facts (summary/repro/analysis) keep their stable identity + freshness; cluster
curation is re-confirmed after a rebuild.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline import storekit
from issue_triage import cluster_issues
from issue_triage import pain_score
from issue_triage.issue_store import IssueStore

if TYPE_CHECKING:
    from issue_triage.issue_model import Issue

_WEIGHTS = json.loads((Path(__file__).resolve().parent / "pain-weights.json").read_text())


def _summaries(issues: dict[int, Issue]) -> list[dict]:
    return [{"number": n, "subsystem": i.subsystem or "other", "identifiers": i.identifiers}
            for n, i in issues.items()]


def rank_pain(groups: list[dict], issues: dict[int, Issue]) -> dict[int, float]:
    """Per-cluster pain via the canonical pain_score blend: percentile-normalized
    engagement (reactions/comments summed across members, distinct-author
    reporters) x severity multiplier. An oversized (needs_review) cluster ranks
    on its canonical's own engagement only, since its membership is
    untrustworthy."""
    agg = []
    for g in groups:
        members = [issues[n] for n in g["members"] if n in issues]
        if not members:
            continue
        canonical = sorted(
            members,
            key=lambda r: (-(r.reactions_total + r.comments), r.created_at or ""))[0]
        scored = [canonical] if g.get("needs_review") else members
        reactions = sum(r.reactions_total for r in scored)
        comments = sum(r.comments for r in scored)
        reporters = len({r.author for r in scored})
        agg.append((g["id"], canonical, reporters, reactions, comments))
    norms = {
        "reporters": pain_score.percentile_normalize([a[2] for a in agg]),
        "reactions": pain_score.percentile_normalize([a[3] for a in agg]),
        "comments": pain_score.percentile_normalize([a[4] for a in agg]),
    }
    out: dict[int, float] = {}
    for cid, canonical, reporters, reactions, comments in agg:
        text = (canonical.title or "") + " " + (canonical.body or "")
        out[cid] = pain_score.pain_for_cluster(
            reporters, reactions, comments, text, canonical.labels, norms, _WEIGHTS)
    return out


def run(store: IssueStore) -> int:
    """Rebuild the candidate clusters, preserving human-confirmed ones.

    Clusters whose curation is confirmed (a /diagnose-issue-cluster verdict) keep
    their membership, canonical, and id — only their pain is restamped, ranked in
    the same population as everything else. Only the issues those curated clusters
    don't claim are re-clustered; the uncurated clusters from the prior run are
    dropped and rebuilt. New cluster ids continue past the curated ones so a
    curated cluster keeps a stable id across runs.

    The rebuild is computed in memory and lands in three bulk writes — delete the
    dropped cluster rows, upsert the rebuilt ones, save the issues whose backref
    moved — so a full-corpus pass costs a handful of statements."""
    issues = store.all_issues()
    existing = store.all_issue_clusters()
    curated = {cid: cl for cid, cl in existing.items() if (cl.curation or {}).get("confirmed")}
    claimed = {n for cl in curated.values() for n in cl.members}

    unclaimed = {n: i for n, i in issues.items() if n not in claimed}
    summaries = _summaries(unclaimed)
    # An identifier shared by more than ~1.5% of issues is too generic to mark a
    # duplicate. Floor at 10 so small corpora still cluster. (Mirrors cluster_issues.main.)
    df_max = max(10, round(len(summaries) * 0.015)) if summaries else 10
    groups = cluster_issues.cluster_issues(summaries, df_max=df_max)
    # Curated clusters join the pain population (keyed by the negated store id, which
    # cannot collide with cluster_issues' positive group ids) so every score shares one
    # normalization. Confirmed curation means their membership is trusted as-is.
    curated_groups = [{"id": -cid, "members": list(cl.members), "needs_review": False}
                      for cid, cl in curated.items()]
    pain = rank_pain(groups + curated_groups, issues)

    new_recs: list[dict] = []
    backref: dict[int, int] = {}
    cid = max(curated, default=0)
    for g in groups:
        cid += 1
        members = sorted(int(m) for m in g["members"])
        title = (unclaimed[members[0]].title if members else None) or f"cluster {cid}"
        rec = {"id": cid, "title": title, "subsystem": g["subsystem"],
               "members": members, "canonical": None,
               "needs_review": bool(g["needs_review"]),
               "checked_at": storekit.now()}
        if g["id"] in pain:
            rec["pain"] = pain[g["id"]]
        new_recs.append(rec)
        for m in members:
            backref[m] = cid

    # Every unclaimed issue lands in exactly one group (cluster_issues is total);
    # only the ones whose backref actually moves are written. Claimed issues point
    # at their curated cluster and are untouched.
    moved: list[dict] = []
    for n, iss in unclaimed.items():
        if iss.cluster_id != backref[n]:
            iss.stage_cluster(backref[n])
            moved.append(iss.raw)

    with store.batch():
        store.delete_issue_clusters([c for c in existing if c not in curated])
        store.save_issue_clusters_many(new_recs)
        store.save_issues_many(moved)
        for ccid, cl in curated.items():
            if -ccid in pain and cl.pain != pain[-ccid]:
                cl.set_pain(pain[-ccid])
    store.append_run({"phase": "cluster", "clusters": len(groups),
                      "curated_preserved": len(curated)})
    return len(groups) + len(curated)
