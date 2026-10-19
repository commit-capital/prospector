---
name: suggestions
description: Get product suggestions for the next incremental improvements to Prospector. Delegates to the Product Analyst agent for a prioritized, effort-to-impact-ranked review of the triage workflow.
---

# Product Suggestions

1. Delegate to a subagent (`Explore` or `general-purpose`) with
   `.claude/agents/product-analyst.md` as its instructions. Pass the user's
   focus area if they named one; otherwise ask for the 3–5 highest-impact
   improvements, ranked by effort to impact.
2. Present the suggestions in the agent's format and ask which to implement.
