"""INGEST for repository security advisories: list every advisory as the bot,
normalize, and upsert the changed ones with recomputed candidate PR links.
`ingest_records` is pure and unit-tested; `main` adds the token mint, the live
fetch, and the PR-corpus join. Mirrors alert_triage/alert_ingest.py.
"""
from __future__ import annotations

import argparse

from alert_triage import advisory_model
from alert_triage import config
from alert_triage import link_prs
from alert_triage.advisory_store import OPEN_STATES, AdvisoryStore, advisory_id
from pipeline.storekit import now as _now

SOURCE = "advisory"
_SEVERITIES = {"critical", "high", "medium", "low"}


def _first_login(rows: list[dict] | None) -> str | None:
    for row in rows or []:
        login = row.get("login")
        if login:
            return login
    return None


def normalize(raw: dict) -> dict:
    vuln = (raw.get("vulnerabilities") or [{}])[0] or {}
    author = (raw.get("author") or {}).get("login")
    severity = (raw.get("severity") or "").lower()
    return {
        "ghsa_id": raw["ghsa_id"],
        "state": raw.get("state"),
        "severity": severity if severity in _SEVERITIES else "unknown",
        "summary": raw.get("summary") or "",
        "description": raw.get("description") or "",
        "cve_id": raw.get("cve_id"),
        "cwe_ids": list(raw.get("cwe_ids") or []),
        "reporter": (_first_login(raw.get("credits"))
                     or _first_login(raw.get("collaborating_users")) or author),
        "author": author,
        "created_at": raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "published_at": raw.get("published_at"),
        "closed_at": raw.get("closed_at"),
        "html_url": raw.get("html_url"),
        "vulnerable_range": vuln.get("vulnerable_version_range") or None,
        "patched_versions": vuln.get("patched_versions") or None,
    }


def fetch(token: str) -> list[dict]:
    """Every advisory in every state, normalized. Raises SourceUnavailable on
    a 403/404 (the App lacks the advisory read permission)."""
    rows = config.gh_alert_read_all(f"repos/{config.REPO}/security-advisories", token,
                                    {"per_page": "100"}, source=SOURCE)
    return [normalize(r) for r in rows]


def _meta_unchanged(existing: advisory_model.Advisory, meta: dict) -> bool:
    stored = existing.section("meta")
    return stored is not None and all(stored.get(k) == v for k, v in meta.items())


def ingest_records(store: AdvisoryStore, metas: list[dict], prs: list[dict],
                   diffs: dict[str, str]) -> int:
    """Upsert each advisory whose meta changed, recomputing candidate links for
    the open states. Existing fact sections ride along. Returns the count."""
    if not metas:
        return 0
    existing = store.all_advisories()
    written = 0
    with store.batch():
        for meta in metas:
            i = advisory_id(meta["ghsa_id"])
            prev = existing.get(i)
            if prev is not None and _meta_unchanged(prev, meta):
                continue
            links = (link_prs.candidates_for(meta, prs, diffs)
                     if meta.get("state") in OPEN_STATES else None)
            adv = prev or advisory_model.Advisory(store, {"id": i})
            adv.apply_facts(meta, links=links)
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", default=None, help="store root override (tests/smoke)")
    args = ap.parse_args(argv)
    store = AdvisoryStore(args.store) if args.store else AdvisoryStore()
    started = _now()
    token = config.mint_token()
    if token is None:
        raise SystemExit("advisory ingest needs a bot token; minting failed "
                         "(check TRIAGE_BOT_APP_ID / TRIAGE_BOT_KEY_FILE)")
    prs, diffs = link_prs.pr_corpus()
    print(f"PR corpus: {len(prs)} | fetching advisories…", flush=True)
    try:
        metas = fetch(token)
    except config.SourceUnavailable as e:
        store.append_run({"phase": "advisory-ingest", "started": started,
                          "finished": _now(),
                          "stats": {"fetched": {}, "unavailable": [SOURCE], "upserted": 0}})
        print(f"  advisories: unavailable ({e.detail})", flush=True)
        return 0
    n = ingest_records(store, metas, prs, diffs)
    store.append_run({"phase": "advisory-ingest", "started": started, "finished": _now(),
                      "stats": {"fetched": {SOURCE: len(metas)}, "unavailable": [],
                                "upserted": n}})
    print(f"ingested {len(metas)} advisories ({n} changed, written)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
