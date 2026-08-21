"""pr_search.coerce(): raw model JSON → a safe filter spec. Never raises; drops
unknown keys, clamps enums/ops, so a hallucinated field can't reach the engine."""
from prospector_app.backend import pr_search


def test_coerce_drops_greptile_fields_when_no_provider(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    spec = pr_search.coerce({"greptile": {"op": "<", "value": 5}, "greptile_stale": True,
                             "greptile_severity": "defects", "ci": "passing"})
    assert "greptile" not in spec
    assert "greptile_stale" not in spec
    assert "greptile_severity" not in spec
    assert spec["ci"] == "passing"


def test_review_field_docs_follow_provider(monkeypatch):
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "greptile")
    assert "greptile_severity" in pr_search._review_field_docs()
    monkeypatch.setenv("TRIAGE_REVIEW_PROVIDER", "none")
    assert pr_search._review_field_docs() == ""


def test_passes_known_fields():
    spec = pr_search.coerce({"disposition": "needs-human", "safety": "not-run",
                             "greptile": {"op": "<", "value": 3}, "merge_ok": True,
                             "has_summary": True, "has_issues": False})
    assert spec["disposition"] == "needs-human"
    assert spec["safety"] == "not-run"
    assert spec["greptile"] == {"op": "<", "value": 3}
    assert spec["merge_ok"] is True
    assert spec["has_summary"] is True
    assert spec["has_issues"] is False


def test_drops_unknown_keys():
    assert "nonsense" not in pr_search.coerce({"nonsense": 1, "cluster": 4})


def test_rejects_bad_enum_and_op():
    assert "safety" not in pr_search.coerce({"safety": "PURPLE"})
    assert "greptile" not in pr_search.coerce({"greptile": {"op": "≈", "value": 3}})
    assert "disposition" not in pr_search.coerce({"disposition": "merge-it"})
    assert "greptile_severity" not in pr_search.coerce({"greptile_severity": "bugs"})


def test_coerces_greptile_severity():
    assert pr_search.coerce({"greptile_severity": "defects"})["greptile_severity"] == "defects"


def test_numeric_value_must_be_number():
    assert "score" not in pr_search.coerce({"score": {"op": "<", "value": "low"}})


def test_coerces_paths_string():
    assert pr_search.coerce({"paths": "  src/auth  "})["paths"] == "src/auth"
    assert "paths" not in pr_search.coerce({"paths": ""})
    assert "paths" not in pr_search.coerce({"paths": 123})


def test_coerces_valid_loc():
    loc = {"metric": "both", "scope": "effective", "op": ">", "value": 500}
    assert pr_search.coerce({"loc": loc})["loc"] == loc


def test_rejects_bad_loc():
    bad = [
        {"metric": "x", "scope": "effective", "op": ">", "value": 5},     # bad metric
        {"metric": "both", "scope": "non_test", "op": ">", "value": 5},   # scope no longer valid
        {"metric": "both", "scope": "all", "op": ">=", "value": 5},       # op must be </>
        {"metric": "both", "scope": "all", "op": ">", "value": "big"},    # value not a number
        "nope",                                                            # not a dict
    ]
    for b in bad:
        assert "loc" not in pr_search.coerce({"loc": b})


def test_non_dict_input_is_empty_spec():
    assert pr_search.coerce("not json") == {}
    assert pr_search.coerce(None) == {}


def test_extract_json_from_model_text():
    raw = 'Sure!\n```json\n{"cluster": 9}\n```\n'
    assert pr_search.extract_spec(raw) == {"cluster": 9}
    assert pr_search.extract_spec("garbage") == {}


def test_search_route(monkeypatch):
    from fastapi.testclient import TestClient
    from prospector_app.backend import app as appmod

    async def fake(query):
        assert "leaf" in query
        return {"risk_tier": 3, "greptile": {"op": "<", "value": 3}}
    monkeypatch.setattr(appmod.pr_search, "search_to_spec", fake)
    c = TestClient(appmod.app)
    r = c.post("/api/prs/search", json={"query": "leaf PRs greptile under 3"})
    assert r.status_code == 200
    assert r.json()["spec"]["risk_tier"] == 3
