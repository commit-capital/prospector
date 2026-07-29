"""Local (no-API) subsystem + identifier tagging for issues.

Subsystem classification is the ONE shared accessor in pipeline/taxonomy.py
(vocabulary from the active repository profile), so issue clusters line up
with the PR subsystem vocabulary (important for issue<->PR linking). Do not
re-define it here.
"""
import re

from pipeline.taxonomy import classify as classify_subsystem  # noqa: F401  (re-exported)


def extract_identifiers(text):
    camel = re.findall(r"\b[a-z]+[A-Z][A-Za-z]+\b", text)
    errors = re.findall(r"\b\w*(?:Error|Exception)\b[^\n.]{0,40}", text)
    # Constants must contain an underscore (e.g. ANTHROPIC_API_KEY, ACP_OUTPUT_EVENT).
    # Bare ALL-CAPS words (POST, HTTP, JSON, NULL, WHERE, ALTER) are generic and
    # would over-merge clusters, so we don't treat them as discriminative identifiers.
    consts = re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text)
    return sorted(set(camel + errors + consts))


def summarize(rec):
    return {
        "number": rec["number"],
        "subsystem": classify_subsystem(rec["title"], rec["body"]),
        "identifiers": extract_identifiers(rec["title"] + " " + rec["body"]),
    }
