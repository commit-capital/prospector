"""INGEST driver: fetch raws -> deterministic facts in the store (pure half)."""
from issue_triage import issue_freshness
from issue_triage import issue_ingest
from issue_triage import issue_store

RAW = {"number": 5, "title": "Login crashes", "body": "Steps:\n1. open\nexpected x actual y",
       "labels": ["bug"], "comments": 2, "reactions_total": 4, "thumbs_up": 2,
       "author": "alice", "created_at": "2026-06-01T00:00:00Z",
       "updated_at": "2026-06-02T00:00:00Z", "state_reason": None}


def test_ingest_writes_meta_summary_repro_links(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    iss = st.load_issue(5)
    assert iss.title == "Login crashes"
    assert iss.state == "open"
    assert iss.updated_at == "2026-06-02T00:00:00Z"
    assert iss.repro_grade in {"A", "B", "C", "D", "F"}
    assert iss.section("summary") is not None
    assert iss.section("repro") is not None
    assert iss.section("links") is not None
    assert issue_freshness.is_current(iss, "summary")
    assert issue_freshness.is_current(iss, "repro")


def test_ingest_links_explicit_fixing_pr(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    prs = [{"number": 100, "title": "fix login", "body": "Fixes #5"}]
    issue_ingest.ingest_records(st, [RAW], prs=prs)
    cands = st.load_issue(5).candidate_prs
    assert any(c["pr"] == 100 and c["how"] == "explicit" for c in cands)


def test_ingest_links_prose_body_reference(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    prs = [{"number": 100, "title": "fix login",
            "body": "Refs upstream issue #5"}]
    issue_ingest.ingest_records(st, [RAW], prs=prs)
    assert {"pr": 100, "how": "body-ref", "title": "fix login"} \
        in st.load_issue(5).candidate_prs


def test_load_prs_reads_open_and_merged_prs_from_sql_store(tmp_path):
    """_load_prs sources its corpus from the SQL PR store — open and merged PRs,
    each stamped with its state and a subsystem; closed-unmerged PRs are out."""
    from pipeline.store import Store
    pr_store = Store(tmp_path)
    pr_store.create_pr(100, {"title": "fix login", "body": "Fixes #5", "state": "open", "head_sha": "a"})
    pr_store.create_pr(200, {"title": "landed fix", "body": "Fixes #9", "state": "merged", "head_sha": "b"})
    pr_store.create_pr(300, {"title": "abandoned", "body": "Fixes #5", "state": "closed", "head_sha": "c"})
    prs = {p["number"]: p for p in issue_ingest._load_prs(pr_store)}
    assert set(prs) == {100, 200}
    assert prs[100]["state"] == "open" and prs[200]["state"] == "merged"
    assert prs[100]["title"] == "fix login" and prs[100]["body"] == "Fixes #5"
    assert "subsystem" in prs[100]


def test_ingest_links_merged_explicit_fixer(tmp_path):
    """A merged PR whose body fixes the issue stays in the issue's links."""
    st = issue_store.IssueStore(tmp_path)
    prs = [{"number": 100, "title": "landed fix", "body": "Fixes #5", "state": "merged"}]
    issue_ingest.ingest_records(st, [RAW], prs=prs)
    cands = st.load_issue(5).candidate_prs
    assert any(c["pr"] == 100 and c["how"] == "explicit" for c in cands)


def test_ingest_links_pr_named_in_issue_text(tmp_path):
    """A PR the issue's own title/body names becomes an issue-ref link."""
    st = issue_store.IssueStore(tmp_path)
    raw = {**RAW, "body": "Still broken despite fix in PR #100."}
    prs = [{"number": 100, "title": "supposed fix", "body": "", "state": "merged"}]
    issue_ingest.ingest_records(st, [raw], prs=prs)
    cands = st.load_issue(5).candidate_prs
    assert any(c["pr"] == 100 and c["how"] == "issue-ref" for c in cands)


def test_load_prs_feeds_explicit_link_end_to_end(tmp_path):
    """The store-backed corpus flows through ingest_records into an issue's links."""
    from pipeline.store import Store
    pr_store = Store(tmp_path / "prs")
    pr_store.create_pr(100, {"title": "fix login", "body": "Fixes #5", "state": "open", "head_sha": "a"})
    st = issue_store.IssueStore(tmp_path / "issues")
    issue_ingest.ingest_records(st, [RAW], issue_ingest._load_prs(pr_store))
    cands = st.load_issue(5).candidate_prs
    assert any(c["pr"] == 100 and c["how"] == "explicit" for c in cands)


def test_ingest_writes_fetched_state(tmp_path):
    """A raw carrying state (from normalize_issue) lands in meta as-is; absent
    state defaults to open."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(
        st, [{**RAW, "state": "closed", "state_reason": "completed"}], prs=[])
    assert st.load_issue(5).state == "closed"
    assert st.load_issue(5).state_reason == "completed"


def test_reconcile_closures_marks_vanished_issue_closed(tmp_path):
    """A store-open issue absent from the open fetch is refetched; its real
    (closed) state is written and counted as a transition."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    closed_raw = {**RAW, "state": "closed", "updated_at": "2026-06-10T00:00:00Z"}
    n = issue_ingest.reconcile_closures(st, open_now=set(), prs=[], fetch_one=lambda _: closed_raw)
    assert n == 1
    iss = st.load_issue(5)
    assert iss.state == "closed"
    assert iss.updated_at == "2026-06-10T00:00:00Z"


def test_reconcile_closures_skips_present_and_already_closed(tmp_path):
    """Issues still in the open fetch, and issues already stored closed, are not
    refetched."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW, {**RAW, "number": 6, "state": "closed"}], prs=[])
    calls: list[int] = []

    def fetch_one(n: int) -> dict | None:
        calls.append(n)
        return None

    assert issue_ingest.reconcile_closures(st, open_now={5}, prs=[], fetch_one=fetch_one) == 0
    assert calls == []


def test_reconcile_closures_leaves_unfetchable_issue_untouched(tmp_path):
    """A failed targeted fetch (deleted/transferred/transient) keeps the stored
    record as-is."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    assert issue_ingest.reconcile_closures(st, open_now=set(), prs=[], fetch_one=lambda _: None) == 0
    assert st.load_issue(5).state == "open"


def test_reconcile_closures_still_open_is_refreshed_not_counted(tmp_path):
    """An open issue past the pagination cap refetches as still-open: the whole
    record refreshes (meta + derived facts, so freshness holds against the moved
    updated_at) but no transition is counted."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    bumped = {**RAW, "updated_at": "2026-06-11T00:00:00Z"}
    assert issue_ingest.reconcile_closures(st, open_now=set(), prs=[], fetch_one=lambda _: bumped) == 0
    iss = st.load_issue(5)
    assert iss.state == "open"
    assert iss.updated_at == "2026-06-11T00:00:00Z"
    assert issue_freshness.is_current(iss, "summary")
    assert issue_freshness.is_current(iss, "repro")


def test_ingest_is_idempotent_and_refreshes_on_update(tmp_path):
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    bumped = {**RAW, "updated_at": "2026-06-09T00:00:00Z", "title": "Login crashes hard"}
    issue_ingest.ingest_records(st, [bumped], prs=[])
    iss = st.load_issue(5)
    assert iss.title == "Login crashes hard"
    assert iss.updated_at == "2026-06-09T00:00:00Z"
    # only one record exists (upsert, not duplicate)
    assert list(st.all_issues().keys()) == [5]


def test_ingest_writes_only_changed_issues(tmp_path, monkeypatch):
    """A re-ingest writes only issues whose facts changed. An unchanged existing
    issue is skipped (no store write); a new or changed one lands as a single
    write (meta + summary + repro + links)."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])          # 5 now exists
    saves: list[dict] = []
    original = st.save_issue

    def counting_save(rec) -> None:
        saves.append(rec)
        original(rec)

    monkeypatch.setattr(st, "save_issue", counting_save)
    # RAW is unchanged (skipped); #6 is new (written) → exactly one save.
    written = issue_ingest.ingest_records(st, [RAW, {**RAW, "number": 6}], prs=[])
    assert written == 1
    assert len(saves) == 1
    assert saves[0]["issue"] == 6


def test_ingest_reingest_of_unchanged_corpus_writes_nothing(tmp_path, monkeypatch):
    """Re-ingesting the identical corpus is a no-op — zero writes."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    saves: list[dict] = []
    monkeypatch.setattr(st, "save_issue", lambda rec: saves.append(rec))
    assert issue_ingest.ingest_records(st, [RAW], prs=[]) == 0
    assert saves == []


def test_ingest_relinks_only_when_the_issue_itself_changes(tmp_path):
    """Links recompute when the issue is (re)written, not on every ingest: a new
    matching PR alone does NOT relink an otherwise-unchanged issue (the snapshot
    omits candidates to dodge the store's statement timeout), but a change to the
    issue does pick the PR up."""
    st = issue_store.IssueStore(tmp_path)
    raw = {**RAW, "body": "fixed by #77"}
    issue_ingest.ingest_records(st, [raw], prs=[])          # no PRs yet → no candidate
    assert st.load_issue(5).candidate_prs == []
    pr = {"number": 77, "title": "fix", "body": "", "state": "open", "subsystem": "auth"}
    # Same issue, PR now exists — but the issue is unchanged, so it's skipped.
    assert issue_ingest.ingest_records(st, [raw], prs=[pr]) == 0
    assert st.load_issue(5).candidate_prs == []
    # Touch the issue (new updated_at) → it's rewritten and picks up the PR.
    bumped = {**raw, "updated_at": "2026-06-09T00:00:00Z"}
    assert issue_ingest.ingest_records(st, [bumped], prs=[pr]) == 1
    assert 77 in [c["pr"] for c in st.load_issue(5).candidate_prs]


def test_reingest_preserves_pipeline_sections(tmp_path):
    """Re-ingesting refreshes the fact sections but leaves the pipeline-owned
    sections (analysis) on the record."""
    st = issue_store.IssueStore(tmp_path)
    issue_ingest.ingest_records(st, [RAW], prs=[])
    st.edit_issue(5).route_to("close-dup", "dup of #4", canonical=4)
    issue_ingest.ingest_records(st, [{**RAW, "updated_at": "2026-06-09T00:00:00Z"}], prs=[])
    iss = st.load_issue(5)
    assert iss.disposition == "close-dup" and iss.canonical == 4
