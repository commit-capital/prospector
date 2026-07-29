from issue_triage.pain_score import severity_multiplier, percentile_normalize, pain_for_cluster

WEIGHTS = {"weights": {"reporters": 0.5, "reactions": 0.25, "comments": 0.25},
           "severity": {"k": 0.34, "cap": 1.0, "keywords": ["crash", "security"], "labels": ["security"]}}

_NORMS = {"reporters": {1: 0.0, 9: 1.0}, "reactions": {0: 0.0, 20: 1.0},
          "comments": {0: 0.0, 15: 1.0}}


def test_severity_multiplier_clamped():
    # 3 keyword hits * 0.34 = 1.02 -> capped at 1.0 -> multiplier 2.0
    m = severity_multiplier("crash crash crash security", ["security"], WEIGHTS["severity"])
    assert m == 2.0


def test_severity_multiplier_none():
    assert severity_multiplier("typo fix", [], WEIGHTS["severity"]) == 1.0


def test_percentile_normalize_monotonic():
    norm = percentile_normalize([1, 5, 10])
    assert norm[1] < norm[5] < norm[10]
    assert 0.0 <= norm[1] <= 1.0 and norm[10] == 1.0


def test_pain_rewards_reporters_and_severity():
    low = pain_for_cluster(reporters=1, reactions=0, comments=0,
                           text="minor typo", labels=[], norms=_NORMS, w=WEIGHTS)
    high = pain_for_cluster(reporters=9, reactions=20, comments=15,
                            text="app crash on save", labels=["security"], norms=_NORMS, w=WEIGHTS)
    assert high > low
