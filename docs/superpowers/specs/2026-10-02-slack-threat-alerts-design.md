# Slack threat alerts

## Problem

The threat scan's findings reach an operator only inside the app. A PR flagged
malicious on the worker at night waits until someone opens Prospector. The
team wants the two findings that need a person now pushed to a Slack channel,
and nothing else: worker outages and contributors' leaks of their own
deployment secrets happen too often to be worth a message.

## What fires

| Alert | When | Key |
|---|---|---|
| Malicious PR | an incident in the `threats` registry for a PR that is open, first noticed within `WINDOW_DAYS` (2) | `malicious:<pr>` |
| Paperclip credential | an open `rotate-secret` action item that does not read as a test fixture, created within `WINDOW_DAYS`, on an open PR whose author is a maintainer (`gates.priority_author`) | `secret:<pr>` |

A contributor working from a fork never holds Paperclip's own secrets, so a
Paperclip credential realistically leaks through a maintainer's PR; a
contributor's leak of their own instance secret stays in the app's Action
items. The window and the open-PR rule keep a first deploy from announcing
history.

Messages are Slack mrkdwn with the PR linked to GitHub. Every outside-written
string (title, author, file path) has `&`, `<` and `>` escaped, so a PR title
cannot mention the channel or forge a link. A credential message names the file
the evidence points at and never the value.

## Where it runs

`pipeline/notify.py`, called at the end of `threat_scan.scan` (the worker's
ten-minute pass, the Control-tab job, the CLI, the pre-merge rescan) with the
registries the scan just committed. It loads only the candidate PRs, posts
through an incoming webhook (`httpx`, 10-second timeout), and never raises into
the scan. With `TRIAGE_SLACK_WEBHOOK_URL` unset it does nothing.

## Exactly once

Each alert is claimed in the shared store before it is posted: a registry row
`notify:<key>` inserted if absent, then read under a row lock. A machine claims
a row that is new, that failed fewer than `MAX_TRIES` (3) times, or that has
sat `sending` longer than `STALE_SECONDS` (600). It records `sent` or `failed`
after the POST. Two machines scanning the same PR post one message; a Slack
outage is retried by later passes, three times at most.

## Configuration and sharing

- `TRIAGE_SLACK_WEBHOOK_URL` is a registered `secret` setting in `.env`, shown
  set or unset on the Setup page's settings card, never edited there.
- The chat agent's environment drops it (`safety_guard._AGENT_ENV_DROP`): the
  agent's environment passes every `TRIAGE_*` variable, and a prompt in a PR
  could otherwise print the URL and post to the channel.
- The Setup tab gets a Slack alerts card: paste the webhook URL (validated as
  `https://hooks.slack.com/…`), save it through a new onboarding step
  `notify` (open on a configured deployment, the only key it may write), clear
  it, and send a test message (`POST /api/onboarding/notify/test`). The
  onboarding state reports `slack_alerts` (set or not), never the URL.
- The join bundle carries the URL with the rest of the deployment env, so a
  machine that joins posts to the same channel.
- `uv run python -m pipeline.notify --test` posts a test message.

## Testing

Unit tests for which alerts are due (open vs closed, inside vs outside the
window, maintainer vs contributor, fixture vs live, already sent), the message
text and its escaping (no secret value, no mention), the store claim and its
retry and stale rules, the scan calling the notifier with the POST stubbed, the
agent environment dropping the URL, the onboarding step and the bundle, and the
Setup card's API.
