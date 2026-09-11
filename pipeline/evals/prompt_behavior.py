"""Run and score versioned behavioral cases for the core decision prompts."""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import tempfile
from typing import Literal

from pipeline import analyze_driver
from pipeline import headless_agent
from pipeline import settings
from pipeline import verify_driver
from pipeline import verify_pr
from pipeline.evals import datasets

EvalKind = Literal["analyze", "blind"]
DEFAULT_GOLDEN = Path(__file__).resolve().parent / "data" / "prompt_behavior.jsonl"
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class EvalCase:
    id: str
    kind: EvalKind
    inputs: dict[str, object]
    expected: dict[str, object]


@dataclass(frozen=True)
class CaseScore:
    id: str
    passed_checks: int
    total_checks: int
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclass(frozen=True)
class EvalScore:
    cases: tuple[CaseScore, ...]

    @property
    def passed(self) -> bool:
        return all(case.passed for case in self.cases)

    def to_dict(self) -> dict[str, object]:
        checks = sum(case.total_checks for case in self.cases)
        passed_checks = sum(case.passed_checks for case in self.cases)
        return {
            "passed": self.passed,
            "cases": len(self.cases),
            "cases_passed": sum(case.passed for case in self.cases),
            "checks": checks,
            "checks_passed": passed_checks,
            "score": passed_checks / checks if checks else 1.0,
            "failures": {
                case.id: list(case.failures) for case in self.cases if case.failures
            },
        }


def load_cases(path: Path = DEFAULT_GOLDEN) -> list[EvalCase]:
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for row in datasets.read_jsonl(path):
        case_id = row.get("id")
        kind = row.get("kind")
        inputs = row.get("input")
        expected = row.get("expected")
        if not isinstance(case_id, str) or _CASE_ID.fullmatch(case_id) is None:
            raise ValueError("every eval case needs a filesystem-safe lowercase id")
        if case_id in seen:
            raise ValueError(f"duplicate eval case id: {case_id}")
        if kind not in ("analyze", "blind"):
            raise ValueError(f"{case_id}: unsupported kind {kind!r}")
        if not isinstance(inputs, dict) or not isinstance(expected, dict):
            raise ValueError(f"{case_id}: input and expected must be objects")
        seen.add(case_id)
        cases.append(EvalCase(case_id, kind, inputs, expected))
    if not cases:
        raise ValueError(f"no eval cases in {path}")
    return cases


def load_predictions(path: Path) -> dict[str, dict[str, object]]:
    predictions: dict[str, dict[str, object]] = {}
    for row in datasets.read_jsonl(path):
        case_id = row.get("id")
        output = row.get("output")
        if not isinstance(case_id, str) or not isinstance(output, dict):
            raise ValueError("prediction rows require string id and object output")
        if case_id in predictions:
            raise ValueError(f"duplicate prediction id: {case_id}")
        predictions[case_id] = output
    return predictions


def _score_analyze(case: EvalCase, output: dict[str, object]) -> CaseScore:
    failures: list[str] = []
    total = passed = 0

    def check(ok: bool, message: str) -> None:
        nonlocal total, passed
        total += 1
        if ok:
            passed += 1
        else:
            failures.append(message)

    outcome = case.expected.get("outcome")
    if isinstance(outcome, str):
        check(output.get("outcome") == outcome,
              f"outcome: expected {outcome!r}, got {output.get('outcome')!r}")
    outcome_one_of = case.expected.get("outcome_one_of")
    if isinstance(outcome_one_of, list):
        check(output.get("outcome") in outcome_one_of,
              f"outcome: expected one of {outcome_one_of!r}, got {output.get('outcome')!r}")

    bundle = case.inputs.get("bundle")
    cluster = bundle.get("cluster") if isinstance(bundle, dict) else None
    cluster_id = cluster.get("id") if isinstance(cluster, dict) else None
    check(output.get("cluster_id") == cluster_id,
          f"cluster_id: expected {cluster_id!r}, got {output.get('cluster_id')!r}")

    raw_rows = output.get("prs")
    rows = {
        str(row["pr"]): row
        for row in raw_rows if isinstance(row, dict) and "pr" in row
    } if isinstance(raw_rows, list) else {}
    expected_rows = case.expected.get("prs")
    if not isinstance(expected_rows, dict):
        check(False, "golden case has no prs expectations")
        return CaseScore(case.id, passed, total, tuple(failures))
    check(set(rows) == {str(pr) for pr in expected_rows},
          f"PR rows: expected {sorted(str(pr) for pr in expected_rows)!r}, "
          f"got {sorted(rows)!r}")
    for pr, raw_expected in expected_rows.items():
        row = rows.get(str(pr))
        check(row is not None, f"PR {pr}: missing output row")
        if row is None or not isinstance(raw_expected, dict):
            continue
        for field in ("disposition", "canonical"):
            if field in raw_expected:
                check(row.get(field) == raw_expected[field],
                      f"PR {pr} {field}: expected {raw_expected[field]!r}, "
                      f"got {row.get(field)!r}")
        asks_contain = raw_expected.get("asks_contain")
        if isinstance(asks_contain, list):
            asks = " ".join(str(value) for value in row.get("asks", [])).lower()
            for needle in asks_contain:
                if isinstance(needle, str):
                    check(needle.lower() in asks,
                          f"PR {pr} asks do not contain {needle!r}")
    return CaseScore(case.id, passed, total, tuple(failures))


def _score_blind(case: EvalCase, output: dict[str, object]) -> CaseScore:
    failures: list[str] = []
    total = passed = 0
    for field, expected in case.expected.items():
        total += 1
        if field.endswith("_present"):
            actual_field = field.removesuffix("_present")
            actual = output.get(actual_field)
            present = actual is not None and actual != ""
            ok = present == (expected is True)
        elif field.endswith("_contains") and isinstance(expected, str):
            actual_field = field.removesuffix("_contains")
            ok = expected.lower() in str(output.get(actual_field, "")).lower()
        else:
            actual_field = field
            ok = output.get(field) == expected
        if ok:
            passed += 1
        else:
            failures.append(
                f"{actual_field}: expectation {expected!r} failed; "
                f"got {output.get(actual_field)!r}"
            )
    return CaseScore(case.id, passed, total, tuple(failures))


def score(
    cases: list[EvalCase], predictions: dict[str, dict[str, object]]
) -> EvalScore:
    results: list[CaseScore] = []
    for case in cases:
        output = predictions.get(case.id)
        if output is None:
            results.append(CaseScore(case.id, 0, 1, ("missing prediction",)))
        elif case.kind == "analyze":
            results.append(_score_analyze(case, output))
        else:
            results.append(_score_blind(case, output))
    return EvalScore(tuple(results))


def _safe_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if path != root.resolve() and root.resolve() not in path.parents:
        raise ValueError(f"eval fixture path escapes its case directory: {relative}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _required_mapping(inputs: dict[str, object], key: str) -> dict[str, object]:
    value = inputs.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"eval input {key} must be an object")
    return value


def _required_string(inputs: dict[str, object], key: str) -> str:
    value = inputs.get(key)
    if not isinstance(value, str):
        raise ValueError(f"eval input {key} must be a string")
    return value


def render_case(case: EvalCase, case_dir: Path) -> str:
    case_dir.mkdir(parents=True, exist_ok=True)
    if case.kind == "analyze":
        bundle = copy.deepcopy(_required_mapping(case.inputs, "bundle"))
        members = bundle.get("members")
        diffs = _required_mapping(case.inputs, "diffs")
        if not isinstance(members, list):
            raise ValueError(f"{case.id}: bundle.members must be an array")
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get("pr"), int):
                raise ValueError(f"{case.id}: each bundle member needs an integer pr")
            pr = str(member["pr"])
            diff = diffs.get(pr)
            if not isinstance(diff, str):
                raise ValueError(f"{case.id}: missing diff for PR {pr}")
            diff_path = _safe_path(case_dir, f"pr-{pr}.diff")
            diff_path.write_text(diff)
            member["diff_path"] = str(diff_path)
        bundle_path = _safe_path(case_dir, "bundle.json")
        bundle_path.write_text(json.dumps(bundle, indent=2))
        return headless_agent.fill(
            analyze_driver.ANALYZE_PROMPT + analyze_driver.ANALYZE_FENCED_TAIL,
            {
                "__BUNDLE_PATH__": str(bundle_path),
                "__BRANCH__": settings.default_branch(),
            },
        )

    base_dir = _safe_path(case_dir, "base")
    base_dir.mkdir(exist_ok=True)
    for relative, contents in _required_mapping(case.inputs, "base_files").items():
        if not isinstance(contents, str):
            raise ValueError(f"{case.id}: base file {relative} must be text")
        _safe_path(base_dir, relative).write_text(contents)
    diff_path = _safe_path(case_dir, "pr.diff")
    diff_path.write_text(_required_string(case.inputs, "diff"))
    pr = case.inputs.get("pr")
    if not isinstance(pr, int):
        raise ValueError(f"{case.id}: pr must be an integer")
    linked_issues = case.inputs.get("linked_issues")
    if not isinstance(linked_issues, list):
        raise ValueError(f"{case.id}: linked_issues must be an array")
    return headless_agent.fill(
        verify_driver.BLIND_PROMPT + verify_pr.BLIND_FENCED_TAIL,
        {
            "__PR__": pr,
            "__TITLE__": _required_string(case.inputs, "title"),
            "__DIFF_PATH__": str(diff_path),
            "__BASE_CLONE__": str(base_dir),
            "__LINKED_ISSUES__": json.dumps(linked_issues, indent=2),
        },
    )


def run_live(cases: list[EvalCase], model: str | None = None) -> dict[str, dict[str, object]]:
    predictions: dict[str, dict[str, object]] = {}
    with tempfile.TemporaryDirectory(prefix="prospector-prompt-eval-") as raw_root:
        root = Path(raw_root)
        for case in cases:
            case_dir = _safe_path(root, case.id)
            prompt = render_case(case, case_dir)
            print(f"▶ {case.id}", flush=True)
            text = headless_agent.run_agent(
                prompt,
                allow_gh=False,
                cwd=str(case_dir),
                model=model,
                on_event=headless_agent.print_progress,
            )
            predictions[case.id] = headless_agent.extract_json(text)
    return predictions


def write_predictions(path: Path, predictions: dict[str, dict[str, object]]) -> None:
    lines = [
        json.dumps({"id": case_id, "output": output}, sort_keys=True)
        for case_id, output in predictions.items()
    ]
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--live", action="store_true")
    source.add_argument("--predictions", type=Path)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--model")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    cases = load_cases(args.golden)
    selected = set(args.case)
    if selected:
        unknown = selected - {case.id for case in cases}
        if unknown:
            parser.error(f"unknown case(s): {', '.join(sorted(unknown))}")
        cases = [case for case in cases if case.id in selected]
    predictions = (
        run_live(cases, model=args.model)
        if args.live else load_predictions(args.predictions)
    )
    if args.output:
        write_predictions(args.output, predictions)
    result = score(cases, predictions)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
