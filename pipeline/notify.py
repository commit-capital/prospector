"""Telling the team in Slack what the threat scan found that needs a person now.

Two findings post, each once: an open PR the scan flagged malicious, and a
live-looking credential committed in an open PR a maintainer wrote — a
contributor working from a fork never holds the project's own secrets, so their
leaks of their own deployment's secrets stay in the app's Action items. Each
finding must be at most WINDOW_DAYS old, so turning this on never announces
history. Messages go to the incoming webhook TRIAGE_SLACK_WEBHOOK_URL names;
with none set nothing is read or posted.

`threat_scan.scan` calls `send_due` with the registries it just committed. A
machine claims each alert in the shared store before posting it
(`Store.claim_notification`), so two machines scanning one PR post one message,
and a failed post is retried by later passes, MAX_TRIES times in all. Text a PR
author wrote is escaped so it cannot mention the channel or forge a link, and a
credential alert names the file, never the value.

  uv run python -m pipeline.notify --test   # post a test message
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

from pipeline import actions, gates, settings, storekit

if TYPE_CHECKING:
    from pipeline.model import Pr
    from pipeline.store import Store

WINDOW_DAYS = 2
MAX_TRIES = 3
# How long a claim may sit `sending` before another pass takes it over.
STALE_SECONDS = 600.0
TIMEOUT_SECONDS = 10.0
# The longest PR title an alert quotes.
TITLE_LIMIT = 100

Post = Callable[[str, str], bool]


@dataclass(frozen=True)
class Alert:
    key: str
    text: str


def _esc(text: str) -> str:
    """Slack's mrkdwn control characters, escaped."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _code(text: str) -> str:
    return "`" + _esc(text).replace("`", "'") + "`"


def _fresh(day: object, today: date) -> bool:
    try:
        when = date.fromisoformat(str(day)[:10])
    except ValueError:
        return False
    return timedelta(0) <= today - when <= timedelta(days=WINDOW_DAYS)


def _pr_link(n: int, pr: Pr) -> str:
    url = pr.url or f"https://github.com/{settings.repo()}/pull/{n}"
    title = (pr.title or "").strip()
    if len(title) > TITLE_LIMIT:
        title = title[:TITLE_LIMIT - 1] + "…"
    return f"<{url}|#{n}>" + (f" “{_esc(title)}”" if title else "")


def _evidence_path(evidence: str) -> str | None:
    path, sep, _ = evidence.partition(":")
    path = path.strip()
    return path if sep and path and path != "?" else None


def due_alerts(incidents: list[dict], items: list[dict], prs: Mapping[int, Pr],
               today: date) -> list[Alert]:
    """The alerts the registries call for: each open PR flagged malicious, and
    each open, non-fixture rotate-secret item on an open PR a maintainer wrote,
    first seen within WINDOW_DAYS of `today` — an item from the day its current
    evidence was found, and keyed by that evidence. A PR missing from `prs` is
    skipped."""
    out: list[Alert] = []
    for inc in incidents:
        n = inc.get("pr")
        pr = prs.get(n) if isinstance(n, int) else None
        if (not isinstance(n, int) or pr is None or pr.state != "open"
                or not _fresh(inc.get("noticed"), today)):
            continue
        sigs = ", ".join(str(s) for s in inc.get("signatures") or []) or "flagged"
        out.append(Alert(f"malicious:{n}", (
            f"⛔ *Malicious PR flagged:* {_pr_link(n, pr)} by {_code(pr.author or '?')}"
            f" · {_esc(sigs)} · Never mergeable — close it on GitHub and check the"
            " author's other PRs.")))
    for item in items:
        n = item.get("pr")
        pr = prs.get(n) if isinstance(n, int) else None
        if (not isinstance(n, int) or pr is None or pr.state != "open"
                or not gates.maintainer_leak(item, pr) or not _fresh(actions.found(item), today)):
            continue
        evidence = str(item.get("evidence") or "")
        path = _evidence_path(evidence)
        where = f" in {_code(path)}" if path else ""
        digest = hashlib.sha256(evidence.encode()).hexdigest()[:12]
        out.append(Alert(f"secret:{n}:{digest}", (
            f"🔑 *Possible {_esc(settings.display_name() or 'project')} credential leaked:*"
            f" {_pr_link(n, pr)} by maintainer"
            f" {_code(pr.author or '?')}{where} · Rotate it at the provider if real —"
            " closing the PR does not revoke a pushed secret.")))
    return out


def post_webhook(url: str, text: str) -> bool:
    """POST `text` to the incoming webhook at `url`; True when Slack took it."""
    import httpx
    try:
        res = httpx.post(url, json={"text": text}, timeout=TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        print(f"[notify] Slack post failed: {type(e).__name__}", flush=True)
        return False
    if res.status_code // 100 != 2:
        print(f"[notify] Slack refused the post: HTTP {res.status_code}", flush=True)
        return False
    return True


def send_due(store: Store, registry: dict, items: list[dict], *, url: str | None = None,
             post: Post | None = None, today: date | None = None) -> list[str]:
    """Post every due alert this machine claims, through `post` (the webhook
    by default). Returns the keys posted."""
    url = url or settings.slack_webhook_url()
    if not url:
        return []
    post = post or post_webhook
    today = today or date.fromisoformat(storekit.utc_day())
    incidents = list(registry.get("incidents") or [])
    wanted = {inc.get("pr") for inc in incidents if _fresh(inc.get("noticed"), today)}
    wanted |= {it.get("pr") for it in items
               if it.get("kind") == "rotate-secret" and _fresh(actions.found(it), today)}
    prs = {n: rec for n in wanted
           if isinstance(n, int) and (rec := store.load_pr(n)) is not None}
    sent: list[str] = []
    for alert in due_alerts(incidents, items, prs, today):
        if not store.claim_notification(alert.key, host=settings.worker_id(),
                                        max_tries=MAX_TRIES, stale_after=STALE_SECONDS):
            continue
        ok = post(url, alert.text)
        store.finish_notification(alert.key, sent=ok)
        if ok:
            sent.append(alert.key)
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="post a test message to TRIAGE_SLACK_WEBHOOK_URL")
    args = ap.parse_args(argv)
    if not args.test:
        ap.print_help()
        return 2
    url = settings.slack_webhook_url()
    if not url:
        print("TRIAGE_SLACK_WEBHOOK_URL is not set", file=sys.stderr)
        return 1
    ok = post_webhook(url, test_text())
    print("posted" if ok else "the post failed")
    return 0 if ok else 1


def test_text() -> str:
    return (f"✅ Prospector ({_esc(settings.worker_id())}) can post threat alerts for "
            f"{_esc(settings.repo() or 'this deployment')} to this channel.")


if __name__ == "__main__":
    sys.exit(main())
