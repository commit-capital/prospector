# Reviewer prompts

What Prospector's own reviewers are told, one file per prompt. Edit a file here
to change what a reviewer is asked. `__NAME__` placeholders are filled per run by
the module named below (`headless_agent.fill`).

A file is used byte for byte, so a trailing newline is part of the prompt.
Editing a prompt does not re-review PRs already reviewed; for the design review,
bumping `reviewers.DESIGN_RUBRIC` does. Each module's `PROMPT_SHA` names the
files that make a new prompt version in the reviewer eval.

## Design review

Judges reuse, layering, the repository's conventions and test strength. Its
blocking and major findings hold a merge (`TRIAGE_DESIGN_REVIEW`), and
`TRIAGE_DESIGN_COMMENT` decides whose PRs get them posted as a comment.

| File | Role |
|---|---|
| `design/review.md` | The reviewer. |
| `design/verify.md` | The verifier, one run per blocking or major finding, asked to refute it. |
| `design/verify-<category>.md` | What the verifier checks for each category of finding. |

What the reviewer reads (`pipeline/design_review.py`):

- the PR's diff, as GitHub serves it;
- the repository's default branch, checked out on the worker;
- the repository's contributor docs from that checkout (`profile.authoring.contributor_docs`,
  such as `AGENTS.md` and `CONTRIBUTING.md`) and its `rules/*.mdc`;
- with `TRIAGE_DESIGN_COVERAGE` on, which changed lines the PR's tests execute.

It has no GitHub access, no edits and no environment. The bar a review must
clear is in `pipeline/reviewers.py`.

## Security review

Three reviewers, one per lens, then a verifier over their findings, batched.
The verdict (RED, YELLOW or GREEN) is never posted on the PR.

| File | Role |
|---|---|
| `security/review.md` | The reviewer, run once per lens. |
| `security/lens-<lens>.md` | What each lens looks for. |
| `security/verify.md` | The verifier, asked to refute each finding. |
| `security/*-reply.md` | The JSON reply format each must return. |

What the reviewer reads (`pipeline/security_review.py`): the PR's diff, the
title, other automated reviewers' findings as untrusted evidence, and the
upstream repository through read-only `gh` when the diff lacks context.
