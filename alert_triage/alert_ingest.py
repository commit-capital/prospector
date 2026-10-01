"""INGEST: fetch every alert from the three repository-security sources as the
bot, and upsert the changed ones with recomputed candidate links. The
store-writing half (ingest_records) is pure and unit-tested; main() adds the
token mint, the live fetch, and the PR-corpus join — read only when an open
alert is new or changed, since only those are linked. Mirrors
issue_triage/issue_ingest.py.
"""
from __future__ import annotations

import argparse

from alert_triage import alert_model
from alert_triage import config
from alert_triage import fetch_alerts
from alert_triage import link_prs
from alert_triage.alert_store import ALERT_SOURCES, AlertStore, alert_id
from pipeline import progress
from pipeline.storekit import now as _now


def _meta_unchanged(existing: alert_model.Alert, meta: dict) -> bool:
    """True when re-ingesting reproduces an identical meta section, so the
    write is a no-op. Ignores the checked_at stamp."""
    stored = existing.rec.get("meta")
    return stored is not None and all(stored.get(k) == v for k, v in meta.items())


def _changed(existing: dict[int, alert_model.Alert], metas: list[dict]) -> list[dict]:
    out: list[dict] = []
    for meta in metas:
        prev = existing.get(alert_id(meta["source"], meta["number"]))
        if prev is None or not _meta_unchanged(prev, meta):
            out.append(meta)
    return out


def ingest_records(store: AlertStore, metas: list[dict], prs: list[dict],
                   diffs: dict[str, str]) -> int:
    """Upsert each normalized alert whose meta changed, recomputing candidate
    links for the open ones (links only mean anything for an alert that can
    still be acted on). Existing fact sections (fix_scan) ride along untouched.
    Returns how many alerts were written."""
    if not metas:
        return 0
    known = store.all_alerts()
    changed = _changed(known, metas)
    if not changed:
        return 0
    saving = progress.Progress("saving", len(changed), "new or changed alerts",
                               one="new or changed alert")
    with store.batch():
        for meta in changed:
            i = alert_id(meta["source"], meta["number"])
            links = (link_prs.candidates_for(meta, prs, diffs)
                     if meta.get("state") == "open" else [])
            alert = known.get(i) or alert_model.Alert(store, {"id": i})
            alert.apply_facts(meta, links=links)
            saving.advance()
    saving.finish()
    return len(changed)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=None, help="store root override (tests/smoke)")
    args = ap.parse_args(argv)
    store = AlertStore(args.store) if args.store else AlertStore()
    started = _now()
    token = config.mint_token()
    if token is None:
        raise SystemExit("alert ingest needs a bot token; minting failed "
                         "(check TRIAGE_BOT_APP_ID / TRIAGE_BOT_KEY_FILE)")
    print("fetching alerts as the bot…", flush=True)
    metas: list[dict] = []
    fetched: dict[str, int] = {}
    unavailable: list[str] = []
    for source in sorted(ALERT_SOURCES):
        try:
            rows = fetch_alerts.fetch_source(source, token)
        except config.SourceUnavailable as e:
            unavailable.append(source)
            print(f"  {source}: unavailable ({e.detail})", flush=True)
            continue
        fetched[source] = len(rows)
        metas += rows
        print(f"  {source}: {len(rows)} alerts", flush=True)
    existing = store.all_alerts()
    changed = _changed(existing, metas)
    linking = [m for m in changed if m.get("state") == "open"]
    print(f"{len(metas)} alerts fetched: {len(changed)} new or changed, "
          f"{len(linking)} of them open", flush=True)
    paths = {m["path"] for m in linking
             if m.get("source") == "code-scanning" and m.get("path")}
    prs, diffs = link_prs.corpus_for(len(linking), "open alerts", paths)
    n = ingest_records(store, metas, prs, diffs)
    store.append_run({"phase": "alert-ingest", "started": started, "finished": _now(),
                      "stats": {"fetched": fetched, "unavailable": unavailable,
                                "upserted": n}})
    dest = store.engine.url.host or store.root
    print(f"ingested {len(metas)} alerts ({n} changed, written) -> {dest}", flush=True)


if __name__ == "__main__":
    main()
