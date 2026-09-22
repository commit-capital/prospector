"""advisory_dups: cycle detection over duplicate_of edges and chain
resolution to one canonical member per group."""
from alert_triage import advisory_dups

A, B, C, D = ("GHSA-aaaa-aaaa-aaaa", "GHSA-bbbb-bbbb-bbbb",
              "GHSA-cccc-cccc-cccc", "GHSA-dddd-dddd-dddd")


def test_would_cycle_direct_and_transitive():
    assert advisory_dups.would_cycle({}, A, A) is True
    assert advisory_dups.would_cycle({}, A, B) is False
    assert advisory_dups.would_cycle({B: A}, A, B) is True
    assert advisory_dups.would_cycle({B: C, C: A}, A, B) is True
    assert advisory_dups.would_cycle({B: C}, A, B) is False


def test_would_cycle_terminates_on_existing_loops():
    # A pre-existing cycle the new edge does not touch never loops the walk.
    assert advisory_dups.would_cycle({C: D, D: C}, A, C) is False


def test_canonical_of_follows_chains():
    pointers = {A: B, B: C}
    assert advisory_dups.canonical_of(pointers, A) == C
    assert advisory_dups.canonical_of(pointers, B) == C
    assert advisory_dups.canonical_of(pointers, C) == C
    assert advisory_dups.canonical_of(pointers, D) == D


def test_canonical_of_resolves_cycles_to_one_member():
    pointers = {A: B, B: A}
    assert advisory_dups.canonical_of(pointers, A) == A
    assert advisory_dups.canonical_of(pointers, B) == A


def test_canonical_of_chain_into_a_cycle():
    pointers = {D: B, B: C, C: B}
    # D's chain lands in the B↔C loop; every entry resolves to the loop's
    # lexicographically first member.
    assert advisory_dups.canonical_of(pointers, D) == B
    assert advisory_dups.canonical_of(pointers, C) == B
    assert advisory_dups.canonical_of(pointers, B) == B
