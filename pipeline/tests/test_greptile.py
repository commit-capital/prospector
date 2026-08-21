"""Greptile-from-GitHub scraping: parse score/SHA/review text from gh data.
gh_list is monkeypatched — no real network."""
import unittest.mock as mock

from pipeline import greptile


class TestFetchGreptileReviewData:
    def test_fetch_greptile_review_data_returns_sha_and_payloads(self):
        reviews = [{"user": {"login": "greptile-apps[bot]"}, "commit_id": "sha1", "body": "b"}]
        comments = [{"user": {"login": "greptile-apps[bot]"}, "original_commit_id": "sha1", "body": "**Bug**"}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", side_effect=[reviews, comments]):
            sha, revs, cmts = greptile.fetch_greptile_review_data(5001)
        assert sha == "sha1"
        assert revs == reviews and cmts == comments

    def test_fetch_greptile_review_data_no_greptile_review(self):
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", side_effect=[[{"user": {"login": "x"}}], []]):
            sha, revs, cmts = greptile.fetch_greptile_review_data(5001)
        assert sha is None and revs == [] and cmts == []

    def test_fetch_greptile_review_data_github_unreachable(self):
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=None):
            sha, revs, cmts = greptile.fetch_greptile_review_data(5001)
        assert sha is None and revs == [] and cmts == []

    def test_fetch_greptile_review_data_falls_back_to_comment_sha(self):
        # A COMMENTED Greptile review reports commit_id=null; the reviewed SHA is
        # recovered from its inline comment's original_commit_id.
        reviews = [{"user": {"login": "greptile-apps[bot]"}, "commit_id": None}]
        comments = [
            {"user": {"login": "greptile-apps[bot]"},
             "commit_id": "outdated9", "original_commit_id": "reviewed7", "body": "nit: spacing"},
        ]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", side_effect=[reviews, comments]):
            sha, _revs, _cmts = greptile.fetch_greptile_review_data(5001)
        assert sha == "reviewed7"

    def test_fetch_greptile_review_data_prefers_review_over_comment_sha(self):
        reviews = [{"user": {"login": "greptile-apps[bot]"}, "commit_id": "rev321"}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", side_effect=[reviews, []]):
            sha, _revs, _cmts = greptile.fetch_greptile_review_data(5001)
        assert sha == "rev321"

    def test_fetch_greptile_review_data_null_review_no_comment_sha(self):
        # Null-commit_id review and no recoverable comment SHA -> sha is None.
        reviews = [{"user": {"login": "greptile-apps[bot]"}, "commit_id": None}]
        comments = [{"user": {"login": "some-human"}, "commit_id": "x"}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", side_effect=[reviews, comments]):
            sha, _revs, _cmts = greptile.fetch_greptile_review_data(5001)
        assert sha is None


# Greptile's issue-level summary comment on a PR (the surface that holds the
# numeric Confidence Score and the authoritative "last reviewed commit" footer).
GREPTILE_SUMMARY_BODY = (
    "<h3>Greptile Summary</h3>\n\nBumps the adapter protocol constant.\n\n"
    "<h3>Confidence Score: 5/5</h3>\n\nThis looks safe to merge.\n\n"
    "<sub>Reviews (2): Last reviewed commit: "
    '["fix(openclaw-gateway): bump probe"]'
    "(https://github.com/test-owner/test-repo/commit/c1c162ed3bb68803433d212033c3102d2e706852)"
    " | Re-trigger Greptile</sub>"
)


class TestParseGreptileSummary:
    def test_extracts_score_and_sha(self):
        comments = [{"user": {"login": "greptile-apps[bot]"}, "body": GREPTILE_SUMMARY_BODY}]
        score, sha = greptile._parse_greptile_summary(comments)
        assert score == 5
        assert sha == "c1c162ed3bb68803433d212033c3102d2e706852"

    def test_empty_comments_are_none(self):
        assert greptile._parse_greptile_summary([]) == (None, None)

    def test_score_without_footer_sha(self):
        body = "<h3>Confidence Score: 3/5</h3>\n\nSome findings remain."
        comments = [{"user": {"login": "greptileai[bot]"}, "body": body}]
        assert greptile._parse_greptile_summary(comments) == (3, None)

    def test_ignores_comment_without_score(self):
        comments = [{"user": {"login": "greptile-apps[bot]"}, "body": "Re-triggering review…"}]
        assert greptile._parse_greptile_summary(comments) == (None, None)

    def test_prefers_most_recent_summary(self):
        older = dict(body="<h3>Confidence Score: 4/5</h3>\n\n<sub>Last reviewed commit: "
                     "[m](https://github.com/test-owner/test-repo/commit/aaaa111)</sub>")
        newer = dict(body="<h3>Confidence Score: 5/5</h3>\n\n<sub>Last reviewed commit: "
                     "[m](https://github.com/test-owner/test-repo/commit/bbbb222)</sub>")
        # GitHub returns issue comments chronologically; the latest wins.
        assert greptile._parse_greptile_summary([older, newer]) == (5, "bbbb222")


class TestParseConfidenceScore:
    def test_extracts_score_from_review_body(self):
        assert greptile.parse_confidence_score("Confidence Score: 4/5\nLooks solid.") == 4

    def test_none_when_body_carries_no_score(self):
        assert greptile.parse_confidence_score("Re-triggering review…") is None

    def test_none_for_empty_or_missing_body(self):
        assert greptile.parse_confidence_score("") is None
        assert greptile.parse_confidence_score(None) is None


class TestFetchGreptileSummary:
    def test_parses_issue_comment(self):
        comments = [{"user": {"login": "greptile-apps[bot]"}, "body": GREPTILE_SUMMARY_BODY}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=comments):
            assert greptile.fetch_greptile_summary(5984) == (
                5, "c1c162ed3bb68803433d212033c3102d2e706852")

    def test_filters_non_greptile_comments(self):
        comments = [{"user": {"login": "some-human"}, "body": "<h3>Confidence Score: 1/5</h3>"}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=comments):
            assert greptile.fetch_greptile_summary(5984) == (None, None)

    def test_github_unreachable_is_none(self):
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=None):
            assert greptile.fetch_greptile_summary(5984) == (None, None)


class TestFetchGreptileVerdict:
    def test_prefers_summary_footer_sha(self):
        import unittest.mock as mock
        with mock.patch.object(greptile, "fetch_greptile_summary", return_value=(5, "footersha")) as fs, \
             mock.patch.object(greptile, "fetch_greptile_review_data") as review:
            assert greptile.fetch_greptile_verdict(5984) == (5, "footersha")
        fs.assert_called_once()
        review.assert_not_called()  # summary already yielded a reviewed SHA

    def test_falls_back_to_review_sha_when_no_summary(self):
        import unittest.mock as mock
        with mock.patch.object(greptile, "fetch_greptile_summary", return_value=(None, None)), \
             mock.patch.object(greptile, "fetch_greptile_review_data",
                               return_value=("reviewsha", [], [])):
            assert greptile.fetch_greptile_verdict(5984) == (None, "reviewsha")

    def test_keeps_summary_score_with_review_sha_fallback(self):
        # Summary gave a score but no footer SHA; the reviewed SHA comes from the
        # formal-review path, the score still comes from the summary.
        import unittest.mock as mock
        with mock.patch.object(greptile, "fetch_greptile_summary", return_value=(3, None)), \
             mock.patch.object(greptile, "fetch_greptile_review_data",
                               return_value=("reviewsha", [], [])):
            assert greptile.fetch_greptile_verdict(5984) == (3, "reviewsha")


class TestFetchGreptileFeedback:
    def test_returns_score_body_and_commit(self):
        comments = [{"user": {"login": "greptile-apps[bot]"}, "body": GREPTILE_SUMMARY_BODY}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=comments):
            fb = greptile.fetch_greptile_feedback(5984)
        assert fb is not None
        assert fb["score"] == 5
        assert fb["commit_id"] == "c1c162ed3bb68803433d212033c3102d2e706852"
        assert fb["version"]
        assert "safe to merge" in fb["body"]
        assert "<h3>" not in fb["body"]  # HTML normalized to displayable text

    def test_version_changes_when_existing_summary_is_edited(self):
        import unittest.mock as mock
        older = {"id": 42, "updated_at": "2026-07-22T10:00:00Z",
                 "user": {"login": "greptile-apps[bot]"}, "body": GREPTILE_SUMMARY_BODY}
        newer = {**older, "updated_at": "2026-07-22T10:05:00Z",
                 "body": GREPTILE_SUMMARY_BODY.replace("5/5", "4/5")}
        with mock.patch.object(greptile, "gh_list", return_value=[older]):
            first = greptile.fetch_greptile_feedback(5984)
        with mock.patch.object(greptile, "gh_list", return_value=[newer]):
            second = greptile.fetch_greptile_feedback(5984)
        assert first and second and first["version"] != second["version"]

    def test_none_when_no_greptile_comment(self):
        comments = [{"user": {"login": "human"}, "body": "<h3>Confidence Score: 1/5</h3>"}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=comments):
            assert greptile.fetch_greptile_feedback(5984) is None

    def test_none_when_github_unreachable(self):
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=None):
            assert greptile.fetch_greptile_feedback(5984) is None

    def test_none_when_greptile_left_no_score(self):
        comments = [{"user": {"login": "greptile-apps[bot]"}, "body": "Re-triggering review…"}]
        import unittest.mock as mock
        with mock.patch.object(greptile, "gh_list", return_value=comments):
            assert greptile.fetch_greptile_feedback(5984) is None


class TestFetchGreptileSummaryText:
    def test_returns_the_scored_summary_body_and_its_footer_sha(self):
        comments = [
            {"user": {"login": "greptile-apps[bot]"}, "body": "earlier, unscored"},
            {"user": {"login": "greptile-apps[bot]"},
             "body": "<h3>Greptile Summary</h3>\nThe guard misses terminalResultSeen.\n"
                     "<h3>Confidence Score: 3/5</h3>\nLast reviewed commit: "
                     "https://github.com/o/r/commit/3bc15948abc"},
            {"user": {"login": "someone"}, "body": "Confidence Score: 5/5"},
        ]
        with mock.patch.object(greptile, "gh_list", return_value=comments):
            body, sha = greptile.fetch_greptile_summary_text(5365)
        assert body is not None and "misses terminalResultSeen" in body
        assert sha == "3bc15948abc"

    def test_no_scored_summary_is_none(self):
        with mock.patch.object(greptile, "gh_list",
                               return_value=[{"user": {"login": "greptile-apps[bot]"},
                                              "body": "just a note"}]):
            assert greptile.fetch_greptile_summary_text(1) == (None, None)

    def test_github_unreachable_is_none(self):
        with mock.patch.object(greptile, "gh_list", return_value=None):
            assert greptile.fetch_greptile_summary_text(1) == (None, None)
