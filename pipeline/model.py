"""Domain model over the store: typed, invariant-owning mutation of PR/cluster
records. These objects are the ONLY way disposition and security state should be
changed — each method mutates, stamps, self-corrects, and persists in one call, so
an inconsistent record can't be produced from a call site.

store.load_pr/all_prs/load_cluster/all_clusters return these objects; readers use
the typed properties and mutators use the typed methods. model never imports the
`store` module (it receives a Store instance), which keeps `store -> model` acyclic;
it owns a local stamp helper for the same reason.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pipeline import gates
from pipeline import storekit

if TYPE_CHECKING:
    from pipeline.store import Store
    from pipeline.wire import Finding


def _stamp(rec: dict, section: str, payload: dict, against_head_sha: str | None) -> None:
    """Stage one section of `rec` in memory, stamping checked_at and (for non-meta
    sections) the head the fact was computed against. Does not persist — typed
    methods stage one or more sections this way then persist once, so a
    multi-section mutation is a single validated write."""
    token_field = None if section == "meta" else "against_head_sha"
    token_value = (against_head_sha or rec["meta"]["head_sha"]) if token_field else None
    storekit.stamp(rec, section, payload, token_field, token_value)


class Pr:
    """A store-bound, auto-saving wrapper around one PR record."""

    def __init__(self, store: Store | None, rec: dict):
        self._store = store
        self.rec = rec

    @property
    def n(self) -> int:
        return self.rec["pr"]

    @property
    def raw(self) -> dict:
        """The underlying record dict — the ONLY sanctioned raw-dict access, for
        store-layer serialization, model-internal ops, and test fixtures ONLY.
        Never call `.raw` from production code outside model.py/store.py: read a
        typed property and mutate through a typed method."""
        return self.rec

    # -- typed read surface (lazy over self.rec) --------------------------------
    @property
    def number(self) -> int:
        return self.rec["pr"]

    def section(self, name: str) -> dict | None:
        """The raw section dict by name, or None — used by freshness (parametric
        over section name) and any reader needing a whole section."""
        return self.rec.get(name)

    def _meta(self) -> dict:
        return self.rec.get("meta") or {}

    def _analysis(self) -> dict:
        return self.rec.get("analysis") or {}

    @property
    def state(self) -> str | None:
        """The PR's open/closed/merged state as stored — set by INGEST and updated
        in place by the app's live sweep and the executor's own actions, so the
        board and gates act on the latest GitHub-owned truth every operator shares."""
        return self._meta().get("state")

    @property
    def unresolvable(self) -> bool:
        """True when GitHub reports this PR's number cannot resolve to a
        PullRequest — the PR was deleted upstream (e.g. a spam scrub). Set by
        the live sweep; an unresolvable PR is excluded from every sweep target
        set, since no fetch can ever reach it again."""
        return bool(self._meta().get("unresolvable"))

    @property
    def head_sha(self) -> str | None:
        """The head SHA we have ingested a diff and signals for. Always mirrors
        the DB, and is what every phase computes its facts against."""
        return self._meta().get("head_sha")

    @property
    def live_head_sha(self) -> str | None:
        """The head GitHub reported at the last live observation. Written by the
        app's live sweep, which sees a push long before an ingest fetches the
        diff behind it; `meta` is rebuilt wholesale at ingest, so catching up
        drops this field."""
        return self._meta().get("live_head_sha")

    @property
    def effective_head_sha(self) -> str | None:
        """The PR's head as last observed upstream, falling back to the ingested
        head. Freshness tokens against this, so a push the sweep has seen makes
        every sha-bound fact read stale for every consumer at once."""
        return self.live_head_sha or self.head_sha

    @property
    def draft(self) -> bool:
        return bool(self._meta().get("draft", False))

    @property
    def author(self) -> str | None:
        return self._meta().get("author")

    @property
    def title(self) -> str | None:
        return self._meta().get("title")

    @property
    def url(self) -> str | None:
        return self._meta().get("url")

    @property
    def base(self) -> str | None:
        return self._meta().get("base")

    @property
    def created_at(self) -> str | None:
        return self._meta().get("created_at")

    @property
    def updated_at(self) -> str | None:
        return self._meta().get("updated_at")

    @property
    def body(self) -> str | None:
        return self._meta().get("body")

    @property
    def comments(self) -> int | None:
        return self._meta().get("comments")

    @property
    def reactions_total(self) -> int | None:
        return self._meta().get("reactions_total")

    @property
    def signals(self) -> dict | None:
        return self.rec.get("signals")

    @property
    def reviews(self) -> dict | None:
        """Every automated reviewer's normalized feedback on this PR, keyed by
        reviewer id (pipeline.reviewers), stamped against the head it was read at."""
        return self.rec.get("reviews")

    def review_entry(self, reviewer_id: str) -> dict | None:
        entry = (self.rec.get("reviews") or {}).get(reviewer_id)
        return entry if isinstance(entry, dict) else None

    @property
    def greptile(self) -> int | None:
        return (self.review_entry("greptile") or {}).get("score")

    @property
    def greptile_reviewed_sha(self) -> str | None:
        """The commit Greptile's score describes; None when unknown."""
        return (self.review_entry("greptile") or {}).get("reviewed_sha")

    @property
    def greptile_severity(self) -> str | None:
        """Severity of the semantic Greptile-findings read: "defects" | "nits" |
        "clean", or None when the read has not run. Lives in the greptile_review
        section as its single source of truth."""
        return (self.rec.get("greptile_review") or {}).get("severity")

    @property
    def greptile_review(self) -> dict | None:
        """The semantic Greptile-findings digest (severity + per-finding classes),
        stamped against the head it read. None when the pass has not run."""
        return self.rec.get("greptile_review")

    @property
    def greptile_stale(self) -> bool | None:
        """True when Greptile's score is from an older commit than the PR's
        current head, False when current, None when unknown (no reviewed SHA
        stored). A stale score predates commits the author may have pushed in
        response to Greptile's feedback."""
        reviewed = self.greptile_reviewed_sha
        if not reviewed:
            return None
        return reviewed != self.head_sha

    @property
    def ci(self) -> str | None:
        return (self.rec.get("signals") or {}).get("ci")

    @property
    def mergeable(self) -> bool | None:
        """Whether the PR merges cleanly, read from signals — set by INGEST and
        refreshed in place by the live sweep, so a PR that developed conflicts after
        analysis reads as not-mergeable and the merge gate blocks it (#189)."""
        return (self.rec.get("signals") or {}).get("mergeable")

    @property
    def disposition(self) -> str | None:
        """The PR's effective disposition: the stored ANALYZE verdict, with the
        route any current fact forces on a merge pick (a security verdict, a
        verify outcome, the quality-gate merge bar — gates.merge_demotion)
        derived at read time. Nothing derived is stored, so a re-run, a logged
        override, or a signal refresh is reflected immediately."""
        stored = self._analysis().get("disposition")
        if stored != "merge":
            return stored
        route = gates.merge_demotion(self)
        return route[0] if route is not None else "merge"

    @property
    def rationale(self) -> str | None:
        """The effective rationale: a forced security/verify route supplies its
        own; otherwise the stored ANALYZE rationale (a quality-gate block keeps
        it — the gap speaks through the derived asks)."""
        a = self._analysis()
        if a.get("disposition") == "merge":
            route = gates.merge_demotion(self)
            if route is not None and route[1] is not None:
                return route[1]
        return a.get("rationale")

    @property
    def asks(self) -> list | None:
        """The effective author asks: the stored ANALYZE asks, with the
        quality-gate asks a blocked merge pick derives appended."""
        a = self._analysis()
        asks = a.get("asks")
        if a.get("disposition") == "merge":
            route = gates.merge_demotion(self)
            if route is not None and route[2]:
                return (asks or []) + route[2]
        return asks

    @property
    def canonical(self) -> int | None:
        return self._analysis().get("canonical")

    @property
    def upstream_pr(self) -> int | None:
        return self._analysis().get("upstream_pr")

    @property
    def upstream_commit(self) -> str | None:
        return self._analysis().get("upstream_commit")

    @property
    def upstream_date(self) -> str | None:
        return self._analysis().get("upstream_date")

    @property
    def security_verdict(self) -> str | None:
        return (self.rec.get("security") or {}).get("verdict")

    @property
    def findings(self) -> list[Finding]:
        return (self.rec.get("security") or {}).get("findings") or []

    @property
    def security_override(self) -> dict | None:
        return (self.rec.get("security") or {}).get("override")

    @property
    def verify_outcome(self) -> str | None:
        """The VERIFY outcome, or None — which is a real state ("blind adequacy
        committed, not yet judged"), not just an absent section."""
        return (self.rec.get("verify") or {}).get("outcome")

    @property
    def verify_signals(self) -> dict:
        """The four VERIFY signals, kept separate. `*_output_tail` values inside
        are untrusted, attacker-influenced text — never derive a verdict from them."""
        return (self.rec.get("verify") or {}).get("signals") or {}

    @property
    def verify_base_sha(self) -> str | None:
        return (self.rec.get("verify") or {}).get("against_base_sha")

    @property
    def verify_findings(self) -> list[dict]:
        return (self.rec.get("verify") or {}).get("findings") or []

    @property
    def verify_override(self) -> dict | None:
        return (self.rec.get("verify") or {}).get("override")

    @property
    def verify_request(self) -> dict | None:
        """The operator's sandbox-verification queue state for this PR
        ({status, queued_at, ...}), or None when never queued."""
        return self.rec.get("verify_request")

    @property
    def fix_request(self) -> dict | None:
        """This PR's autofix queue state ({status, action, queued_at, ...}), or
        None when never queued."""
        return self.rec.get("fix_request")

    @property
    def drift_state(self) -> str | None:
        return (self.rec.get("drift") or {}).get("state")

    @property
    def threat_verdict(self) -> str | None:
        return (self.rec.get("threat") or {}).get("verdict")

    @property
    def threat_signatures(self) -> list:
        return (self.rec.get("threat") or {}).get("signatures") or []

    @property
    def linked_issues(self) -> list:
        return (self.rec.get("issues") or {}).get("linked") or []

    @property
    def cluster_ids(self) -> list[int]:
        """The clusters this PR belongs to (sorted). Empty for a standalone or
        unconsidered PR. A PR may straddle several clusters (#196)."""
        return list((self.rec.get("cluster") or {}).get("ids") or [])

    @property
    def primary_cluster_id(self) -> int | None:
        """The lowest cluster id this PR belongs to, or None. A display/grouping
        convenience (e.g. the activity-log tag) — NOT a policy concept; nothing
        about disposition depends on it."""
        ids = self.cluster_ids
        return ids[0] if ids else None

    def _persist(self) -> None:
        assert self._store is not None, "a store-less Pr view is read-only"
        self._store.save_pr(self.rec)

    def route_to(self, disposition: str, rationale: str, *, asks: list | None = None,
                 canonical: int | None = None, upstream_pr: int | None = None,
                 upstream_commit: str | None = None, upstream_date: str | None = None,
                 head_sha: str | None = None, from_cluster: int | None = None) -> None:
        """Set this PR's analysis disposition — ANALYZE's verdict, stored
        verbatim. The route current facts force on a merge pick (a security
        verdict, a verify outcome, the quality-gate merge bar) is derived at
        read time by the disposition/rationale/asks properties
        (gates.merge_demotion), never stored."""
        section: dict = {"disposition": disposition, "rationale": rationale}
        if from_cluster is not None:
            section["from_cluster"] = int(from_cluster)
        if canonical is not None:
            section["canonical"] = int(canonical)
        if upstream_pr is not None:
            section["upstream_pr"] = int(upstream_pr)
        if upstream_commit:
            section["upstream_commit"] = str(upstream_commit)
        if upstream_date:
            section["upstream_date"] = str(upstream_date)
        cleaned = [a for a in (asks or []) if a]
        if cleaned:
            section["asks"] = cleaned
        _stamp(self.rec, "analysis", section, head_sha)
        self._persist()

    def record_security(self, verdict: str, findings: list[Finding], *, tier: str = "adversarial",
                        override: dict | None = None, head_sha: str | None = None) -> None:
        """Record a security verdict. Its disposition consequence on a merge pick
        (YELLOW -> request-changes, RED -> needs-human) is derived at read time
        by the disposition/rationale properties (gates.merge_demotion), so a
        re-run or override that clears the verdict clears the route with it."""
        head = head_sha or self.rec["meta"]["head_sha"]
        _stamp(self.rec, "security",
               {"verdict": verdict, "findings": findings, "tier": tier, "override": override},
               head)
        self._persist()

    def record_verify(self, outcome: str | None, signals: dict, *,
                      findings: list[dict] | None = None, tier: int = 0,
                      base_sha: str, head_sha: str | None = None) -> None:
        """Record a VERIFY outcome. The section is stamped against BOTH the PR
        head (freshness) and the base `base_sha` the run booted, which names
        what the outcome was proven against — each machine pins its own base
        and tracks the default branch on its own cadence. Its disposition
        consequence on a merge pick (escalate/
        deps-touched -> needs-human, not-verified/needs-rebase/regressed ->
        request-changes) is derived at read time by the disposition/rationale
        properties (gates.merge_demotion), so a re-run that clears the outcome
        clears the route with it.

        `outcome=None` records the blind adequacy verdict before any sandbox runs:
        a real state that fails the merge bar and forces no route."""
        head = head_sha or self.rec["meta"]["head_sha"]
        _stamp(self.rec, "verify",
               {"outcome": outcome, "signals": signals, "findings": findings or [],
                "tier": tier, "against_base_sha": base_sha},
               head)
        self._persist()

    def record_verify_request(self, status: str, *, queued_at: str | None = None,
                              started_at: str | None = None, finished_at: str | None = None,
                              step: str | None = None, error_kind: str | None = None,
                              error: str | None = None, log_tail: str | None = None,
                              attempts: int | None = None, source: str | None = None,
                              host: str | None = None,
                              head_sha: str | None = None) -> None:
        """Record this PR's sandbox-verification queue state (the verify_request
        section): the app writes `queued`/`cancelled`, the verify_pr
        orchestrator advances it through `running` (with `step`) to `done` or
        `error` (with error_kind/error/log_tail), parks it `waiting-for-base`
        (with error_kind/error) for the worker to retry, or re-queues a
        transient failure as `queued` with `attempts` counting the runs so far
        (plus error_kind/error naming the failure). Replaces the whole section —
        callers carry forward the fields still true (queued_at across a run's
        transitions). source records who queued the request ("auto" for the idle hunter;
        the operator path leaves it unset). host records the machine that ran (or
        is running) the request; orphan recovery touches only its own host's
        leftovers. None-valued fields are omitted, so the stored record
        carries only what is set."""
        section: dict = {"status": status}
        for field, value in (("queued_at", queued_at), ("started_at", started_at),
                             ("finished_at", finished_at), ("step", step),
                             ("error_kind", error_kind), ("error", error),
                             ("log_tail", log_tail), ("attempts", attempts),
                             ("source", source), ("host", host)):
            if value is not None:
                section[field] = value
        _stamp(self.rec, "verify_request", section, head_sha)
        self._persist()

    def record_fix_request(self, status: str, action: str, *, queued_at: str | None = None,
                           started_at: str | None = None, finished_at: str | None = None,
                           step: str | None = None, error: str | None = None,
                           refused_reason: str | None = None,
                           result: dict | None = None, attempts: int | None = None,
                           source: str | None = None, host: str | None = None,
                           base_sha: str | None = None,
                           guidance: str | None = None,
                           head_sha: str | None = None) -> None:
        """Record this PR's autofix queue state (the fix_request section): any app
        writes `queued`/`cancelled`/`approved`, the fix worker advances it through
        `running` (with `step`) to `awaiting-review` (with `result` carrying the
        authored patch, commit message, and preflight verdict), then `pushing` and
        `pushed`, or ends it `refused` (with refused_reason — a gate or preflight
        blocked it, nothing reached upstream) or `failed` (with error). Replaces
        the whole section — callers carry forward the fields still true (queued_at
        and action across a run's transitions). base_sha pins the base the action
        was computed against; head_sha stamps the contributor head it applies to,
        so a head that moves invalidates the request. source records who queued it
        ("auto" for the idle hunter; the operator path leaves it unset). guidance
        carries the operator's own instruction for an agent-authored fix, which
        the worker authors from and which is what authorizes the fix at all.
        host records the machine that ran it. None-valued fields are omitted, so
        the stored record carries only what is set."""
        section: dict = {"status": status, "action": action}
        for field, value in (("queued_at", queued_at), ("started_at", started_at),
                             ("finished_at", finished_at), ("step", step),
                             ("error", error), ("refused_reason", refused_reason),
                             ("result", result), ("attempts", attempts),
                             ("source", source), ("host", host),
                             ("base_sha", base_sha), ("guidance", guidance)):
            if value is not None:
                section[field] = value
        _stamp(self.rec, "fix_request", section, head_sha)
        self._persist()

    def log_security_override(self, reason: str, *, by: str | None = None) -> None:
        """Log an operator override on the recorded security verdict: set
        security.override = {reason, by, at}. The override annotates the existing
        verdict — checked_at and against_head_sha stay untouched, so it neither
        extends the verdict's merge-recency window nor survives a re-review
        (record_security replaces the whole section). The gates honor it: an
        overridden verdict no longer blocks merge_eligibility or forces a
        disposition (gates.security_disposition returns None). Raises ValueError
        on a blank reason or when the PR has no security verdict to override."""
        if not (reason or "").strip():
            raise ValueError("security override requires a non-empty reason")
        sec = self.rec.get("security")
        if not sec:
            raise ValueError(f"PR #{self.n} has no security verdict to override")
        sec["override"] = {"reason": reason.strip(), "by": by, "at": storekit.now()}
        self._persist()

    def log_verify_override(self, reason: str, *, by: str | None = None) -> None:
        """Log an operator override on the recorded VERIFY outcome: set
        verify.override = {reason, by, at}. Only an `escalate` outcome — "a human
        must decide" — is meant to be overridden this way; the gate enforces
        that. The override annotates the existing record (checked_at /
        against_head_sha untouched, so it neither extends the merge-recency
        window nor survives a re-verify), and the gates honor it: an overridden
        escalate no longer blocks merge_eligibility or forces needs-human
        (gates.verify_disposition returns None). Raises ValueError on a blank
        reason or when the PR has no verify outcome to override."""
        if not (reason or "").strip():
            raise ValueError("verify override requires a non-empty reason")
        sec = self.rec.get("verify")
        if not sec:
            raise ValueError(f"PR #{self.n} has no verify record to override")
        sec["override"] = {"reason": reason.strip(), "by": by, "at": storekit.now()}
        self._persist()

    def set_meta(self, meta: dict) -> None:
        """Replace the meta section (gh-owned facts). Meta is the one section with no
        against_head_sha stamp — _stamp adds only checked_at for it."""
        _stamp(self.rec, "meta", meta, None)
        self._persist()

    def set_signals(self, signals: dict) -> None:
        """Set the signals section."""
        _stamp(self.rec, "signals", signals, None)
        self._persist()

    def set_drift(self, drift: dict) -> None:
        """Set the drift section."""
        _stamp(self.rec, "drift", drift, None)
        self._persist()

    def set_issues(self, linked: list) -> None:
        """Set the linked-issues section."""
        _stamp(self.rec, "issues", {"linked": linked}, None)
        self._persist()

    def set_reviews(self, payload: dict, *, head_sha: str | None = None) -> None:
        """Stamp every reviewer's entries against the head they were read at."""
        _stamp(self.rec, "reviews", payload, head_sha)
        self._persist()

    def apply_facts(self, meta: dict, *, signals: dict | None = None,
                    drift: dict | None = None, issues: list | None = None,
                    reviews: dict | None = None) -> None:
        """Stamp the gh-owned fact sections and persist them in one save.
        `meta` is always set; `signals`, `drift`, and `issues` are set only when
        provided. A single write lands a PR's whole ingest atomically."""
        self.stage_facts(meta, signals=signals, drift=drift, issues=issues, reviews=reviews)
        self._persist()

    def stage_facts(self, meta: dict, *, signals: dict | None = None,
                    drift: dict | None = None, issues: list | None = None,
                    reviews: dict | None = None) -> None:
        """Stamp ingest-owned facts in memory without persisting.

        Bulk ingest stages the corpus and saves it in chunks; single-PR callers
        use `apply_facts`, which stages through this method and persists once.
        """
        _stamp(self.rec, "meta", meta, None)
        if signals is not None:
            _stamp(self.rec, "signals", signals, None)
        if drift is not None:
            _stamp(self.rec, "drift", drift, None)
        if issues is not None:
            _stamp(self.rec, "issues", {"linked": issues}, None)
        if reviews is not None:
            _stamp(self.rec, "reviews", reviews, None)

    def set_threat(self, result: dict) -> None:
        """Set the threat-scan verdict section."""
        _stamp(self.rec, "threat", result, None)
        self._persist()

    def set_summary(self, payload: dict, *, head_sha: str | None = None) -> None:
        """Set the diff summary, stamped against the head it was computed on
        (head_sha) so a moved head correctly stales it."""
        _stamp(self.rec, "summary", payload, head_sha)
        self._persist()

    def set_greptile_review(self, payload: dict, *, head_sha: str | None = None) -> None:
        """Stamp the semantic Greptile digest against the head it read. Its
        `severity` verdict ("defects" | "nits" | "clean") lives in this section as
        the single source of truth the Explorer's severity column/filter reads."""
        _stamp(self.rec, "greptile_review", payload, head_sha)
        self._persist()

    def mark_standalone(self) -> None:
        """Stamp a cluster section with an empty id-list — a PR a clustering pass
        considered but placed in no cluster (a confirmed singleton). Membership
        (adding real ids) is owned by Cluster, never set here."""
        _stamp(self.rec, "cluster", {"ids": []}, None)
        self._persist()

    def clear_cluster(self) -> None:
        """Remove the cluster section entirely (the section becomes absent, not an
        empty standalone stamp) — used by a from-scratch re-cluster reset."""
        self.rec.pop("cluster", None)
        self._persist()

    def record_live_state(self, *, state: str | None = None,
                          ci: str | None = None,
                          mergeable: bool | None = None,
                          diffstat: dict | None = None,
                          has_tests: bool | None = None,
                          live_head_sha: str | None = None) -> None:
        """Persist GitHub-owned live facts into the shared store: the PR's
        open/closed/merged `meta.state`, the head GitHub reports
        (`meta.live_head_sha`), and its `signals` verdicts — `ci`, `mergeable`,
        `diffstat` ({additions, deletions, changed_files}), and `has_tests`. The
        app's live sweep and executor actions call this so upstream drift is
        shared with every operator at once — no per-machine overlay. Each touched
        section is restamped; signals keeps its `against_head_sha`, so the
        freshness check still anchors it to the same head. One validated write.

        `live_head_sha` is the head as just observed upstream. It is retained
        only while it differs from the ingested `head_sha`; an observation that
        agrees clears it, so a force-push back to the ingested head heals the
        freshness read instead of pinning it stale."""
        meta_next = dict(self.section("meta") or {})
        meta_dirty = False
        if state is not None:
            meta_next["state"] = state
            meta_dirty = True
        if live_head_sha is not None:
            observed = live_head_sha if live_head_sha != meta_next.get("head_sha") else None
            if observed != meta_next.get("live_head_sha"):
                if observed is None:
                    meta_next.pop("live_head_sha", None)
                else:
                    meta_next["live_head_sha"] = observed
                meta_dirty = True
        if meta_dirty:
            _stamp(self.rec, "meta", meta_next, None)
        if (ci is not None or mergeable is not None
                or diffstat is not None or has_tests is not None):
            signals = dict(self.section("signals") or {})
            if ci is not None:
                signals["ci"] = ci
            if mergeable is not None:
                signals["mergeable"] = mergeable
            if diffstat is not None:
                signals["diffstat"] = diffstat
            if has_tests is not None:
                signals["has_tests"] = has_tests
            _stamp(self.rec, "signals", signals, None)
        self._persist()

    def record_unresolvable(self) -> None:
        """Mark this PR unresolvable upstream — GitHub reports its number cannot
        resolve to a PullRequest (the PR was deleted, e.g. a spam scrub). Stamps
        `meta.unresolvable` and moves an open `meta.state` to closed, so the PR
        leaves the active queue and every live-sweep target set. A merged or
        closed state is kept. Idempotent: an already-marked record with a
        non-open state is left unwritten."""
        meta = dict(self.section("meta") or {})
        if meta.get("unresolvable") and meta.get("state") != "open":
            return
        meta["unresolvable"] = True
        if meta.get("state") == "open":
            meta["state"] = "closed"
        _stamp(self.rec, "meta", meta, None)
        self._persist()


class Cluster:
    """A store-bound, auto-saving wrapper around one cluster record. Owns the
    two-way membership link (cluster.prs <-> each member's pr.cluster.ids)."""

    def __init__(self, store: Store, rec: dict):
        self._store = store
        self.rec = rec

    @property
    def id(self) -> int:
        return self.rec["id"]

    @property
    def raw(self) -> dict:
        """The underlying record dict — the ONLY sanctioned raw-dict access, for
        store-layer serialization, model-internal ops, and test fixtures ONLY.
        Never call `.raw` from production code outside model.py/store.py."""
        return self.rec

    @property
    def prs(self) -> list:
        return self.rec.get("prs") or []

    @property
    def proposals(self) -> list[dict]:
        """Each member's per-cluster proposed disposition row, retained so a PR
        that straddles several clusters can be reconciled (#196)."""
        return self.rec.get("proposals") or []

    def proposal_for(self, n: int) -> dict | None:
        """This cluster's proposed row for PR `n`, or None."""
        return next((p for p in self.proposals if int(p.get("pr", -1)) == int(n)), None)

    def set_proposals(self, rows: list[dict]) -> None:
        """Store this cluster's per-member proposed dispositions (validated on
        save)."""
        self.rec["proposals"] = list(rows)
        self._persist()

    @property
    def root_problem(self) -> str | None:
        return self.rec.get("root_problem")

    @property
    def outcome(self) -> str | None:
        return self.rec.get("outcome")

    @property
    def checked_at(self) -> str | None:
        return self.rec.get("checked_at")

    @property
    def rationale(self) -> str | None:
        return self.rec.get("rationale")

    @property
    def rationale_summary(self) -> str | None:
        """A one-sentence TL;DR generated for display. Unlike `rationale` (the
        analyst's verbatim record), this is freshly written prose, not gated for
        content-identity — it sits above the rationale as a scannable headline."""
        return self.rec.get("rationale_summary")

    @property
    def notes(self) -> str | None:
        return self.rec.get("notes")

    def _persist(self) -> None:
        self.rec["checked_at"] = storekit.now()
        self._store.save_cluster(self.rec)

    def set_outcome(self, outcome: str | None) -> None:
        """Set (or reset, with None) the cluster outcome. Validated on save."""
        self.rec["outcome"] = outcome
        self._persist()

    def add_member(self, n: int) -> None:
        """Add PR `n` to this cluster and union this cluster's id into the PR's
        membership list. Idempotent; preserves the PR's other memberships."""
        n = int(n)
        if n not in self.rec["prs"]:
            self.rec["prs"] = sorted(set(self.rec["prs"]) | {n})
            self._persist()
        pr = self._store.edit_pr(n)
        ids = sorted(set(pr.cluster_ids) | {self.id})
        _stamp(pr.rec, "cluster", {"ids": ids}, None)
        pr._persist()

    def remove_member(self, n: int) -> None:
        """Remove PR `n` from this cluster and drop this cluster's id from the
        PR's membership list, leaving its other memberships intact."""
        n = int(n)
        if n in self.rec["prs"]:
            self.rec["prs"] = [x for x in self.rec["prs"] if x != n]
            self._persist()
        pr = self._store.load_pr(n)
        if pr and self.id in pr.cluster_ids:
            ids = [i for i in pr.cluster_ids if i != self.id]
            _stamp(pr.rec, "cluster", {"ids": ids}, None)
            self._store.save_pr(pr.rec)

    def set_members(self, members: list[int]) -> None:
        """Reconcile membership to exactly `members`: add the missing (wiring each
        PR's backref), remove the departed (clearing the backref iff it still points
        here). Does not touch outcome/root_problem."""
        want = {int(m) for m in members}
        current = {int(x) for x in self.rec.get("prs") or []}
        for n in sorted(want - current):
            self.add_member(n)
        for n in sorted(current - want):
            self.remove_member(n)

    def set_root_problem(self, text: str) -> None:
        """Set this cluster's root-problem text. Validated on save."""
        self.rec["root_problem"] = text
        self._persist()

    def record_analysis(self, outcome: str | None, rationale: str | None, notes: str | None) -> None:
        """Write the ANALYZE per-cluster result — outcome + rationale + notes — in
        one validated save. `outcome` is validated against OUTCOMES on save."""
        self.rec["outcome"] = outcome
        self.rec["rationale"] = rationale
        self.rec["notes"] = notes
        # the display summary is derived from the old rationale — drop it so a
        # re-analysis never shows a TL;DR for text that no longer exists.
        self.rec.pop("rationale_summary", None)
        self._persist()

    def record_reformat(self, rationale: str, summary: str | None) -> None:
        """Store a display-reformatted `rationale` + one-line `summary` WITHOUT
        re-stamping checked_at — no analysis happened, only presentation. The
        reformat driver gates `rationale` to be the same words as before; `summary`
        is freshly generated display prose. Bypasses _persist precisely to keep the
        analyzed date honest."""
        self.rec["rationale"] = rationale
        self.rec["rationale_summary"] = summary
        self._store.save_cluster(self.rec)
