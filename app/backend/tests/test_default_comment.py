"""The /api/default-comment endpoint lets the Disposition panel preview & edit
the comment a manual close will post (#77). It must mirror the executor's
fallback exactly (decisions.default_comment)."""
from app.backend import app
from app.backend import decisions
from app.backend import models


def test_triage_close():
    assert app.default_comment("CLOSE")["comment"] == decisions.default_comment(models.CloseAction(action="CLOSE"))
    assert "triage" in app.default_comment("CLOSE")["comment"].lower()


def test_close_dup_includes_canonical():
    out = app.default_comment("CLOSE_DUP", canonical=1234)["comment"]
    assert "#1234" in out
    assert "duplicate" in out.lower()


def test_close_dup_drops_unfounded_coverage_claim():
    """#184: the default dup comment must NOT assert the canonical has 'broader
    test coverage' — that boilerplate was repetitive and frequently untrue."""
    for canon in (1, 2, 3, 1234, 9999):
        out = decisions.default_comment(models.CloseAction(action="CLOSE_DUP", canonical=canon))
        assert "broader test coverage" not in out.lower()


def test_close_dup_states_reason_only_when_given():
    """#184: only claim *why* the canonical is preferred when a genuine reason is
    supplied — and then say exactly that, verbatim."""
    plain = decisions.default_comment(models.CloseAction(action="CLOSE_DUP", canonical=1234))
    assert "broader test coverage" not in plain.lower()
    withreason = decisions.default_comment(
        models.CloseAction(action="CLOSE_DUP", canonical=1234,
                           dup_reason="which adds broader test coverage"))
    assert "broader test coverage" in withreason.lower()


def test_close_dup_varies_across_canonicals():
    """#184: a queue of dupe-closes shouldn't read as the identical boilerplate
    line every time — the wording varies deterministically by canonical."""
    bodies = {decisions.default_comment(models.CloseAction(action="CLOSE_DUP", canonical=c))
              for c in (1, 2, 3)}
    assert len(bodies) > 1


def test_close_dup_is_deterministic():
    """Same action → same comment, so the panel preview matches what's posted."""
    a = models.CloseAction(action="CLOSE_DUP", canonical=77)
    assert decisions.default_comment(a) == decisions.default_comment(a.model_copy())


def test_close_dup_without_canonical_is_neutral():
    out = app.default_comment("CLOSE_DUP")["comment"]
    assert "#" not in out and "duplicate" in out.lower()


def test_close_fixed_cites_pr_not_commit():
    """A close-fixed comment cites the upstream PR (+ merge date), never the raw
    commit hash — the repo squash-merges, so a branch commit the agent picked may
    not exist in the default branch's history and only adds noise over the
    clickable PR link."""
    out = decisions.default_comment(models.CloseAction(
        action="CLOSE_FIXED", upstream_pr=8331,
        upstream_commit="364f0f5a8d4426910bb630e65570fd3b3df4ff4c",
        upstream_date="2026-06-19"))
    assert "#8331" in out
    assert "merged 2026-06-19" in out
    assert "364f0f5a" not in out and "commit" not in out.lower()


def test_close_fixed_names_the_configured_default_branch():
    """The comment names the repo's actual default branch (conftest pins
    TRIAGE_DEFAULT_BRANCH=trunk), never a hardcoded branch name."""
    out = decisions.default_comment(models.CloseAction(
        action="CLOSE_FIXED", upstream_pr=8331, upstream_date="2026-06-19"))
    assert "landed on trunk" in out
    assert "master" not in out


def test_close_fixed_without_pr_hedges():
    """With no upstream PR to cite, don't fabricate a reference — hedge honestly.
    A bare commit or date must not stand in as the citation."""
    out = decisions.default_comment(models.CloseAction(
        action="CLOSE_FIXED",
        upstream_commit="364f0f5a8d4426910bb630e65570fd3b3df4ff4c"))
    assert "#" not in out
    assert "364f0f5a" not in out
    assert "already landed" in out.lower()
    assert "trunk" in out and "master" not in out


def test_close_stale():
    assert "inactive" in app.default_comment("CLOSE_STALE")["comment"].lower()


def test_close_oversized_encourages_splitting():
    """#183: the oversized-close comment asks the author to break it into smaller PRs."""
    out = decisions.default_comment(models.CloseAction(action="CLOSE_OVERSIZED"))
    assert "smaller" in out.lower()
    assert "#" not in out  # no merge PRs given → no stray references


def test_close_oversized_references_cluster_merge_prs():
    """#183: when the cluster is already merging PRs, name them so the author
    doesn't resubmit that work."""
    out = decisions.default_comment(models.CloseAction(action="CLOSE_OVERSIZED", merge_prs=[12, 34]))
    assert "#12" in out and "#34" in out
    assert "smaller" in out.lower()


def test_unknown_action_is_empty():
    assert app.default_comment("MERGE")["comment"] == ""


def test_close_oversized_suggestion_pulls_cluster_merge_prs(monkeypatch):
    """#183: a close-oversized suggestion collects the cluster's open merge PRs
    and bakes their references into the comment it would post."""
    from app.backend import data
    from app.backend import suggest
    from types import SimpleNamespace
    target = SimpleNamespace(number=5, rationale=None)
    merge_pr = SimpleNamespace(number=7, disposition="merge", state="open")
    dup_pr = SimpleNamespace(number=8, disposition="close-dup", state="open")
    closed_merge = SimpleNamespace(number=9, disposition="merge", state="closed")  # not open → skip
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {5: [99], 7: [99], 8: [99], 9: [99]})
    monkeypatch.setattr(data, "prs", lambda: {5: target, 7: merge_pr, 8: dup_pr, 9: closed_merge})

    assert suggest._cluster_merge_prs(target) == [7]
    s = suggest.suggest_for_record(target, "close-oversized")
    assert s["action"] == "CLOSE_OVERSIZED"
    assert s["accept"].merge_prs == [7]
    assert "#7" in s["bot_comment"] and "smaller" in s["bot_comment"].lower()
