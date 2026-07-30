"""The app's FastAPI-boundary models: the discriminated accept union, the
close-action payload, and the per-endpoint request bodies."""
import pydantic

from prospector_app.backend import models


def test_suggest_accept_discriminates_on_kind():
    ta = pydantic.TypeAdapter(models.SuggestAccept)
    assert isinstance(ta.validate_python({"kind": "merge"}), models.MergeAccept)
    assert isinstance(
        ta.validate_python({"kind": "close", "action": "CLOSE_DUP", "canonical": 9}),
        models.CloseAccept)
    assert isinstance(
        ta.validate_python({"kind": "review", "event": "request-changes", "body": "fix"}),
        models.ReviewAccept)


def test_close_action_defaults_optional_refs_to_none():
    a = models.CloseAction(action="CLOSE_DUP")
    assert a.canonical is None and a.tags is None and a.override_action is None


def test_close_action_keeps_supplied_refs():
    a = models.CloseAction(action="CLOSE_FIXED", upstream_pr=4416, comment="hi")
    assert a.upstream_pr == 4416 and a.comment == "hi"


def test_bulk_comments_coerces_str_keys_to_int():
    # JSON object keys arrive as strings; the model coerces them back to int.
    b = models.BulkExecuteBody.model_validate({"prs": [1], "comments": {"1": "hi", "2": "yo"}})
    assert b.comments == {1: "hi", 2: "yo"}


def test_cluster_body_parses_items():
    body = models.ClusterExecuteBody.model_validate(
        {"items": [{"pr": 5, "action": "merge"}], "dry_run": False})
    assert body.items[0].pr == 5 and body.items[0].action == "merge"
    assert body.dry_run is False
