// The prefix every Slack incoming webhook URL carries — the same check the
// backend's `notify` setup step makes before it writes one.
const SLACK_HOOK_PREFIX = "https://hooks.slack.com/";

/** Why `url` cannot be saved as the Slack alerts webhook, or null when it can. */
export function slackHookProblem(url: string): string | null {
  return url.trim().startsWith(SLACK_HOOK_PREFIX)
    ? null
    : `a Slack incoming webhook starts with ${SLACK_HOOK_PREFIX}`;
}
