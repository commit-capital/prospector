---
name: suggestions
description: Get product suggestions for the next incremental improvements to Prospector. Delegates to the Product Analyst agent for a prioritized, effort-to-impact-ranked review of the triage workflow.
---

# Product Suggestions

You are being asked to suggest improvements to Prospector, the PR/issue triage
pipeline and review cockpit configured by `TRIAGE_REPO` and
`TRIAGE_BOT_LOGIN`. Use the
Product Analyst agent to analyze the current state and propose actionable next
steps.

1. Read the Product Analyst agent prompt from `.claude/agents/product-analyst.md` to understand the design principles and suggestion format.

2. If the user provided a focus area (e.g., "clustering accuracy", "cockpit UX", "security gate", "issue triage", "executor safety"), note it for the agent. Otherwise, prepare for an open-ended review.

3. Run the Product Analyst workflow from `.claude/agents/product-analyst.md`. Delegate the analysis to a subagent using that prompt as the instruction prefix (the `Explore` or `general-purpose` agent type works well). Let the workflow explore the codebase on its own rather than pre-enumerating files for it. If the user specified a focus area, ask for detailed suggestions in that area. Otherwise, ask for an open-ended review: the next 3-5 highest-impact improvements, prioritized by effort-to-impact ratio.

4. Present the suggestions to the user in a clear, readable format. For each suggestion, include:
   - A short title
   - What it adds and why it matters for the triage workflow (merge / request-changes / close)
   - Scope (data-only, config change, small code change, new feature, new policy/gate change)
   - Specific enough details that you could implement it immediately if asked

5. Ask the user which suggestions they'd like to implement (they can pick one, several, or ask for different ideas).
