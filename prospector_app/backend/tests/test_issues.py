"""#192: the Issues projection over the issue store, the close-as-dup worklist,
and the bot-gated issue-close path. The store is seeded in a temp dir; GitHub is
never touched."""
from prospector_app.backend import executor
from issue_triage import issue_store
from prospector_app.backend import issues
from prospector_app.backend import models
from prospector_app.backend import safety_guard


def _seed(tmp_path, monkeypatch):
    """A canonical (#10) and its confirmed duplicate (#11) in one cluster. PR 900 is
    in the PR store and open (opens in-app); anything else is off-store, its real
    state resolved live (stubbed here — never touches GitHub)."""
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(issues, "_store_pr_states", lambda: {900: "open"})
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {})
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    st = issue_store.IssueStore(tmp_path)
    canon = st.create_issue(10, {"title": "crash on boot", "state": "open", "author": "al",
                                 "labels": ["bug"], "comments": 2, "reactions_total": 3,
                                 "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"})
    canon.set_summary("startup", [])
    canon.set_repro({"grade": "A", "score": 5})
    canon.set_links([{"pr": 900, "title": "fix boot", "how": "subsystem"}])
    st.create_issue(11, {"title": "also crashes on boot", "state": "open", "author": "bo",
                         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
    cl = st.create_issue_cluster(5, "boot crashes", subsystem="startup")
    cl.set_members([10, 11])
    cl.set_pain(0.81)
    cl.record_curation({"confirmed": True, "canonical": 10, "label": "boot crashes"})
    # reload (set_members wrote 11's cluster backref via a fresh instance; routing a
    # stale handle would clobber it)
    st.edit_issue(11).route_to("close-dup", "Duplicate of #10.", canonical=10)
    return st


def test_list_issues_enriches(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    rows = {r["number"]: r for r in issues.list_issues()}
    assert rows[10]["pain"] == 0.81 and rows[10]["repro_grade"] == "A"
    assert rows[10]["linked_prs"] == [
        {"pr": 900, "title": "fix boot", "how": "subsystem", "in_store": True, "state": "open"}]
    assert rows[10]["linked_pr_count"] == 1 and rows[10]["referenced_pr_count"] == 0
    assert rows[10]["cluster_size"] == 2 and rows[10]["is_dup"] is False  # canonical
    assert rows[10]["subsystem"] == "startup"
    assert rows[11]["is_dup"] is True and rows[11]["canonical"] == 10
    assert rows[11]["duplicates"] == [10]


def test_issue_rows_flag_trusted_authors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(
        issues.profile,
        "active",
        lambda: issues.profile.RepoProfile(trusted_authors=("bo",)),
    )

    rows = {r["number"]: r for r in issues.list_issues()}
    assert rows[10]["trusted_author"] is False
    assert rows[11]["trusted_author"] is True
    assert issues.get_issue(11)["trusted_author"] is True

    groups = issues.duplicate_groups()
    assert groups[0]["dups"][0]["trusted_author"] is True


def test_query_issues_pages_and_trims_linked_prs(tmp_path, monkeypatch):
    """The paged rows trim linked PRs to 6, explicit matches sorting ahead of
    subsystem ones so the trim never drops them."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(10).set_links(
        [{"pr": n, "title": f"fix {n}", "how": "subsystem"} for n in range(900, 909)]
        + [{"pr": 909, "title": "fix 909", "how": "explicit"}])
    out = issues.query_issues(sort="number", direction="asc", offset=0, limit=1)
    assert out["total"] == 2
    assert [r["number"] for r in out["items"]] == [10]
    row = out["items"][0]
    assert row["linked_pr_count"] == 10
    assert row["referenced_pr_count"] == 1
    assert [p["pr"] for p in row["linked_prs"]] == [909, 900, 901, 902, 903, 904]


def test_query_issues_search_and_sort(tmp_path, monkeypatch):
    st = _seed(tmp_path, monkeypatch)
    extra = st.create_issue(12, {"title": "billing crash", "state": "open", "author": "ci",
                                 "created_at": "2026-01-03T00:00:00Z",
                                 "updated_at": "2026-01-03T00:00:00Z"})
    extra.set_summary("billing", [])
    out = issues.query_issues(q="billing", sort="number", direction="asc")
    assert out["total"] == 1
    assert out["items"][0]["number"] == 12


def test_query_issues_filters_by_state(tmp_path, monkeypatch):
    """`state` narrows to one GitHub lifecycle state; "all"/None returns both."""
    st = _seed(tmp_path, monkeypatch)  # #10, #11 both open
    st.edit_issue(11).record_live_state("closed")
    assert {r["number"] for r in issues.query_issues(state="open")["items"]} == {10}
    assert {r["number"] for r in issues.query_issues(state="closed")["items"]} == {11}
    assert {r["number"] for r in issues.query_issues(state="all")["items"]} == {10, 11}
    assert {r["number"] for r in issues.query_issues()["items"]} == {10, 11}


def test_query_issues_author_pain_repro_and_labels_filters(tmp_path, monkeypatch):
    """The Issues-table per-column filters added for #494: author is a
    case-insensitive starts-with match; pain is a numeric compare (#10 and #11
    share their cluster's 0.81 pain score, so only the threshold differs the
    result); repro_grade excludes #11 (unset, unlike #10's "A"); labels is a
    substring match against any of the issue's labels."""
    _seed(tmp_path, monkeypatch)  # #10: author al, labels [bug], pain 0.81, repro A
    assert {r["number"] for r in issues.query_issues(author="AL")["items"]} == {10}
    assert {r["number"] for r in issues.query_issues(author="b")["items"]} == {11}
    assert {r["number"] for r in issues.query_issues(pain={"op": ">", "value": 0.9})["items"]} == set()
    assert {r["number"] for r in issues.query_issues(pain={"op": "<", "value": 0.9})["items"]} == {10, 11}
    assert {r["number"] for r in issues.query_issues(repro_grade="A")["items"]} == {10}
    assert {r["number"] for r in issues.query_issues(repro_grade=["A", "B"])["items"]} == {10}
    assert {r["number"] for r in issues.query_issues(labels="bug")["items"]} == {10}
    assert {r["number"] for r in issues.query_issues(labels="nope")["items"]} == set()


def test_query_issues_subsystem_dups_and_linked_prs_filters(tmp_path, monkeypatch):
    """subsystem is a substring match; dups (duplicate count) and linked_prs
    (linked-PR count) are numeric compares — see filters.num_cmp."""
    st = _seed(tmp_path, monkeypatch)  # #10, #11 share the "startup" cluster
    st.create_issue(12, {"title": "unrelated", "state": "open", "author": "ci",
                         "created_at": "2026-01-03T00:00:00Z", "updated_at": "2026-01-03T00:00:00Z"})
    assert {r["number"] for r in issues.query_issues(subsystem="start")["items"]} == {10, 11}
    assert {r["number"] for r in issues.query_issues(subsystem="nope")["items"]} == set()
    assert {r["number"] for r in issues.query_issues(dups={"op": ">", "value": 0})["items"]} == {10, 11}
    assert {r["number"] for r in issues.query_issues(dups={"op": "==", "value": 0})["items"]} == {12}
    # #10 links PR 900 (subsystem match); #11 and #12 link nothing.
    assert {r["number"] for r in issues.query_issues(linked_prs={"op": ">", "value": 0})["items"]} == {10}


def test_query_issues_sorts_prs_by_fix_evidence(tmp_path, monkeypatch):
    """sort=prs ranks by fix evidence — merged explicit fixers first, then explicit
    references, then total linked PRs — so a pile of subsystem tag-matches never
    outranks a real fixer."""
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_store_pr_states", lambda: {900: "merged", 910: "open"})
    st.edit_issue(10).set_links([{"pr": 900, "title": "fix boot", "how": "explicit"}])
    st.edit_issue(11).set_links(
        [{"pr": n, "title": f"t{n}", "how": "subsystem"} for n in (901, 902, 903)])
    extra = st.create_issue(12, {"title": "billing crash", "state": "open", "author": "ci",
                                 "created_at": "2026-01-03T00:00:00Z",
                                 "updated_at": "2026-01-03T00:00:00Z"})
    extra.set_links([{"pr": 910, "title": "billing fix", "how": "explicit"}])
    out = issues.query_issues(sort="prs", direction="desc")
    assert [r["number"] for r in out["items"]] == [10, 12, 11]
    rows = {r["number"]: r for r in out["items"]}
    assert rows[10]["referenced_merged_count"] == 1
    assert rows[12]["referenced_merged_count"] == 0 and rows[12]["referenced_pr_count"] == 1


def test_linked_prs_merged_explicit_leads(tmp_path, monkeypatch):
    """Within an issue's linked-PR list, explicit matches lead and a merged one
    sorts ahead of an open one, so a trim never hides the strongest fix evidence."""
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_store_pr_states",
                        lambda: {900: "open", 901: "open", 902: "merged"})
    st.edit_issue(10).set_links([
        {"pr": 900, "title": "a", "how": "explicit"},
        {"pr": 901, "title": "b", "how": "subsystem"},
        {"pr": 902, "title": "c", "how": "explicit"},
    ])
    row = issues.get_issue(10)
    assert [p["pr"] for p in row["linked_prs"]] == [902, 900, 901]


def test_get_issue(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    assert issues.get_issue(11)["canonical"] == 10
    assert issues.get_issue(99999) is None


def test_get_issue_includes_default_close_comments(tmp_path, monkeypatch):
    """get_issue prefills the exact templates the executor would post: a
    close-dup issue carries dup_comment (sourced from dup_issue_comment); a
    close-fixed issue carries fixed_comment (from fixed_issue_comment) — so the
    flyout never duplicates the wording."""
    st = _seed(tmp_path, monkeypatch)
    fixer = st.create_issue(410, {"title": "crashes on save", "state": "open", "author": "al",
                                  "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"})
    fixer.record_fixed(265, rationale="fixed by #265")

    dup_row = issues.get_issue(11)
    assert dup_row["dup_comment"] == issues.dup_issue_comment(10)
    assert dup_row["fixed_comment"] is None

    fixed_row = issues.get_issue(410)
    assert fixed_row["fixed_comment"] == issues.fixed_issue_comment(265)
    assert fixed_row["dup_comment"] is None


def test_duplicate_groups(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    groups = issues.duplicate_groups()
    assert len(groups) == 1
    g = groups[0]
    assert g["canonical"] == 10 and g["pain"] == 0.81
    assert [d["number"] for d in g["dups"]] == [11]
    assert g["linked_prs"][0]["pr"] == 900   # PRs cross-linked across cluster members
    assert g["dup_comment"] == issues.dup_issue_comment(10)   # prefill note = the executor's template


def test_duplicate_groups_unions_member_prs(tmp_path, monkeypatch):
    """A fixing PR that matched a duplicate (not the canonical) still surfaces on the
    group, deduped by PR number, explicit outranking subsystem for the same PR and
    explicit matches leading the list, with each PR's real state resolved."""
    st = _seed(tmp_path, monkeypatch)
    # #10 (canonical) already carries PR 900 (subsystem, open, in-store); the dup #11
    # carries the dup-only PR 950 plus an explicit match on the same 900.
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {950: "merged"})
    st.edit_issue(11).set_links([
        {"pr": 950, "title": "dup-only fix", "how": "subsystem"},
        {"pr": 900, "title": "fix boot", "how": "explicit"},
    ])
    g = issues.duplicate_groups()[0]
    linked = {p["pr"]: p for p in g["linked_prs"]}
    assert set(linked) == {900, 950}                          # the dup's PR surfaces too
    assert linked[900]["how"] == "explicit"                   # explicit outranks the subsystem match
    assert [p["pr"] for p in g["linked_prs"]] == [900, 950]   # explicit 900 leads the merged tag-match 950
    assert linked[900]["in_store"] is True and linked[900]["state"] == "open"   # in store → opens in-app
    assert linked[950]["in_store"] is False and linked[950]["state"] == "merged"  # off-store, resolved live


def test_duplicate_groups_in_store_open_pr_resolved_live(tmp_path, monkeypatch):
    """An in-store fixer whose snapshot is still 'open' is re-resolved live, so a PR
    that merged since the last ingest is labeled merged instead of being trusted as
    open off the stale store row."""
    _seed(tmp_path, monkeypatch)  # PR 900 is in the store as 'open'
    seen = {}
    def live(nums):
        seen["nums"] = list(nums)
        return {900: "merged"} if 900 in nums else {}
    monkeypatch.setattr(issues, "_live_pr_states", live)
    g = issues.duplicate_groups()[0]
    linked = {p["pr"]: p for p in g["linked_prs"]}
    assert 900 in seen["nums"]                      # stale-open in-store PR is sent to the live check
    assert linked[900]["in_store"] is True
    assert linked[900]["state"] == "merged"         # live merge overrides the stale-open snapshot


def test_duplicate_groups_excludes_unconfirmed(tmp_path, monkeypatch):
    """A cluster whose curation is not confirmed is not in the close worklist."""
    st = _seed(tmp_path, monkeypatch)
    cl = st.edit_issue_cluster(5)
    cl.rec.pop("curation", None)            # unconfirm
    st.save_issue_cluster(cl)
    assert issues.duplicate_groups() == []


def test_dup_comment_references_canonical():
    assert "#10" in issues.dup_issue_comment(10)
    assert "duplicate" in issues.dup_issue_comment(None).lower()


def test_close_issue_dry_run(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    res = executor.close_issue(11, models.IssueCloseDupBody(canonical=10), token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert "close issue #11" in res["detail"] and "#10" in res["detail"]


def test_close_issue_dry_run_blocked_when_dup_closed_upstream(tmp_path, monkeypatch):
    """#411: a duplicate already closed upstream fails the gate, so even a dry-run
    reports blocked — the preview matches what a live run does."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_state", lambda n: "closed" if n == 11 else "open")
    res = executor.close_issue(11, models.IssueCloseDupBody(canonical=10), token=None, dry_run=True)
    assert res["status"] == "blocked"
    assert "already closed" in res["detail"]


def test_close_issue_dry_run_blocked_when_canonical_closed_upstream(tmp_path, monkeypatch):
    """#411: the gate live-checks the canonical too, even when the store snapshot
    still has it open."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_state", lambda n: "open" if n == 11 else "closed")
    res = executor.close_issue(11, models.IssueCloseDupBody(canonical=10), token=None, dry_run=True)
    assert res["status"] == "blocked"
    assert "canonical" in res["detail"].lower()


def test_close_issue_dry_run_allows_canonical_fixed_upstream(tmp_path, monkeypatch):
    """A completed canonical can still be the target of a duplicate close."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_state", lambda n: "open" if n == 11 else "closed")
    monkeypatch.setattr(issues, "_live_state_reason", lambda n: "completed")
    res = executor.close_issue(
        11, models.IssueCloseDupBody(canonical=10), token=None, dry_run=True)
    assert res["status"] == "dry-run"


def test_close_issue_blocked_when_not_eligible(tmp_path, monkeypatch):
    """An issue that is not a confirmed duplicate is refused by the gate, even dry-run."""
    monkeypatch.setattr(issues, "STORE_ROOT", tmp_path)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    st = issue_store.IssueStore(tmp_path)
    st.create_issue(10, {"title": "c", "state": "open", "updated_at": "T"})
    dup = st.create_issue(11, {"title": "d", "state": "open", "updated_at": "T"})
    dup.route_to("close-dup", "dup", canonical=10)          # no confirmed cluster
    res = executor.close_issue(11, models.IssueCloseDupBody(canonical=10), token=None, dry_run=True)
    assert res["status"] == "blocked"
    assert "gate" in res["detail"].lower()


def test_close_issue_live_closes_as_duplicate_of_canonical(tmp_path, monkeypatch):
    """The cluster-driven dup close carries --duplicate-of, so GitHub records the
    marked_as_duplicate link rather than a bare 'not planned' close."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)
    calls = []

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: (calls.append(argv), _Ok())[1])
    res = executor.close_issue(11, models.IssueCloseDupBody(canonical=10), token="tok", dry_run=False)
    assert res["status"] == "executed"
    assert ["gh", "issue", "close", "11", "--repo", executor.REPO,
            "--reason", "duplicate", "--duplicate-of", "10"] in calls


def test_close_issue_reflects_closed_state_to_store(tmp_path, monkeypatch):
    """After a live dup-close, the issue's store row is stamped closed immediately —
    so the dup worklist drops it without waiting for the next INGEST closure sweep
    (the issue-side analog of the PR executor's _reflect_state, #192)."""
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_state", lambda n: "open")
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Ok())
    assert st.load_issue(11).state == "open"
    res = executor.close_issue(11, models.IssueCloseDupBody(canonical=10), token="tok", dry_run=False)
    assert res["status"] == "executed"
    # the store row now reads closed, read back through a fresh handle
    assert issue_store.IssueStore(tmp_path).load_issue(11).state == "closed"
    # and the worklist no longer offers #11 — its cluster has no closeable dup left
    assert issues.duplicate_groups() == []


def test_close_issue_with_comment_reflects_closed_state(tmp_path, monkeypatch):
    """An operator-directed issue close likewise stamps the store row closed on
    success, so every issue-close executor path shares the write-back."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_has_bot_comment", lambda n, contains=None: False)

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Ok())
    res = executor.close_issue_with_comment(
        10, models.IssueCloseBody(disposition="not-planned", comment="stale, closing"),
        token="tok", dry_run=False)
    assert res["status"] == "executed"
    assert issue_store.IssueStore(tmp_path).load_issue(10).state == "closed"


def test_reopen_issue_dry_run(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    res = executor.reopen_issue(11, token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert "reopen" in res["detail"] and res["forced"] is False


def test_reopen_issue_live_reopens_deletes_comment_and_reflects_open(tmp_path, monkeypatch):
    """The undo of an issue close: reopen upstream, delete the bot's closing
    comment(s), and stamp the store row back to open so the Issues view restores it
    immediately (the inverse of reflect_issue_state on close)."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(11).record_live_state("closed")   # as our close would have left it
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(executor, "_bot_comment_ids", lambda n: [555])
    calls = []

    class _Ok:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: (calls.append(argv), _Ok())[1])
    res = executor.reopen_issue(11, token="tok", dry_run=False)
    assert res["status"] == "reopened"
    assert ["gh", "issue", "reopen", "11", "--repo", executor.REPO] in calls
    assert any("comments/555" in " ".join(c) for c in calls)          # bot comment removed
    assert issue_store.IssueStore(tmp_path).load_issue(11).state == "open"


def test_reopen_issue_records_on_reopen_failure(tmp_path, monkeypatch):
    """A failed reopen still lands in the activity log — the trust model requires
    every upstream write attempt recorded — and does not touch the store."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(11).record_live_state("closed")
    recorded = []
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: recorded.append((a, k)))

    class _Fail:
        returncode = 1
        stderr = "rate limited"

    monkeypatch.setattr(executor, "bot_run", lambda argv, token, **kw: _Fail())
    res = executor.reopen_issue(11, token="tok", dry_run=False)
    assert res["status"] == "error" and "reopen failed" in res["detail"]
    assert recorded, "a failed upstream write must still be logged"
    assert issue_store.IssueStore(tmp_path).load_issue(11).state == "closed"  # unchanged


def test_close_fixed_eligibility_branches():
    """Merged fixer + open issue passes; a non-merged fixer or a non-open issue is
    refused."""
    from issue_triage import issue_gates

    class _Iss:
        def __init__(self, state): self.state = state

    assert issue_gates.close_fixed_eligibility(_Iss("open"), "merged")[0] is True
    ok, r = issue_gates.close_fixed_eligibility(_Iss("open"), "closed")
    assert ok is False and "not merged" in r
    ok, r = issue_gates.close_fixed_eligibility(_Iss("open"), None)
    assert ok is False and "not merged" in r
    ok, r = issue_gates.close_fixed_eligibility(_Iss("closed"), "merged")
    assert ok is False and "no action needed" in r
    # a live issue state overrides the store snapshot both ways
    ok, r = issue_gates.close_fixed_eligibility(_Iss("open"), "merged", "closed")
    assert ok is False and "no action needed" in r
    assert issue_gates.close_fixed_eligibility(_Iss("closed"), "merged", "open")[0] is True
    # a failed live fetch (None) falls back to the store state
    assert issue_gates.close_fixed_eligibility(_Iss("open"), "merged", None)[0] is True


def _seed_explicit_fixer(tmp_path, monkeypatch):
    """The _seed store with #10's PR 900 link upgraded to an explicit Fixes match —
    the only link kind the close-as-fixed gate accepts as fixed_by."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(10).set_links([{"pr": 900, "title": "fix boot", "how": "explicit"}])
    return st


def test_close_issue_fixed_dry_run(tmp_path, monkeypatch):
    _seed_explicit_fixer(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {900: "merged"})
    res = executor.close_issue_fixed(10, models.IssueCloseFixedBody(fixed_by=900), token=None, dry_run=True)
    assert res["status"] == "dry-run"
    assert "close issue #10" in res["detail"] and "#900" in res["detail"]


def test_close_issue_fixed_blocked_when_pr_not_a_candidate(tmp_path, monkeypatch):
    """The gate refuses a fixed_by PR that isn't an explicit fix candidate of the
    issue (or a cluster sibling), so the endpoint can't close an arbitrary issue by
    any merged PR."""
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {555: "merged"})
    res = executor.close_issue_fixed(10, models.IssueCloseFixedBody(fixed_by=555), token=None, dry_run=True)
    assert res["status"] == "blocked" and "not a fix candidate" in res["detail"]


def test_close_issue_fixed_blocked_when_pr_only_subsystem_matched(tmp_path, monkeypatch):
    """A merged PR whose only link to the issue is a shared subsystem tag is refused
    as fixed_by — the gate accepts explicit Fixes/Closes/Resolves candidates only."""
    _seed(tmp_path, monkeypatch)  # PR 900 is linked to #10 with how="subsystem"
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {900: "merged"})
    res = executor.close_issue_fixed(10, models.IssueCloseFixedBody(fixed_by=900), token=None, dry_run=True)
    assert res["status"] == "blocked" and "not a fix candidate" in res["detail"]


def test_close_issue_fixed_blocked_when_pr_not_merged(tmp_path, monkeypatch):
    """The gate re-verifies the fixing PR is merged live — a not-merged PR is refused
    even dry-run, so a stale snapshot can never drive a close."""
    _seed_explicit_fixer(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {900: "open"})
    res = executor.close_issue_fixed(10, models.IssueCloseFixedBody(fixed_by=900), token=None, dry_run=True)
    assert res["status"] == "blocked" and "not merged" in res["detail"]


def test_close_issue_fixed_blocked_when_issue_already_closed(tmp_path, monkeypatch):
    """A dry-run reflects the gate's live issue-state check: an issue already closed
    upstream blocks (matching a live run) instead of previewing a comment+close."""
    _seed_explicit_fixer(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.activity, "record", lambda *a, **k: None)
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {900: "merged"})
    monkeypatch.setattr(issues, "_live_state", lambda n: "closed")
    res = executor.close_issue_fixed(10, models.IssueCloseFixedBody(fixed_by=900), token=None, dry_run=True)
    assert res["status"] == "blocked" and "no action needed" in res["detail"]


def test_has_bot_comment_scopes_by_substring(monkeypatch):
    """With `contains`, the idempotency check filters bot comments to those mentioning
    the given reference, so a close-fixed comment (#PR) and a close-dup comment
    (#canonical) on the same thread do not suppress each other. Matching happens in
    Python, so a body containing quotes or backslashes is searched correctly."""
    class _R:
        stdout = '["fixed by #900", "closing as a dup of \\"#42\\" \\\\ x"]'

    monkeypatch.setattr(executor, "run", lambda argv, **kw: _R())
    assert executor._has_bot_comment(11, "#900")
    assert executor._has_bot_comment(11, 'dup of "#42" \\ x')   # quotes + backslash
    assert not executor._has_bot_comment(11, "#123")
    assert executor._has_bot_comment(11)                         # any bot comment


def test_has_bot_comment_false_on_unreadable_response(monkeypatch):
    class _R:
        stdout = ""

    monkeypatch.setattr(executor, "run", lambda argv, **kw: _R())
    assert not executor._has_bot_comment(11, "#900")


def test_bot_write_allows_issue_close_not_edit():
    safety_guard.assert_bot_write(["gh", "issue", "close", "11", "--repo", "x/y", "--reason", "not planned"])
    safety_guard.assert_bot_write(["gh", "issue", "close", "10", "--repo", "x/y", "--reason", "completed"])
    safety_guard.assert_bot_write(["gh", "issue", "comment", "11", "--repo", "x/y", "--body", "dup"])
    import pytest
    with pytest.raises(safety_guard.WriteAttemptBlocked):
        safety_guard.assert_bot_write(["gh", "issue", "edit", "11", "--repo", "x/y"])


def test_issue_ref_ranks_between_explicit_and_subsystem(tmp_path, monkeypatch):
    """An issue-ref candidate renders after explicit matches and before subsystem
    tag-matches, and counts as reference-backed for the fix-evidence sort."""
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_store_pr_states",
                        lambda: {900: "open", 901: "merged", 902: "open"})
    st.edit_issue(10).set_links([
        {"pr": 900, "title": "tag", "how": "subsystem"},
        {"pr": 901, "title": "named", "how": "issue-ref"},
        {"pr": 902, "title": "fixes", "how": "explicit"},
    ])
    row = issues.get_issue(10)
    assert [p["pr"] for p in row["linked_prs"]] == [902, 901, 900]
    assert row["referenced_pr_count"] == 2
    assert row["referenced_merged_count"] == 1  # the merged issue-ref counts


def test_issue_ref_merged_outranks_open_explicit_in_sort(tmp_path, monkeypatch):
    """sort=prs puts an issue whose text names a now-merged PR above one whose
    only evidence is an open explicit fixer — merged evidence leads."""
    st = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(issues, "_store_pr_states", lambda: {900: "merged", 910: "open"})
    st.edit_issue(10).set_links([{"pr": 900, "title": "named fix", "how": "issue-ref"}])
    st.edit_issue(11).set_links([{"pr": 910, "title": "open fix", "how": "explicit"}])
    out = issues.query_issues(sort="prs", direction="desc")
    assert [r["number"] for r in out["items"]] == [10, 11]


def test_cluster_fixer_dedup_prefers_strongest_evidence(tmp_path, monkeypatch):
    """When cluster members link the same PR as issue-ref and explicit, the
    explicit link wins the dedup (it alone can drive close-as-fixed)."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(10).set_links([{"pr": 900, "title": "t", "how": "issue-ref"}])
    st.edit_issue(11).set_links([{"pr": 900, "title": "t", "how": "explicit"}])
    g = issues.duplicate_groups()[0]
    linked = {p["pr"]: p for p in g["linked_prs"]}
    assert linked[900]["how"] == "explicit"


def test_row_carries_disposition_and_detail_has_analysis(tmp_path, monkeypatch):
    """Table rows expose the triage disposition; the detail row adds body,
    the full analysis section, and the cluster's curated label."""
    _seed(tmp_path, monkeypatch)
    out = issues.query_issues(sort="number", direction="asc")
    rows = {r["number"]: r for r in out["items"]}
    assert rows[11]["disposition"] == "close-dup"
    assert rows[10]["disposition"] is None
    d = issues.get_issue(11)
    assert d["analysis"]["disposition"] == "close-dup"
    assert d["analysis"]["rationale"] == "Duplicate of #10."
    assert d["cluster_label"] == "boot crashes"
    assert "body" in d


def test_query_issues_disposition_filter_and_sort(tmp_path, monkeypatch):
    """disposition= filters to one verdict ("none" = unanalyzed); the
    disposition sort ranks most-actionable first with unanalyzed last."""
    st = _seed(tmp_path, monkeypatch)
    extra = st.create_issue(12, {"title": "billing crash", "state": "open", "author": "ci",
                                 "created_at": "2026-01-03T00:00:00Z",
                                 "updated_at": "2026-01-03T00:00:00Z"})
    extra.route_to("link-pr", "PR #900 addresses it.")
    out = issues.query_issues(disposition="close-dup")
    assert [r["number"] for r in out["items"]] == [11]
    out = issues.query_issues(disposition="none")
    assert [r["number"] for r in out["items"]] == [10]
    out = issues.query_issues(sort="disposition", direction="asc")
    assert [r["number"] for r in out["items"]] == [12, 11, 10]  # link-pr, close-dup, unanalyzed last


def test_fix_found_candidate_is_referenced_and_sorts_after_explicit(tmp_path, monkeypatch):
    """A detector-found fixer counts as reference-backed (it renders as a link,
    never in the same-subsystem count) and ranks between explicit and issue-ref."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(10).set_links([
        {"pr": 901, "title": "tag match", "how": "subsystem"},
        {"pr": 902, "title": "found fix", "how": "fix-found"},
        {"pr": 903, "title": "named in issue", "how": "issue-ref"},
        {"pr": 904, "title": "claims fix", "how": "explicit"},
    ])
    row = {r["number"]: r for r in issues.list_issues()}[10]
    assert row["referenced_pr_count"] == 3
    assert [p["pr"] for p in row["linked_prs"]] == [904, 902, 903, 901]


def test_dup_group_offers_fix_found_fixer(tmp_path, monkeypatch):
    """A cluster whose only merged fixer is detector-found still gets the
    close-as-fixed prefill (the gate accepts fix-found fixers)."""
    st = _seed(tmp_path, monkeypatch)
    st.edit_issue(10).set_links([{"pr": 950, "title": "found fix", "how": "fix-found"}])
    monkeypatch.setattr(issues, "_live_pr_states", lambda nums: {950: "merged"})
    g = next(g for g in issues.duplicate_groups() if g["cluster"] == 5)
    assert g["fixed_comment"] is not None and "#950" in g["fixed_comment"]
