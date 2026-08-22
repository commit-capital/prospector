"""Signal 4's host-observed facts — the independent repro's exit code and the
shape of its command — settle whether the repro corroborates, ahead of the
judge's rating of an output tail the exit no longer explains."""
from pipeline import gates
from pipeline.model import Pr
from pipeline.testsupport import reviews_section


HEAD = "abc123"
NOW = "2026-06-10T00:00:00+00:00"

# A rating the judge would have to have produced for the exit to read as
# corroboration; every case below pairs it with a host fact that outranks it.
MATCHING = {"matches": True, "applicable": True, "confidence": "high"}


def _pr(**over) -> Pr:
    rec = {
        "pr": 1,
        "meta": {"title": "t", "author": "a", "state": "open", "draft": False,
                 "head_sha": HEAD, "checked_at": NOW},
        "signals": {"ci": "passing", "mergeable": True,
                    "has_tests": True, "checked_at": NOW,
                    "against_head_sha": HEAD},
        "reviews": reviews_section(HEAD, NOW),
        "drift": {"state": "applicable", "checked_at": NOW,
                  "against_head_sha": HEAD},
    }
    rec.update(over)
    return Pr(None, rec)


def _merge_analysis(**over) -> dict:
    return dict({"disposition": "merge", "rationale": "r",
                 "checked_at": NOW, "against_head_sha": HEAD}, **over)


def _green(**over) -> dict:
    return dict({"verdict": "GREEN", "findings": [], "override": None,
                 "checked_at": NOW, "against_head_sha": HEAD}, **over)


def _verified(**over) -> dict:
    return dict({"outcome": "verified-fix", "signals": {}, "findings": [],
                 "tier": 0, "against_base_sha": "base1", "checked_at": NOW,
                 "against_head_sha": HEAD}, **over)


def _repro(*, command: str = "node --test repro.mjs", exit_code: int | None = 20,
           rating: dict | None = None) -> dict:
    signals: dict = {"blind_adequacy": {"repro_command": command},
                     "independent_repro": {"ran": True, "exit_code": exit_code}}
    if rating is not None:
        signals["repro_reason_match"] = rating
    return signals


class TestReproHostFactsOutrankTheRating:
    """A repro whose exit carries no meaning cannot corroborate, however the
    judge rated the tail: the rating is read from untrusted test output, while
    the exit and the pre-committed command are the host's own facts. Each case
    pairs a non-corroborating host fact with a matching rating — the rating
    that would otherwise carry the record to a full-confidence verified-fix."""

    def test_a_container_error_is_incomplete(self):
        # An exit outside the sentinel set is the phase container failing — a
        # timeout, an OOM, an image fault — not a test result.
        pr = _pr(verify=_verified(signals=_repro(exit_code=137, rating=MATCHING)))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "errored at the harness level" in why

    def test_a_repro_that_passed_on_the_base_is_incomplete(self):
        # SENTINEL_PASS means the repro found the pinned base healthy, so it
        # demonstrated no defect there to corroborate.
        pr = _pr(verify=_verified(signals=_repro(exit_code=0, rating=MATCHING)))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "demonstrated no defect" in why

    def test_a_misrooted_config_is_incomplete(self):
        # --config names a config in server/ that the command never makes the
        # runner's root, so that config's own relative setupFiles resolve
        # against the repo root and the suite dies at load: the failing exit is
        # indistinguishable from a genuine failure, and it is the harness's
        # defect rather than the PR's bug.
        pr = _pr(verify=_verified(signals=_repro(
            command=("npx vitest run --config server/vitest.config.ts "
                     "server/src/x.test.ts"),
            rating=MATCHING)))
        why = gates.verify_signals_incomplete(pr)
        assert why is not None and "dies at load" in why

    def test_a_config_the_command_roots_itself_at_still_corroborates(self):
        # The same config, with the runner rooted where it lives: nothing is
        # misrooted, so the failing exit means what it says.
        pr = _pr(verify=_verified(signals=_repro(
            command=("cd server && npx vitest run --config vitest.config.ts "
                     "src/x.test.ts"),
            rating=MATCHING)))
        assert gates.verify_signals_incomplete(pr) is None

    def test_a_clean_failing_repro_still_corroborates(self):
        # The corroborating case the checks above must not swallow: a plain
        # SENTINEL_TEST_FAIL on a command with no config conflict.
        pr = _pr(verify=_verified(signals=_repro(rating=MATCHING)))
        assert gates.verify_signals_incomplete(pr) is None


class TestMergeConsequences:
    """Partial Signal 4 evidence is already wired end to end; these pin that a
    host-errored repro reaches both merge paths the same way a not-matching
    rating does."""

    def test_merge_allowed_refuses_a_container_error(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(signals=_repro(exit_code=137, rating=MATCHING)))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is False and "incomplete" in why

    def test_merge_allowed_passes_a_clean_failing_repro(self):
        pr = _pr(analysis=_merge_analysis(), security=_green(),
                 verify=_verified(signals=_repro(rating=MATCHING)))
        ok, why = gates.merge_allowed(pr, today="2026-06-10")
        assert ok is True, why

    def test_merge_eligibility_stays_open_but_names_the_gap(self):
        pr = _pr(verify=_verified(signals=_repro(exit_code=137, rating=MATCHING)))
        ok, why = gates.merge_eligibility(pr, today="2026-06-10")
        assert ok is True
        assert "incomplete" in why
