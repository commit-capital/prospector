"""cluster_id on activity events (#43): every pr-bearing event carries a
3-digit zero-padded cluster id, or null for a PR outside any cluster.
When a PR straddles multiple clusters, the primary (lowest id) is used."""
from review_cockpit.backend import data
from review_cockpit.backend import executor


def test_cluster_id_is_zero_padded_3_digits(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {236: [1]})
    assert executor._cluster_id(236) == "001"


def test_cluster_id_uses_primary_when_multi(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {236: [42, 7]})
    assert executor._cluster_id(236) == "007"        # min of the membership list


def test_cluster_id_is_none_for_unclustered_pr(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {})
    assert executor._cluster_id(99999) is None


def test_cluster_id_accepts_str_pr(monkeypatch):
    monkeypatch.setattr(data, "pr_to_clusters", lambda: {236: [7]})
    assert executor._cluster_id("236") == "007"
