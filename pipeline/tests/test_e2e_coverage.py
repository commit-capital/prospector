from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pipeline import e2e_coverage, profile, verify_driver
from pipeline.e2e_artifacts import Artifacts
from pipeline.e2e_report import E2eRun, SpecResult, SpecStatus

PNG = b"\x89PNG\r\n\x1a\n"


def _res(title: str, status: SpecStatus, error: str | None = None,
         attachments: list[str] | None = None, file: str = "a.spec.ts") -> SpecResult:
    return SpecResult(file, title, status, error, attachments or [])


def _run(*results: SpecResult) -> E2eRun:
    return E2eRun(booted=True, results=list(results), problem=None)


def _by_title(cov: list[e2e_coverage.SpecCoverage]) -> dict[str, e2e_coverage.SpecCoverage]:
    return {c.title: c for c in cov}


def test_reliable_means_every_run_passed():
    cov = e2e_coverage.classify([_run(_res("t", "passed")) for _ in range(3)])
    assert [(c.verdict, c.passes, c.runs) for c in cov] == [("reliable", 3, 3)]


def test_any_mixed_result_is_flaky():
    mixed = e2e_coverage.classify([_run(_res("t", s)) for s in ("passed", "failed", "passed")])
    retried = e2e_coverage.classify([_run(_res("t", s)) for s in ("passed", "flaky", "passed")])
    assert mixed[0].verdict == "flaky" and mixed[0].passes == 2
    assert retried[0].verdict == "flaky"


def test_never_passed_is_failing():
    runs = [_run(_res("t", "failed", "expect(locator).toBeVisible() failed",
                      ["x/shot.png"])) for _ in range(3)]
    (c,) = e2e_coverage.classify(runs)
    assert (c.verdict, c.passes, c.cause) == ("failing", 0, "unknown")
    assert c.evidence == "expect(locator).toBeVisible() failed"
    assert c.screenshot == "x/shot.png"


def test_timed_out_without_error_is_a_timeout():
    (c,) = e2e_coverage.classify([_run(_res("t", "timedOut"))])
    assert (c.verdict, c.cause, c.evidence) == ("failing", "timeout", None)


def test_a_run_that_did_not_boot_counts_against_no_spec():
    down = E2eRun(booted=False, results=[], problem="app never came up")
    empty = E2eRun(booted=True, results=[], problem=None)
    runs = [_run(_res("t", "passed")), down, empty]
    (c,) = e2e_coverage.classify(runs)
    assert (c.verdict, c.runs) == ("reliable", 1)
    assert e2e_coverage.boot_failures(runs) == ["app never came up", "no results"]


def test_a_spec_missing_from_a_run_is_flaky_and_skips_are_set_aside():
    runs = [_run(_res("t", "passed"), _res("s", "skipped")), _run(_res("s", "skipped"))]
    cov = _by_title(e2e_coverage.classify(runs))
    assert (cov["t"].verdict, cov["t"].passes, cov["t"].runs) == ("flaky", 1, 2)
    assert "s" not in cov
    assert e2e_coverage.skipped_count(runs) == 1


@pytest.mark.parametrize("err,cause", [
    ("getaddrinfo ENOTFOUND api.openai.com", "network"),
    ("connect ECONNREFUSED 140.82.112.3:443", "network"),
    ("401 Unauthorized: invalid api key", "secret"),
    ("browserType.launch: Failed to launch: libnss3.so: cannot open shared object", "sandbox"),
    ("Test timeout of 60000ms exceeded.", "timeout"),
    ("expect(locator).toBeVisible() failed", "unknown")])
def test_cause_hint(err, cause):
    assert e2e_coverage.cause_hint(err) == cause


def _cov(reliable: int, flaky: int, failing: int) -> list[e2e_coverage.SpecCoverage]:
    out = []
    for verdict, n in (("reliable", reliable), ("flaky", flaky), ("failing", failing)):
        for i in range(n):
            out.append(e2e_coverage.SpecCoverage(
                file=f"{verdict}.spec.ts", title=f"t{i}", verdict=verdict, passes=0, runs=3,
                cause=None if verdict == "reliable" else "unknown",
                evidence=None if verdict == "reliable" else "a | b\nc", screenshot=None))
    return out


def test_render_leads_with_the_count():
    out = e2e_coverage.render(_cov(reliable=40, flaky=2, failing=2), base_sha="abc",
                              boot_failures=["no results"], skipped=3, runs=3)
    lines = out.splitlines()
    assert lines[0] == "# E2E coverage at abc: 40 of 44 specs reliable"
    assert "failing.spec.ts › t0" in out
    assert "3 skipped" in out and "no results" in out
    assert "a \\| b c" in out


def test_render_says_when_no_run_booted():
    out = e2e_coverage.render([], base_sha="abc", boot_failures=["no report"] * 3, runs=3)
    assert out.splitlines()[0] == "# E2E coverage at abc: no run booted (3 of 3 runs)"
    assert "no report" in out


def test_render_says_when_runs_booted_but_no_spec_ran():
    out = e2e_coverage.render([], base_sha="abc", boot_failures=["x"], skipped=4, runs=3)
    assert out.splitlines()[0] == "# E2E coverage at abc: no specs ran"


def test_render_lists_notes():
    out = e2e_coverage.render(_cov(1, 0, 0), base_sha="abc", boot_failures=[], runs=1,
                              notes=["could not write shots/a.png: disk full"])
    assert "## Notes" in out and "- could not write shots/a.png: disk full" in out


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The CLI on a pinned tier-0 base with an e2e profile and a browser."""
    monkeypatch.setattr(verify_driver, "SCRATCH", tmp_path)
    monkeypatch.setattr(verify_driver, "local_pin",
                        lambda store: {"base_sha": "f" * 40, "tier": 0})
    monkeypatch.setattr(profile, "active", lambda: profile.parse_profile(
        {"version": 1, "verify": {"e2e": {"config": "e2e/p.config.ts"}}}, "t"))
    monkeypatch.setattr(verify_driver, "base_has_browser", lambda image: True)
    monkeypatch.setattr(verify_driver, "e2e_specs", lambda src, cfg: ["e2e/a.spec.ts"])
    return ["e2e-coverage", "--store", str(tmp_path / "s.db")]


def _ok_run(image: str, **kw: object):
    return 0, _run(_res("ok", "passed")), Artifacts(report=None, pngs={}, skipped=[])


def _report_of(capsys) -> str:
    return Path(capsys.readouterr().out.strip().splitlines()[-1]).read_text()


@pytest.mark.parametrize("unready, message", [
    ("profile", "no verify.e2e in the profile"),
    ("browser", "re-run prepare-base after adding verify.e2e to the profile")])
def test_cli_checks_the_profile_and_browser_once_before_any_run(
        cli, monkeypatch, capsys, unready, message):
    if unready == "profile":
        monkeypatch.setattr(profile, "active", lambda: profile.parse_profile({"version": 1}, "t"))
    else:
        monkeypatch.setattr(verify_driver, "base_has_browser", lambda image: False)
    monkeypatch.setattr(verify_driver, "run_e2e", lambda *a, **k: pytest.fail("ran"))
    assert verify_driver.main([*cli, "--runs", "3"]) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("exc", [OSError("docker gone"), subprocess.TimeoutExpired("docker", 5),
                                 RuntimeError("x" * 400)])
def test_cli_keeps_earlier_runs_when_one_raises(cli, monkeypatch, capsys, exc):
    calls: list[int] = []

    def flaky_run(image: str, **kw: object):
        calls.append(1)
        if len(calls) == 2:
            raise exc
        return _ok_run(image)

    monkeypatch.setattr(verify_driver, "run_e2e", flaky_run)
    assert verify_driver.main([*cli, "--runs", "3"]) == 0
    report = _report_of(capsys)
    assert len(calls) == 3
    assert report.splitlines()[0] == f"# E2E coverage at {'f' * 12}: 1 of 1 specs reliable"
    line = next(ln for ln in report.splitlines() if "run 2 raised" in ln)
    assert f"run 2 raised {type(exc).__name__}: " in line
    assert len(line) <= len("- ") + 300


def test_cli_lets_a_keyboard_interrupt_through(cli, monkeypatch):
    def interrupted(image: str, **kw: object):
        raise KeyboardInterrupt
    monkeypatch.setattr(verify_driver, "run_e2e", interrupted)
    with pytest.raises(KeyboardInterrupt):
        verify_driver.main([*cli, "--runs", "2"])


def test_cli_skips_a_screenshot_it_cannot_write(cli, monkeypatch, capsys):
    def failing_run(image: str, **kw: object):
        run = _run(_res("bad", "failed", "boom", ["bad/shot.png"]))
        return 1, run, Artifacts(report=None, pngs={"bad/shot.png": PNG}, skipped=[])

    monkeypatch.setattr(verify_driver, "run_e2e", failing_run)
    real_write = Path.write_bytes

    def write_bytes(self: Path, data: bytes) -> int:
        if self.name == "shot.png":
            raise OSError("disk full")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    assert verify_driver.main([*cli, "--runs", "1"]) == 0
    report = _report_of(capsys)
    assert "could not write shots/bad/shot.png: disk full" in report
    row = next(ln for ln in report.splitlines() if "bad" in ln and "boom" in ln)
    assert "shots/" not in row


@pytest.mark.parametrize("run, headline", [
    (E2eRun(booted=False, results=[], problem="no report"), "no run booted (2 of 2 runs)"),
    (_run(_res("s", "skipped")), "no specs ran")])
def test_cli_fails_when_nothing_counted(cli, monkeypatch, capsys, run, headline):
    monkeypatch.setattr(verify_driver, "run_e2e", lambda image, **kw: (
        1, run, Artifacts(report=None, pngs={}, skipped=[])))
    assert verify_driver.main([*cli, "--runs", "2"]) == 1
    assert _report_of(capsys).splitlines()[0] == f"# E2E coverage at {'f' * 12}: {headline}"


def test_cli_writes_the_report_and_the_failing_screenshot(tmp_path, cli, monkeypatch, capsys):
    calls: list[str] = []

    def fake_run_e2e(image: str, **kw: object):
        calls.append(image)
        run = _run(_res("ok", "passed"),
                   _res("bad", "failed", "boom", ["bad/shot.png"]))
        return 0, run, Artifacts(report=None, pngs={"bad/shot.png": PNG}, skipped=[])

    monkeypatch.setattr(verify_driver, "run_e2e", fake_run_e2e)
    rc = verify_driver.main([*cli, "--runs", "2"])
    assert rc == 0 and calls == ["pr-verify-base:ffffffffffff-t0"] * 2
    report = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert report.is_file() and report.is_relative_to(tmp_path / "e2e-coverage")
    assert report.read_text().splitlines()[0] == (
        f"# E2E coverage at {'f' * 12}: 1 of 2 specs reliable")
    assert (report.parent / "shots" / "bad" / "shot.png").read_bytes() == PNG


def test_cli_runs_the_listed_specs_and_notes_a_booted_runs_top_level_error(
        cli, monkeypatch, capsys):
    seen: list[object] = []

    def run(image: str, **kw: object):
        seen.append(kw["specs"])
        e2e = E2eRun(booted=True, results=[_res("ok", "passed")],
                     problem="Timed out waiting 1800s for the entire test run")
        return 20, e2e, Artifacts(report=None, pngs={}, skipped=[])

    monkeypatch.setattr(verify_driver, "run_e2e", run)
    assert verify_driver.main([*cli, "--runs", "1"]) == 0
    assert seen == [["e2e/a.spec.ts"]]
    assert "- run 1: Timed out waiting 1800s for the entire test run" in _report_of(capsys)
