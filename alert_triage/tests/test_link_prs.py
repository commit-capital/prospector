"""Deterministic alert→PR candidate matching: bump-title parsing, version
compare, diff overlap, text references, and the link cap."""
from alert_triage import link_prs


def _pr(number: int, title: str = "", body: str = "", state: str = "open",
        head_sha: str = "") -> dict:
    return {"number": number, "title": title, "body": body, "state": state,
            "head_sha": head_sha}


def test_parse_bump_variants():
    assert link_prs.parse_bump("Bump lodash from 4.17.20 to 4.17.30") == ("lodash", "4.17.30")
    assert link_prs.parse_bump("chore(deps): bump foo-bar from 1.2.3 to 2.0.0") == ("foo-bar", "2.0.0")
    assert link_prs.parse_bump("Update readme") is None


def test_version_gte():
    assert link_prs.version_gte("2.0.0", "1.9.9") is True
    assert link_prs.version_gte("4.17.30", "4.17.30") is True
    assert link_prs.version_gte("4.17.29", "4.17.30") is False
    assert link_prs.version_gte("abc", "1.0") is None


def test_dependabot_manifest_bump_match():
    meta = {"source": "dependabot", "package": "lodash",
            "manifest_path": "web/package.json", "ghsa_id": "GHSA-1111-2222-3333"}
    prs = [_pr(10, "Bump lodash from 4.17.20 to 4.17.30", state="merged"),
           _pr(11, "Bump react from 18 to 19")]
    out = link_prs.candidates_for(meta, prs, {})
    assert [(c["number"], c["how"], c["state"]) for c in out] == [(10, "manifest-bump", "merged")]


def test_code_scanning_diff_overlap():
    meta = {"source": "code-scanning", "path": "src/server/render.ts",
            "rule_id": "js/reflected-xss"}
    prs = [_pr(20, "Escape output", head_sha="aaa", state="open"),
           _pr(21, "Unrelated", head_sha="bbb")]
    diffs = {"aaa": "--- a/src/server/render.ts\n+++ b/src/server/render.ts\n+escape()",
             "bbb": "+++ b/other.ts\n"}
    out = link_prs.candidates_for(meta, prs, diffs)
    assert [(c["number"], c["how"]) for c in out] == [(20, "diff-overlap")]


def test_text_ref_matches_identifiers_and_caps():
    meta = {"source": "dependabot", "package": "lodash", "ghsa_id": "GHSA-aaaa-bbbb-cccc",
            "cve_id": "CVE-2026-1234"}
    prs = [_pr(n, f"Mention lodash fix {n}") for n in range(30, 45)]
    prs.append(_pr(50, "Fixes CVE-2026-1234", state="merged"))
    out = link_prs.candidates_for(meta, prs, {})
    assert len(out) == link_prs.LINK_CAP
    assert all(c["how"] == "text-ref" for c in out)
    assert 50 in [c["number"] for c in out]  # merged text-refs sort first


def test_short_identifiers_do_not_text_match():
    meta = {"source": "code-scanning", "rule_id": "xss", "path": None}
    prs = [_pr(60, "Fix xss everywhere")]
    assert link_prs.candidates_for(meta, prs, {}) == []


def test_secret_type_text_match():
    meta = {"source": "secret-scanning", "secret_type": "github_personal_access_token"}
    prs = [_pr(70, "Remove github_personal_access_token from config", state="merged")]
    out = link_prs.candidates_for(meta, prs, {})
    assert [(c["number"], c["how"]) for c in out] == [(70, "text-ref")]
