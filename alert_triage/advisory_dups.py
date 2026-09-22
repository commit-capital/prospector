"""Duplicate-chain resolution for advisories — the ONE place `duplicate_of`
pointers become groups with a single canonical member. `pointers` is the
current edge map, each duplicate-marked advisory's GHSA to its target's."""
from __future__ import annotations


def would_cycle(pointers: dict[str, str], source: str, target: str) -> bool:
    """True when pointing `source` at `target` closes a loop: `target`'s chain
    under `pointers` reaches `source` (a self-pointer included)."""
    seen: set[str] = set()
    cur: str | None = target
    while cur is not None and cur not in seen:
        if cur == source:
            return True
        seen.add(cur)
        cur = pointers.get(cur)
    return False


def canonical_of(pointers: dict[str, str], ghsa: str) -> str:
    """The GHSA `ghsa`'s group resolves to: the end of its pointer chain, or —
    when the chain loops — the cycle's lexicographically first member, so every
    member of a cycle names the same canonical and no canonical points onward."""
    seen: list[str] = []
    cur = ghsa
    while cur in pointers and cur not in seen:
        seen.append(cur)
        cur = pointers[cur]
    if cur in seen:
        cycle = seen[seen.index(cur):]
        return min(cycle)
    return cur
