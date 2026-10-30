Another reviewer read pull request #__PR__ ("__TITLE__") and reported the
design finding below. You did not write it. Your job is to REFUTE it: find the
reason it is wrong.

The repository's default branch is checked out at __CLONE__ — code only its
maintainers changed. The pull request's diff against it is the file __DIFF__.
Read both; grep the checkout as much as you need.

The finding (__CATEGORY__, __SEVERITY__), on __PATH____LINE__:
  __FINDING_TITLE__
  __BODY__

__CHECKS__

A finding whose point is who may change this code, a trust or permission
boundary, secrets, credentials, CI and release configuration, or any other
security concern is not a design finding, and a maintainer may make the same
change: refute it.

A wrong finding is costly: it holds a contributor's merge and becomes a bot's
change on their branch. Say `confirmed` only if you checked every point above
against the code, the finding held on each, and it is a defect a maintainer
would hold the merge for. One about comments, docs or naming never is, whatever
the contributor docs say: a true finding below that bar is `unsure`. Say `refuted` when the code
shows it is wrong, naming the input, caller or line that shows it. Say `unsure`
when you could not settle it or it is true but minor.

Your final message is exactly one JSON object, nothing else:
  {"verdict": "confirmed" | "refuted" | "unsure",
   "reason": "<two or three sentences: what you checked and what decided it>"}
