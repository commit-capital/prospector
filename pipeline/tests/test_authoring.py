"""The contributor docs an authoring agent is handed, and the section they make
in its prompt."""
from __future__ import annotations

import json

from pipeline import authoring, gh, profile


def _profile(tmp_path, monkeypatch, docs: list[str]) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 1, "authoring": {"contributor_docs": docs}}))
    monkeypatch.setenv("TRIAGE_PROFILE", str(path))


def test_the_tree_s_docs_are_read_in_profile_order_skipping_missing_ones(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, ["CONTRIBUTING.md", "docs/STYLE.md", "AGENTS.md"])
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "AGENTS.md").write_text("agents\n")
    (tree / "CONTRIBUTING.md").write_text("contributing\n")
    assert authoring.docs_from_tree(tree) == [
        authoring.Doc("CONTRIBUTING.md", "contributing"), authoring.Doc("AGENTS.md", "agents")]


def test_a_doc_linked_to_one_already_read_is_read_once(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "AGENTS.md").write_text("one set of rules\n")
    (tree / "CLAUDE.md").symlink_to("AGENTS.md")
    assert authoring.docs_from_tree(tree) == [authoring.Doc("AGENTS.md", "one set of rules")]


def test_a_doc_resolving_outside_the_tree_is_left_out(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tmp_path / "outside.md").write_text("not the repository's\n")
    (tree / "AGENTS.md").symlink_to(tmp_path / "outside.md")
    assert authoring.docs_from_tree(tree) == []


def test_an_empty_doc_is_left_out(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "AGENTS.md").write_text("  \n")
    assert authoring.docs_from_tree(tree) == []


def test_upstream_docs_are_read_from_the_default_branch(monkeypatch):
    asked: list[str] = []

    def fake_file(path: str) -> str | None:
        asked.append(path)
        return "be small\n" if path == "CONTRIBUTING.md" else None

    monkeypatch.setattr(gh, "default_branch_file", fake_file)
    assert authoring.docs_from_upstream() == [authoring.Doc("CONTRIBUTING.md", "be small")]
    assert asked == list(profile.DEFAULT_CONTRIBUTOR_DOCS)


def test_no_docs_make_no_section():
    assert authoring.docs_block([]) == ""


def test_each_doc_is_labelled_with_its_path():
    block = authoring.docs_block([authoring.Doc("AGENTS.md", "a"),
                                  authoring.Doc("CONTRIBUTING.md", "b")])
    assert '<doc path="AGENTS.md">\na\n</doc>\n\n<doc path="CONTRIBUTING.md">\nb\n</doc>' in block
    assert "this prompt wins" in block
    assert block.endswith("\n\n")


def test_docs_past_the_budget_are_cut(monkeypatch):
    monkeypatch.setattr(authoring, "MAX_CHARS", 10)
    block = authoring.docs_block([authoring.Doc("AGENTS.md", "x" * 8),
                                  authoring.Doc("CONTRIBUTING.md", "y" * 8),
                                  authoring.Doc("DESIGN.md", "z")])
    assert "x" * 8 in block
    assert "y" * 2 + "\n[... the rest of this file is omitted ...]" in block
    assert "y" * 3 not in block
    assert "DESIGN.md" not in block


def test_without_a_lint_command_there_is_no_lint_note():
    assert authoring.lint_note("/bin/check") == ""


def test_a_lint_command_offers_the_agent_the_lint_lane(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"version": 1, "verify": {"lint_cmd": "pnpm lint"}}))
    monkeypatch.setenv("TRIAGE_PROFILE", str(path))
    note = authoring.lint_note("/bin/check")
    assert note.startswith("`/bin/check lint` runs the repository's lint")
    assert "refuses a change" in note
