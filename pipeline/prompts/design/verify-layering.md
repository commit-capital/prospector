The finding says the change sits outside the layer that owns the behavior.
Grep for every caller of the code it names, count them, and say whether moving
it where the finding asks changes behavior for any of them. A move that changes
other callers' behavior or widens the change past what the pull request does is
a scope change, not a layering defect: refute it.