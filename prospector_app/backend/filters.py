# prospector_app/backend/filters.py
"""Filter spec → boolean over a pr_row dict (service.pr_row shape).

ONE place decides whether a PR matches a query. The UI's lane chips, the
granular controls, and the agent search bar all produce a spec; this module is
the single consumer. Pure functions, no store access — callers pass rows in.
"""
from __future__ import annotations

_OPS = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b,
    ">=": lambda a, b: a >= b, ">": lambda a, b: a > b,
}


def num_cmp(row_val, cmp) -> bool:
    """A {op, value} numeric compare; a missing row value never matches."""
    if row_val is None or not isinstance(cmp, dict):
        return False
    op_name = cmp.get("op")
    if not isinstance(op_name, str):
        return False
    op = _OPS.get(op_name)
    val = cmp.get("value")
    if op is None or not isinstance(val, (int, float)):
        return False
    return op(row_val, val)


def _signals(row: dict) -> dict:
    return row.get("signals") or {}


# A PR is "artifact-dominated" when its diff looks huge but is almost all
# generated noise (migration snapshots / locale bundles / lockfiles / vendored).
# Needs a real raw size so a tiny PR that only bumps a lockfile doesn't qualify,
# and a high generated share. Backs the "mostly generated" triage filter — these
# are the PRs a maintainer asks to rebase/gitignore rather than review as-is.
ARTIFACT_MIN_RAW = 2000
ARTIFACT_MIN_RATIO = 0.7


def _artifact_dominated(row: dict) -> bool:
    b = row.get("loc_breakdown")
    if not b or b["raw"] < ARTIFACT_MIN_RAW:
        return False
    return b["artifact"] / b["raw"] >= ARTIFACT_MIN_RATIO


def _loc_value(row: dict, metric: str, scope: str):
    """Lines changed by a PR, as the `loc` filter counts them.

    scope "effective" counts only the lines a human wrote — source + test, with
    generated artifacts (migration snapshots, locale bundles, lockfiles,
    vendored) stripped — from the cached-diff loc_breakdown. It falls back to the
    test/non-test split, then to the aggregate diffstat, so every PR with size
    data stays filterable. scope "all" is the raw diffstat. metric picks
    additions, deletions, or both. Returns None when no size is known.
    """
    add = dele = None
    if scope == "effective":
        b = row.get("loc_breakdown")
        if b:
            by = b["by_category"]
            add = sum(by.get(c, {}).get("additions", 0) for c in ("source", "test"))
            dele = sum(by.get(c, {}).get("deletions", 0) for c in ("source", "test"))
        else:                                 # no breakdown yet — best available
            nt = (row.get("size_split") or {}).get("non_test")
            if nt is not None:
                add, dele = nt.get("additions"), nt.get("deletions")
    if add is None and dele is None:          # "all", or effective with no diff data
        d = _signals(row)
        add, dele = d.get("additions"), d.get("deletions")
    if metric == "additions":
        return add
    if metric == "deletions":
        return dele
    if add is None and dele is None:
        return None
    return (add or 0) + (dele or 0)


def matches(row: dict, spec: dict) -> bool:
    d = _signals(row)
    if "q" in spec and spec["q"]:
        needle = str(spec["q"]).lower()
        hay = (row.get("title") or "").lower(), (row.get("author") or "").lower()
        if not (needle in hay[0] or needle in hay[1] or needle == str(row.get("number"))):
            return False
    if spec.get("safety"):
        safety_spec = spec["safety"]
        if isinstance(safety_spec, list):
            row_safety = row.get("safety")
            row_fresh = row.get("safety_fresh")
            if not any(
                (v == "not-run" and (row_safety is None or not row_fresh))
                or (v != "not-run" and (row_safety or "").upper() == v.upper())
                for v in safety_spec
            ):
                return False
        elif safety_spec == "not-run":
            if row.get("safety") is not None and row.get("safety_fresh"):
                return False
        elif (row.get("safety") or "").upper() != str(safety_spec).upper():
            return False
    if spec.get("drift"):
        drift_spec = spec["drift"]
        row_drift = row.get("drift_state")
        if isinstance(drift_spec, list):
            if row_drift not in drift_spec:
                return False
        elif row_drift != drift_spec:
            return False
    if spec.get("disposition"):
        disp_spec = spec["disposition"]
        row_disp = row.get("disposition")
        if isinstance(disp_spec, list):
            if row_disp not in disp_spec:
                return False
        elif row_disp != disp_spec:
            return False
    if spec.get("paths"):
        # case-insensitive substring over the PR's changed file paths (from the
        # cached diff). A PR with no cached diff has [] and never matches.
        needle = str(spec["paths"]).strip().lower()
        if needle and not any(needle in p.lower() for p in (row.get("changed_paths") or [])):
            return False
    if spec.get("numbers") is not None:
        # restrict to an explicit PR-number set — how Deep Search renders its
        # agent-judged result set through the normal (sorted, paginated) engine.
        if row.get("number") not in spec["numbers"]:
            return False
    if spec.get("cluster") is not None and spec["cluster"] not in (row.get("clusters") or []):
        return False
    if spec.get("cluster_none") and (row.get("clusters") or []):
        return False
    if spec.get("author") and not (row.get("author") or "").lower().startswith(str(spec["author"]).lower()):
        return False
    if spec.get("ci"):
        ci_spec = spec["ci"]
        ci_row = d.get("ci")
        if isinstance(ci_spec, list):
            if ci_row not in ci_spec:
                return False
        elif ci_row != ci_spec:
            return False
    if spec.get("checks"):
        # Per-check filter (#578): each clause narrows on one named check
        # (its stable `key`, from pr_checks.checks_for_record) to one or more
        # of passed/failed/never-ran; clauses AND together, statuses within a
        # clause OR. A check that never ran for this PR — either because the
        # phase hasn't run, or its rolled-up status is "na" — reads as
        # never_ran; "warn" (a caution short of a clean pass, e.g. a stale
        # security verdict) counts as failed, since it isn't a clean pass.
        row_checks = {c.get("key"): c.get("status") for c in (row.get("checks") or {}).get("checks") or []}
        for clause in spec["checks"]:
            if not isinstance(clause, dict):
                continue
            key = clause.get("key")
            want = clause.get("status")
            if not key or not want:
                continue
            raw = row_checks.get(key)
            effective = "never_ran" if raw is None or raw == "na" else "pass" if raw == "pass" else "fail"
            want_list = want if isinstance(want, list) else [want]
            if effective not in want_list:
                return False
    if "conflicts" in spec and bool(d.get("conflicts")) != bool(spec["conflicts"]):
        return False
    if "has_tests" in spec and bool(d.get("has_tests")) != bool(spec["has_tests"]):
        return False
    if "draft" in spec and bool(row.get("draft")) != bool(spec["draft"]):
        return False
    if "trusted_author" in spec and bool(row.get("trusted_author")) != bool(spec["trusted_author"]):
        return False
    if "clean" in spec and bool(row.get("clean")) != bool(spec["clean"]):
        return False
    if spec.get("threat") and (row.get("threat") or "clear") != spec["threat"]:
        return False
    if "greptile" in spec:
        # A PR Greptile hasn't reviewed yet has greptile=None (shown "—" in the
        # UI); treat it as 0 so an unreviewed PR counts as "below X".
        greptile = d.get("greptile")
        if not num_cmp(0 if greptile is None else greptile, spec["greptile"]):
            return False
    if "greptile_stale" in spec:
        # "stale" (True) keeps only PRs whose stored reviewed SHA predates the
        # head; "current" (False) keeps only PRs whose reviewed SHA equals the
        # head. Unknown staleness (None — no reviewed SHA stored) matches
        # neither, so "current" guarantees the score reflects the head rather
        # than merely the absence of evidence that it doesn't.
        if d.get("greptile_stale") is not bool(spec["greptile_stale"]):
            return False
    if spec.get("reviewer_status"):
        # {reviewer id: status | [statuses]} against the row's reviewer digests;
        # a reviewer with no digest on this row matches nothing.
        digests = row.get("reviews") or {}
        for rid, want in spec["reviewer_status"].items():
            wanted = want if isinstance(want, list) else [want]
            have = (digests.get(rid) or {}).get("status")
            if have is None or have not in wanted:
                return False
    if spec.get("greptile_severity") and d.get("greptile_severity") != spec["greptile_severity"]:
        # "nits"/"defects" keeps only PRs whose Greptile feedback was classified
        # that way; a PR with no classification yet ("clean", or unclassified —
        # None) matches neither, since there's no finding to judge.
        return False
    if "age_days" in spec:
        # A PR with no computable age has age_days=None (shown "—" in the UI);
        # treat it as 0 so it counts as "below X" (newer than X).
        age_days = row.get("age_days")
        if not num_cmp(0 if age_days is None else age_days, spec["age_days"]):
            return False
    if "pain" in spec:
        # A PR with no community-pain signal has pain_score=None (shown "—" in
        # the UI); treat it as 0 so it counts as "below X".
        pain_score = row.get("pain_score")
        if not num_cmp(0 if pain_score is None else pain_score, spec["pain"]):
            return False
    if "author_rate" in spec:
        # An author with no decided PRs has merge_rate=None (shown "—" in the UI);
        # treat it as 0% so an unproven author counts as "below X%".
        rate = (row.get("author_stats") or {}).get("merge_rate")
        if not num_cmp(0.0 if rate is None else rate, spec["author_rate"]):
            return False
    if "max_files" in spec and (d.get("changed_files") or 99) > spec["max_files"]:
        return False
    if "max_total_lines" in spec:
        total = (d.get("additions") or 0) + (d.get("deletions") or 0)
        if total > spec["max_total_lines"]:
            return False
    if isinstance(spec.get("loc"), dict):
        loc = spec["loc"]
        op = _OPS.get(loc.get("op"))
        val = loc.get("value")
        # an open control with no value entered yet doesn't filter
        if op is not None and isinstance(val, (int, float)):
            rv = _loc_value(row, loc.get("metric", "both"), loc.get("scope", "effective"))
            if rv is None or not op(rv, val):
                return False
    if isinstance(spec.get("files"), dict):
        files = spec["files"]
        op = _OPS.get(files.get("op"))
        val = files.get("value")
        # an open control with no value entered yet doesn't filter
        if op is not None and isinstance(val, (int, float)):
            rv = d.get("changed_files")
            if rv is None or not op(rv, val):
                return False
    if spec.get("artifact_dominated") and not _artifact_dominated(row):
        return False
    if "risk_tier" in spec:
        # path-based blast-radius tier (0 = core … 3 = leaf). A PR with no
        # cached diff has tier None (unknown) and never matches a tier filter.
        tier_spec = spec["risk_tier"]
        row_tier = row.get("risk_tier")
        if row_tier is None:
            return False
        if isinstance(tier_spec, list):
            if row_tier not in tier_spec:
                return False
        elif row_tier != tier_spec:
            return False
    if "merge_ok" in spec and bool((row.get("merge_gate") or {}).get("ok")) != bool(spec["merge_ok"]):
        return False
    if "has_summary" in spec:
        # whether an agent summary exists for the PR (the CLUSTER phase's
        # diff-grounded one-liner)
        one_liner = (row.get("summary") or {}).get("one_liner")
        if bool(one_liner) != bool(spec["has_summary"]):
            return False
    if "has_issues" in spec and bool(row.get("issues")) != bool(spec["has_issues"]):
        return False
    if spec.get("responses"):
        # how the community responded to our triage since we acted; "any" = a
        # response of any kind, else a specific signal (reopened/new_commits/replied/resubmitted).
        # An acked signal is one an operator has marked seen: the signal still
        # renders and carries the ack, but is out of the queue this filter serves.
        resp = row.get("responses")
        want = spec["responses"]
        if not resp or resp.get("ack"):
            return False
        want_list = want if isinstance(want, list) else [want]
        if not any(v == "any" or resp.get(v) for v in want_list):
            return False
    return True
