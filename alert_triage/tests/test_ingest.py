"""ingest_records: upsert-on-change, link recompute for open alerts only, and
fact preservation across meta rewrites."""
from alert_triage import alert_ingest
from alert_triage.alert_store import AlertStore, alert_id


def _meta(source: str = "dependabot", number: int = 1, **over) -> dict:
    meta = {
        "source": source, "number": number, "state": "open", "raw_state": "open",
        "severity": "high", "created_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-02T00:00:00Z",
        "html_url": f"https://github.com/o/r/security/{source}/{number}",
        "package": "lodash", "manifest_path": "web/package.json",
    }
    meta.update(over)
    return meta


PRS = [{"number": 10, "title": "Bump lodash from 1 to 2", "body": "",
        "state": "merged", "head_sha": "aaa"}]


def test_new_alert_lands_with_meta_and_links(tmp_path):
    store = AlertStore(tmp_path)
    written = alert_ingest.ingest_records(store, [_meta()], PRS, {})
    assert written == 1
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.state == "open"
    assert [(c["number"], c["how"]) for c in a.candidates] == [(10, "manifest-bump")]


def test_unchanged_reingest_writes_nothing(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta()], PRS, {})
    assert alert_ingest.ingest_records(store, [_meta()], PRS, {}) == 0


def test_changed_updated_at_rewrites_meta_and_links(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta()], PRS, {})
    moved = _meta(updated_at="2026-08-09T00:00:00Z")
    assert alert_ingest.ingest_records(store, [moved], PRS, {}) == 1
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.updated_at == "2026-08-09T00:00:00Z"


def test_closed_alert_gets_no_links(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta(state="fixed", raw_state="fixed")], PRS, {})
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.state == "fixed" and a.candidates == []


def test_fix_scan_survives_meta_rewrite(tmp_path):
    store = AlertStore(tmp_path)
    alert_ingest.ingest_records(store, [_meta()], PRS, {})
    store.edit_alert(alert_id("dependabot", 1)).record_fix_scan(
        "not-fixed", action="needs-fix", by="agent")
    alert_ingest.ingest_records(store, [_meta(updated_at="2026-08-10T00:00:00Z")], PRS, {})
    a = store.load_alert(alert_id("dependabot", 1))
    assert a is not None and a.verdict == "not-fixed"


def _fake_fetch(monkeypatch, by_source: dict[str, list[dict]]) -> None:
    monkeypatch.setattr(alert_ingest.config, "mint_token", lambda: "token")
    monkeypatch.setattr(alert_ingest.fetch_alerts, "fetch_source",
                        lambda source, token: by_source.get(source, []))


def test_main_skips_the_pr_corpus_when_no_open_alert_changed(tmp_path, monkeypatch, capsys):
    alert_ingest.ingest_records(AlertStore(tmp_path), [_meta()], PRS, {})
    _fake_fetch(monkeypatch, {"dependabot": [_meta()]})

    def corpus(*_, **__):
        raise AssertionError("the PR corpus was read")
    monkeypatch.setattr(alert_ingest.link_prs, "pr_corpus", corpus)
    alert_ingest.main(["--store", str(tmp_path)])
    assert "1 alerts fetched: 0 new or changed, 0 of them open" in capsys.readouterr().out


def test_main_asks_only_for_the_diffs_of_alerted_files(tmp_path, monkeypatch):
    scanning = _meta("code-scanning", 2, path="src/x.py", package=None)
    _fake_fetch(monkeypatch, {"dependabot": [_meta()], "code-scanning": [scanning]})
    asked: list[set[str]] = []
    monkeypatch.setattr(alert_ingest.link_prs, "pr_corpus",
                        lambda store=None, paths=(): asked.append(set(paths)) or (PRS, {}))
    alert_ingest.main(["--store", str(tmp_path)])
    assert asked == [{"src/x.py"}]
    assert AlertStore(tmp_path).all_alerts()[alert_id("dependabot", 1)].rec["links"]


def test_pr_corpus_reads_the_pr_store_once_per_process(tmp_path, monkeypatch):
    from alert_triage import link_prs
    from pipeline.store import Store

    store = Store(tmp_path / "prs")
    reads: list[int] = []
    real = store.link_rows
    monkeypatch.setattr(store, "link_rows", lambda: reads.append(1) or real())
    monkeypatch.setattr(link_prs, "_ROWS", {})
    link_prs.pr_corpus(store)
    link_prs.pr_corpus(store, paths={"a.py"})
    assert reads == [1]
