from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import e2e_coverage, verify_driver
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
                              boot_failures=["no results"], skipped=3)
    lines = out.splitlines()
    assert lines[0] == "# E2E coverage at abc: 40 of 44 specs reliable"
    assert "failing.spec.ts › t0" in out
    assert "3 skipped" in out and "no results" in out
    assert "a \\| b c" in out


def test_cli_writes_the_report_and_the_failing_screenshot(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(verify_driver, "SCRATCH", tmp_path)
    monkeypatch.setattr(verify_driver, "local_pin",
                        lambda store: {"base_sha": "f" * 40, "tier": 0})
    calls: list[str] = []

    def fake_run_e2e(image: str, **kw: object):
        calls.append(image)
        run = _run(_res("ok", "passed"),
                   _res("bad", "failed", "boom", ["bad/shot.png"]))
        return 0, run, Artifacts(report=None, pngs={"bad/shot.png": PNG}, skipped=[])

    monkeypatch.setattr(verify_driver, "run_e2e", fake_run_e2e)
    rc = verify_driver.main(["e2e-coverage", "--runs", "2", "--store", str(tmp_path / "s.db")])
    assert rc == 0 and calls == ["pr-verify-base:ffffffffffff-t0"] * 2
    report = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert report.is_file() and report.is_relative_to(tmp_path / "e2e-coverage")
    assert report.read_text().splitlines()[0] == (
        f"# E2E coverage at {'f' * 12}: 1 of 2 specs reliable")
    assert (report.parent / "shots" / "bad" / "shot.png").read_bytes() == PNG
