"""Deterministic first-pass clustering by subsystem + shared identifiers.

This is the cheap, total pass: every issue lands somewhere. LLM-curated
refinement (confirm / split / merge) happens later in /diagnose-issue-cluster,
exactly as the PR pipeline curates cluster_local.py output in cluster_semantic.py.
"""
from collections import defaultdict


def _document_frequency(summaries):
    df = defaultdict(int)
    for s in summaries:
        for i in set(s["identifiers"]):
            df[i] += 1
    return df


SIZE_FLAG = 12  # clusters bigger than this are flagged needs_review (no bulk dedup)


def _edge(ids_a, ids_b, df, rare_df, min_shared):
    """Two issues join if they share a rare identifier (strong evidence) or at
    least `min_shared` discriminative identifiers. A single coincidentally shared
    common field name is not enough — that is what chained mega-clusters."""
    shared = ids_a & ids_b
    if not shared:
        return False
    if any(df[i] <= rare_df for i in shared):
        return True
    return len(shared) >= min_shared


def cluster_issues(summaries, df_max=None, rare_df=6, min_shared=2):
    """Cluster issues by connected components within each subsystem.

    Identifiers with corpus document-frequency > df_max are dropped as too
    generic (companyId @198, POST, HTTP...). Edges require strong overlap (see
    `_edge`) so unrelated issues that merely co-mention a common field don't fuse.
    With df_max=None, no df filtering (unit tests on tiny corpora). The cluster
    driver sets a real threshold. A missed duplicate is safe; a false mega-cluster
    is not.
    """
    df = _document_frequency(summaries)

    def keys(s):
        return {i for i in s["identifiers"] if df_max is None or df[i] <= df_max}

    by_sub = defaultdict(list)
    for s in summaries:
        by_sub[s["subsystem"]].append(s)

    clusters, cid = [], 0
    for sub, members in by_sub.items():
        idsets = {s["number"]: keys(s) for s in members}
        nums = [s["number"] for s in members]
        # union-find over the subsystem's issues
        parent = {n: n for n in nums}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            parent[find(a)] = find(b)

        for i, a in enumerate(nums):
            if not idsets[a]:
                continue
            for b in nums[i + 1:]:
                if idsets[b] and _edge(idsets[a], idsets[b], df, rare_df, min_shared):
                    union(a, b)

        comps = defaultdict(list)
        for n in nums:
            comps[find(n)].append(n)
        for group in comps.values():
            cid += 1
            allids = set().union(*(idsets[n] for n in group)) if group else set()
            clusters.append({"id": cid, "subsystem": sub,
                             "identifiers": sorted(allids), "members": sorted(group),
                             "needs_review": len(group) > SIZE_FLAG})
    return clusters
