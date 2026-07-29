from issue_triage.repro_grade import grade_repro


def test_full_repro_scores_high():
    body = ("Steps:\n1. open app\n2. click save\nExpected: saved\nActual: crash\n"
            "Environment: macOS 14, v1.2.3\nStack:\nTypeError at foo.ts:10")
    g = grade_repro(body)
    assert g["grade"] in ("A", "B")
    assert g["has_steps"] and g["has_expected_actual"] and g["has_env"]


def test_empty_body_scores_f():
    assert grade_repro("it doesn't work")["grade"] == "F"
