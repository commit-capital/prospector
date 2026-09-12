"""Executable contracts for the prompts refactored into product layers."""

from __future__ import annotations

from pipeline import analyze_driver
from pipeline import settings
from pipeline import verify_driver
from pipeline import verify_pr
from pipeline.evals import contracts as prompt_contracts
from prospector_app.backend import chat
from prospector_app.backend import claude_backend
from prospector_app.backend import codex_backend

CHAT_BEHAVIOR_TEMPLATE = (
    "Trust",
    "Ground truth for actions",
    "Verify app capabilities in source",
    "Where the data lives (read it with `store-read`)",
    "Proposed vs. activity-recorded — two different records, never conflate them",
    "Vocabulary (the operator will use these terms)",
    "How merge-readiness is decided",
    "Forming an independent opinion (do this before agreeing with the algorithm)",
    "Filing issues",
    "Making changes upstream (as {bot})",
    "Resubmitting a PR (as the confirming operator, NOT the bot)",
    "Updating a stale PR's branch (as the operator, NOT the bot)",
    'Adopting a PR ("adopt")',
    "Refreshing a PR after it moves (`reingest`)",
    "Fixing a mis-grouped cluster",
    "Live / cross-PR data",
    "Remembering what you learn",
    "Visible app context",
)


def _chat_behavior() -> tuple[str, ...]:
    return tuple(
        heading.replace("{bot}", settings.bot_login())
        for heading in CHAT_BEHAVIOR_TEMPLATE
    )


def _contract(
    name: str,
    *,
    background: tuple[str, ...] = (),
    behavior: tuple[str, ...] = (),
    output: tuple[str, ...] = (),
    placeholders: tuple[tuple[str, int], ...] = (),
    fields: tuple[str, ...] = (),
) -> prompt_contracts.PromptContract:
    return prompt_contracts.PromptContract(
        name=name,
        subsections=(
            ("Background", background),
            ("Behavior", behavior),
            ("Output", output),
        ),
        placeholder_counts=placeholders,
        output_fields=frozenset(fields),
    )


def test_chat_manual_contract() -> None:
    contract = _contract("chat", background=("Role",), behavior=_chat_behavior())
    prompt_contracts.assert_prompt_contract(contract, chat.system_prompt())


def test_codex_chat_manual_keeps_provider_context_in_behavior() -> None:
    contract = _contract(
        "codex-chat",
        background=("Role",),
        behavior=(*_chat_behavior(), "Codex cockpit"),
    )
    prompt_contracts.assert_prompt_contract(
        contract,
        codex_backend._with_codex_context(chat.system_prompt()),
    )


def test_classifier_contract() -> None:
    contract = _contract("classifier")
    prompt_contracts.assert_prompt_contract(
        contract, claude_backend.CLASSIFIER_SYSTEM_PROMPT
    )


def test_analyze_contract() -> None:
    contract = _contract(
        "analyze",
        background=("Input",),
        behavior=(
            "Trust",
            "Per-PR decisions",
            "Close safeguards",
            "Evidence standards",
            "Risk review",
            "Cluster outcome",
            "Selection",
        ),
        placeholders=(("__BUNDLE_PATH__", 1), ("__BRANCH__", 6)),
        fields=(
            "cluster_id", "outcome", "rationale", "prs", "pr", "head_sha",
            "disposition", "canonical", "upstream_pr", "upstream_date", "asks",
        ),
    )
    prompt_contracts.assert_prompt_contract(
        contract, analyze_driver.ANALYZE_PROMPT + analyze_driver.ANALYZE_FENCED_TAIL
    )


def test_verification_contracts() -> None:
    contracts = (
        (
            _contract(
                "blind",
                background=("Inputs",),
                behavior=(
                    "Trust", "Adequacy decision", "Test execution",
                    "Independent reproduction", "Decision fields",
                ),
                placeholders=(
                    ("__PR__", 1), ("__TITLE__", 1), ("__DIFF_PATH__", 1),
                    ("__BASE_CLONE__", 2), ("__LINKED_ISSUES__", 1),
                ),
                fields=(
                    "faithful", "confidence", "claimed_symptom",
                    "expected_red_signature", "repro_command",
                    "expected_repro_signature", "from_linked_issue",
                    "requires_live_agent", "reasoning",
                ),
            ),
            verify_driver.BLIND_PROMPT + verify_pr.BLIND_FENCED_TAIL,
        ),
        (
            _contract(
                "author",
                background=("Inputs",),
                behavior=("Trust", "Authored test rules", "Decision fields"),
                placeholders=(
                    ("__PR__", 1), ("__TITLE__", 1), ("__DIFF_PATH__", 1),
                    ("__BASE_CLONE__", 1), ("__LINKED_ISSUES__", 1),
                ),
                fields=(
                    "can_author", "files", "path", "contents",
                    "expected_red_signature", "confidence", "reasoning",
                ),
            ),
            verify_driver.AUTHOR_PROMPT + verify_pr.AUTHOR_FENCED_TAIL,
        ),
        (
            _contract(
                "judge",
                background=("Inputs",),
                behavior=("Trust", "Match decisions", "Confidence and findings"),
                placeholders=(
                    ("__PR__", 1), ("__EXPECTED_RED__", 1),
                    ("__EXPECTED_REPRO__", 3), ("__EVIDENCE__", 1),
                ),
                fields=("red_reason_match", "repro_reason_match", "findings"),
            ),
            verify_driver.JUDGE_PROMPT + verify_pr.JUDGE_FENCED_TAIL,
        ),
    )
    for contract, prompt in contracts:
        prompt_contracts.assert_prompt_contract(contract, prompt)


def test_contract_reports_layer_and_placeholder_drift() -> None:
    contract = _contract(
        "example",
        placeholders=(("__ITEM__", 1),),
        fields=("answer",),
    )
    broken = "# Behavior\nDo __ITEM__ and __ITEM__.\n# Background\nContext.\n# Output\n{}"
    errors = contract.violations(broken)
    assert any("top-level sections" in error for error in errors)
    assert any("occurs 2 times" in error for error in errors)
    assert any("does not name field answer" in error for error in errors)
