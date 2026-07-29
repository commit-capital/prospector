from issue_triage.summarize_issues import classify_subsystem, extract_identifiers


def test_classify_execution_lock():
    assert classify_subsystem("Issue stuck in_progress",
                              "stale lock not cleared") == "execution-locks"


def test_classify_default_other():
    assert classify_subsystem("Typo in readme", "fix spelling") == "other"


def test_extract_identifiers_finds_camelcase_and_errors():
    ids = extract_identifiers("TypeError: executionRunId is null in issueService.update()")
    assert "executionRunId" in ids
    assert any("TypeError" in i for i in ids)
