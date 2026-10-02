from __future__ import annotations

import pytest
from pipeline import progress


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(progress, "_clock", c)
    return c


def test_duration_reads_like_a_person_would_say_it():
    assert progress.duration(0.4) == "0s"
    assert progress.duration(42) == "42s"
    assert progress.duration(125) == "2m 05s"
    assert progress.duration(3 * 3600 + 120) == "3h 02m"


def test_progress_announces_counts_and_finishes(capsys, clock):
    p = progress.Progress("processing", 300, "new PRs")
    for _ in range(10):
        clock.t += 1
        p.advance()
    p.finish("12 changed")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "  processing 300 new PRs…"
    assert out[-1] == "  processing new PRs: done, 10 of 300 in 10s — 12 changed"
    assert "  processing new PRs: 3 of 300 (1%) · 3s · ~4m 57s left" in out


def test_progress_prints_at_most_once_per_interval(capsys, clock):
    p = progress.Progress("linking", 1000, "alerts")
    for _ in range(1000):
        clock.t += 2 ** -9
        p.advance()
    p.finish()
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["  linking 1,000 alerts…", "  linking alerts: done, 1,000 in 1s"]


def test_track_walks_every_item(capsys, clock):
    assert list(progress.track([1, 2, 3], "checking", "PRs")) == [1, 2, 3]
    assert capsys.readouterr().out.splitlines()[-1] == "  checking PRs: done, 3 in 0s"


def test_store_reads_are_reported_only_when_enabled(monkeypatch, capsys, clock):
    monkeypatch.delenv(progress.ENV, raising=False)
    assert progress.store_read("prs", lambda: 6110) is None
    monkeypatch.setenv(progress.ENV, "1")
    p = progress.store_read("prs", lambda: 6110)
    assert p is not None
    assert capsys.readouterr().out == "  downloading 6,110 PRs from the store…\n"


def test_small_store_reads_stay_quiet(monkeypatch, clock):
    monkeypatch.setenv(progress.ENV, "1")
    assert progress.store_read("alerts", lambda: 40) is None


def test_a_step_of_no_items_prints_nothing(capsys, clock):
    progress.Progress("saving", 0, "alerts").finish()
    assert list(progress.track([], "checking", "PRs")) == []
    assert capsys.readouterr().out == ""


def test_a_single_item_reads_in_the_singular(capsys, clock):
    progress.Progress("saving", 1, "changed alerts", one="changed alert")
    assert capsys.readouterr().out == "  saving 1 changed alert…\n"


def test_steps_reach_the_reporter_only_inside_its_block() -> None:
    seen: list[str] = []
    progress.step("before")
    with progress.reporting_steps(seen.append):
        progress.step("cloning")
        progress.step("building")
    progress.step("after")
    assert seen == ["cloning", "building"]


def test_a_failing_reporter_never_fails_the_step() -> None:
    def boom(what: str) -> None:
        raise RuntimeError(what)
    with progress.reporting_steps(boom):
        progress.step("cloning")
