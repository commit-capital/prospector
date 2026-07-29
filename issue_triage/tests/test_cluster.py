from issue_triage.cluster_issues import cluster_issues


def test_shared_subsystem_and_identifier_cluster_together():
    summaries = [
        {"number": 1, "subsystem": "execution-locks", "identifiers": ["executionRunId"]},
        {"number": 2, "subsystem": "execution-locks", "identifiers": ["executionRunId"]},
        {"number": 3, "subsystem": "inbox", "identifiers": ["badgeCount"]},
    ]
    clusters = cluster_issues(summaries)
    members = {c["id"]: set(c["members"]) for c in clusters}
    assert any({1, 2} <= m for m in members.values())
    assert any(m == {3} for m in members.values())


def test_every_issue_lands_in_a_cluster():
    summaries = [{"number": 9, "subsystem": "other", "identifiers": []}]
    clusters = cluster_issues(summaries)
    assert sum(len(c["members"]) for c in clusters) == 1
