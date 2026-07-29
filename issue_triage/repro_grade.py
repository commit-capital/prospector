"""Heuristic reproduction-quality grade A-F.

This is the contract a future auto-reproduction agent consumes: an A/B issue has
enough structure (steps, expected-vs-actual, env, trace) to attempt an automated
repro. D/F issues get a 'request-repro' suggested action instead.
"""
import re


def grade_repro(body):
    b = body.lower()
    has_steps = bool(re.search(r"(steps?:|^\s*1\.|reproduc)", b, re.M))
    has_expected_actual = ("expected" in b and "actual" in b)
    has_env = bool(re.search(r"(environment|version|os:|v\d+\.\d+|macos|windows|linux)", b))
    has_trace = bool(re.search(r"(error|exception|stack|traceback|\.ts:\d+|\.py:\d+)", b))
    score = sum([has_steps, has_expected_actual, has_env, has_trace])
    grade = {4: "A", 3: "B", 2: "C", 1: "D", 0: "F"}[score]
    return {"grade": grade, "score": score, "has_steps": has_steps,
            "has_expected_actual": has_expected_actual, "has_env": has_env,
            "has_trace": has_trace}
