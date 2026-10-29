from __future__ import annotations

import json
from pathlib import Path

from pipeline.e2e_report import E2eRun, SpecResult, parse_report

FIXTURE = Path(__file__).parent / "fixtures" / "playwright-report.json"


def _real() -> dict:
    return json.loads(FIXTURE.read_text())


def _run(report: object) -> E2eRun:
    return parse_report(json.dumps(report).encode(), output_tail="")


def _spec(title: str, results: list[dict], **extra: object) -> dict:
    return {"title": title, "file": "a.spec.ts", "tests": [{"results": results, **extra}]}


def _report(*specs: dict) -> dict:
    return {"config": {"rootDir": "/r"}, "suites": [{"title": "a.spec.ts", "file": "a.spec.ts", "specs": list(specs)}]}


def _by_title(run: E2eRun) -> dict[str, SpecResult]:
    return {r.title: r for r in run.results}


def test_parses_passed_and_failed_specs():
    run = parse_report(FIXTURE.read_bytes(), output_tail="")
    assert run.booted and run.problem is None
    got = [(r.file, r.title, r.status) for r in run.results]
    assert got == [
        ("x.spec.ts", "suite A › passes", "passed"),
        ("x.spec.ts", "suite A › fails with long message", "failed"),
        ("x.spec.ts", "suite A › flaky one", "flaky"),
        ("x.spec.ts", "suite A › skipped one", "skipped"),
    ]


def test_retried_pass_is_flaky_without_outcome_field():
    r = _report(_spec("t", [{"status": "failed"}, {"status": "passed"}]))
    assert _run(r).results[0].status == "flaky"


def test_error_is_first_line_capped():
    run = parse_report(FIXTURE.read_bytes(), output_tail="")
    assert _by_title(run)["suite A › fails with long message"].error == "Error: first line of failure"
    long = _report(_spec("t", [{"status": "failed", "errors": [{"message": "x" * 1000}]}]))
    assert len(_run(long).results[0].error or "") == 300


def test_attachment_names_are_relative_to_e2e_out():
    run = parse_report(FIXTURE.read_bytes(), output_tail="")
    assert _by_title(run)["suite A › fails with long message"].attachments == ["x-chromium/test-failed-1.png"]
    assert _by_title(run)["suite A › passes"].attachments == []


def test_foreign_attachments_are_dropped():
    atts = [
        {"contentType": "image/png", "path": "/etc/shot.png"},
        {"contentType": "image/png", "path": "/tmp/e2e-out/../x.png"},
        {"contentType": "text/plain", "path": "/tmp/e2e-out/a.txt"},
        {"contentType": "image/png", "path": 5},
        {"contentType": "image/png", "path": "/tmp/e2e-out/ok/a.png"},
    ]
    run = _run(_report(_spec("t", [{"status": "failed", "attachments": atts}])))
    assert run.results[0].attachments == ["ok/a.png"]


def test_missing_report_is_a_boot_failure():
    run = parse_report(None, output_tail="x\nError: Timed out waiting 120000ms from config.webServer.\n")
    assert run.booted is False and run.problem and "webServer" in run.problem
    assert parse_report(None, output_tail="").problem == "no report"


def test_truncated_json_is_a_boot_failure():
    run = parse_report(b'{"suites": [', output_tail="")
    assert run.booted is False and run.problem


def test_non_object_or_no_suites_is_a_boot_failure():
    for body in (b"[]", b'"x"', b"{}", b'{"suites": 3}', b"\xff\xfe"):
        run = parse_report(body, output_tail="")
        assert run.booted is False and run.problem


def test_unknown_status_is_failed_not_passed():
    r = _report(_spec("t", [{"status": "weird"}]), _spec("u", [], status="weird"))
    assert [x.status for x in _run(r).results] == ["failed", "failed"]


def test_unsafe_files_are_dropped():
    good = _spec("good", [{"status": "passed"}])
    bad = [_spec(f"b{i}", [{"status": "passed"}], ) for i in range(3)]
    bad[0]["file"] = "/abs/x.spec.ts"
    bad[1]["file"] = "../x.spec.ts"
    bad[2]["file"] = "a/../../x.spec.ts"
    run = _run(_report(good, *bad))
    assert [r.title for r in run.results] == ["good"]
    assert run.problem and "3" in run.problem


def test_file_falls_back_to_suite_file():
    spec = {"title": "t", "tests": [{"results": [{"status": "passed"}]}]}
    assert _run(_report(spec)).results[0].file == "a.spec.ts"


def test_result_cap_stops_with_a_note():
    specs = [_spec(f"t{i}", [{"status": "passed"}]) for i in range(5001)]
    run = _run(_report(*specs))
    assert len(run.results) == 5000 and run.problem and "5000" in run.problem


def test_title_is_truncated():
    run = _run(_report(_spec("y" * 1000, [{"status": "passed"}])))
    assert len(run.results[0].title) == 300


def test_deep_nesting_does_not_raise():
    body = b'{"suites":' + b'[{"suites":' * 100_000 + b"[]" + b"}]" * 100_000 + b"}"
    assert isinstance(parse_report(body, output_tail=""), E2eRun)


def test_deep_suites_within_json_limits_are_walked_iteratively():
    suite: dict = {"title": "leaf", "specs": [_spec("t", [{"status": "passed"}])]}
    for _ in range(400):
        suite = {"title": "s", "suites": [suite]}
    run = _run({"suites": [suite]})
    assert run.booted


def test_garbage_types_never_raise():
    junk = [None, 1, "s", [], {}, [None], {"suites": [None, 1, {"specs": [None, {"tests": [None, {"results": [1]}]}]}]}]
    for j in junk:
        assert isinstance(_run(j), E2eRun)
    weird = {"suites": [{"title": 1, "file": 2, "specs": [{"title": None, "tests": [{"results": [{"status": [], "errors": 3, "attachments": [None, 1]}]}]}]}]}
    assert isinstance(_run(weird), E2eRun)
