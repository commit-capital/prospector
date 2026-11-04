import { useEffect, useState } from "react";
import { BugfixButton } from "@commit-capital/bugfix-button";
import { api, type FeedbackTarget } from "../api";

export function FeedbackButton() {
  const [target, setTarget] = useState<FeedbackTarget | null>(null);

  useEffect(() => {
    api.feedbackTarget().then(setTarget).catch(() => {});
  }, []);

  // Without PROSPECTOR_FEEDBACK_REPO there is nowhere to file.
  if (!target?.repo) return null;

  const details = [
    target.branch && `branch \`${target.branch}\``,
    target.worktree && `worktree \`${target.worktree}\``,
  ].filter((d): d is string => Boolean(d));

  return (
    <BugfixButton
      repo={target.repo}
      assignee={target.assignee ?? undefined}
      labels={target.labels}
      appName="Prospector"
      details={details}
      generate={api.generateFeedback}
      placement="inline"
      buttonClassName="feedback-btn"
    />
  );
}
