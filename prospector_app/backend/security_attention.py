"""The Home Security card's data: open security work waiting on a person —
critical/high advisories the find-fixed pass has not judged fixed or
duplicate, plus every open secret-scanning alert."""
from __future__ import annotations

from alert_triage.advisory_store import OPEN_STATES
from prospector_app.backend import advisories, alerts

SEVERE_SEVERITIES = {"critical", "high"}
FIXED_VERDICTS = {"fixed", "likely-fixed", "duplicate"}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# How many sample items the card lists under its count.
SAMPLE_LIMIT = 3


def _sort_key(item: dict) -> tuple[int, str, str]:
    return (_SEVERITY_RANK.get(item["severity"] or "", 4),
            item["created_at"] or "",
            item["key"])


def attention() -> dict:
    """Per-kind counts plus the most severe (then oldest) sample items."""
    adv_rows, _ = advisories.list_advisories()
    alert_rows, _ = alerts.list_alerts()
    items: list[dict] = []
    for r in adv_rows:
        if (r["state"] in OPEN_STATES
                and r["severity"] in SEVERE_SEVERITIES
                and r["verdict"] not in FIXED_VERDICTS):
            items.append({"kind": "advisory", "key": r["ghsa_id"],
                          "ghsa_id": r["ghsa_id"], "severity": r["severity"],
                          "title": r["summary"], "created_at": r["created_at"],
                          "html_url": r["html_url"]})
    for r in alert_rows:
        if r["source"] == "secret-scanning" and r["state"] == "open":
            items.append({"kind": "secret", "key": f"secret-{r['number']}",
                          "number": r["number"], "severity": r["severity"],
                          "title": r["title"], "created_at": r["created_at"],
                          "html_url": r["html_url"]})
    items.sort(key=_sort_key)
    advisory_count = sum(1 for i in items if i["kind"] == "advisory")
    return {"total": len(items),
            "advisories": advisory_count,
            "secrets": len(items) - advisory_count,
            "items": items[:SAMPLE_LIMIT]}
