from pipeline import ci_signal


def _run(app, conclusion, status="completed", name="x"):
    return {"app": app, "name": name, "status": status, "conclusion": conclusion,
            "title": None, "summary": None, "url": None}


def test_reviewer_apps_are_excluded():
    runs = [_run("github-actions", "success"), _run("greptile-apps", "failure"),
            _run("superagent-security", "action_required")]
    assert ci_signal.verdict(runs, []) == "passing"


def test_failure_outranks_pending_and_statuses_count():
    assert ci_signal.verdict([_run("github-actions", None, "in_progress")],
                             [{"context": "c", "state": "failure"}]) == "failing"
    assert ci_signal.verdict([_run("github-actions", None, "in_progress")], []) == "pending"
    assert ci_signal.verdict([], []) is None
    assert ci_signal.verdict([_run("greptile-apps", "failure")], []) is None


def test_from_graphql_contexts():
    nodes = [{"__typename": "CheckRun", "name": "Build", "status": "COMPLETED", "conclusion": "SUCCESS",
              "title": "ok", "summary": "s", "detailsUrl": "d", "url": "u",
              "checkSuite": {"app": {"slug": "github-actions"}}},
             {"__typename": "StatusContext", "context": "lint", "state": "PENDING"}]
    runs, statuses = ci_signal.from_graphql_contexts(nodes)
    assert runs == [{"app": "github-actions", "name": "Build", "status": "completed",
                     "conclusion": "success", "title": "ok", "summary": "s", "url": "u"}]
    assert statuses == [{"context": "lint", "state": "pending"}]


def test_from_rest_check_runs():
    rest = [{"app": {"slug": "socket-security"}, "name": "n", "status": "completed", "conclusion": "neutral",
             "output": {"title": "t", "summary": "s"}, "html_url": "h"}]
    assert ci_signal.from_rest_check_runs(rest) == [
        {"app": "socket-security", "name": "n", "status": "completed", "conclusion": "neutral",
         "title": "t", "summary": "s", "url": "h"}]


def test_required_context_nothing_reported_is_unreported():
    old_names = [_run("github-actions", "success", name="verify"),
                 _run("github-actions", "success", name="e2e")]
    required = ["ci / verify", "ci / e2e"]
    assert ci_signal.unreported(old_names, [], required) == required
    assert ci_signal.verdict(old_names, [], required=required) == "unreported"
    assert ci_signal.verdict([], [], required=required) == "unreported"
    assert ci_signal.verdict(old_names, [], required=None) == "unknown"
    assert ci_signal.verdict(old_names + [_run("github-actions", "failure")], [],
                             required=None) == "failing"
    assert ci_signal.verdict(old_names + [_run("github-actions", None, "queued")], [],
                             required=required) == "pending"
    assert ci_signal.verdict(old_names + [_run("github-actions", "failure")], [],
                             required=required) == "failing"


def test_required_context_reported_by_run_status_or_reviewer_counts():
    runs = [_run("github-actions", "success", name="ci / verify"),
            _run("greptile-apps", "success", name="Greptile Review")]
    statuses = [{"context": "ci / e2e", "state": "success"}]
    assert ci_signal.verdict(runs, statuses,
                             required=["ci / verify", "ci / e2e", "Greptile Review"]) == "passing"


def test_required_contexts_merges_protection_and_rulesets():
    ref = {"branchProtectionRule": {"requiredStatusCheckContexts": ["lint", "build"]},
           "rules": {"nodes": [
               {"parameters": {}},
               {"parameters": None},
               {"parameters": {"requiredStatusChecks": [{"context": "build"},
                                                        {"context": "ci / e2e"}]}}]}}
    assert ci_signal.required_contexts(ref) == ["lint", "build", "ci / e2e"]
    assert ci_signal.required_contexts({}) == []
