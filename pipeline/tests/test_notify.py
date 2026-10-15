"""notify: the Slack alerts the threat scan's findings raise, each posted once."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from pipeline import notify
from pipeline.model import Pr
from pipeline.store import Store

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _claim(st: Store, key: str = "malicious:1", host: str = "a",
           now: datetime = T0) -> bool:
    return st.claim_notification(key, host=host, max_tries=3, stale_after=600, now=now)


class TestClaim:
    def test_a_new_alert_is_claimed_by_one_machine(self, tmp_path):
        st = Store(tmp_path)
        assert _claim(st, host="a")
        assert not _claim(st, host="b")

    def test_a_sent_alert_is_never_claimed_again(self, tmp_path):
        st = Store(tmp_path)
        assert _claim(st)
        st.finish_notification("malicious:1", sent=True)
        assert not _claim(st, now=T0 + timedelta(days=1))

    def test_a_failed_alert_is_claimed_again_until_its_tries_run_out(self, tmp_path):
        st = Store(tmp_path)
        for _ in range(3):
            assert _claim(st)
            st.finish_notification("malicious:1", sent=False)
        assert not _claim(st)

    def test_a_claim_left_sending_past_the_stale_window_is_claimed_again(self, tmp_path):
        st = Store(tmp_path)
        assert _claim(st, host="a")
        assert not _claim(st, host="b", now=T0 + timedelta(seconds=599))
        assert _claim(st, host="b", now=T0 + timedelta(seconds=601))

    def test_each_alert_is_claimed_on_its_own(self, tmp_path):
        st = Store(tmp_path)
        assert _claim(st, key="malicious:1")
        assert _claim(st, key="secret:1")

    def test_a_claim_without_a_time_reads_the_clock(self, tmp_path):
        st = Store(tmp_path)
        assert st.claim_notification("malicious:9", host="a", max_tries=3, stale_after=600)


TODAY = date(2026, 10, 2)


def _pr(n: int, *, state: str = "open", author: str = "mallory",
        association: str = "CONTRIBUTOR", title: str = "fix: things") -> Pr:
    return Pr(None, {"pr": n, "meta": {
        "title": title, "author": author, "author_association": association,
        "state": state, "head_sha": "h", "url": f"https://github.com/o/r/pull/{n}"}})


def _incident(n: int, noticed: str = "2026-10-02") -> dict:
    return {"pr": n, "author": "mallory", "head_sha": "h",
            "signatures": ["obfuscated-self-decoder", "capability-smuggle"], "noticed": noticed}


KEY = "server/src/config.ts: const API_KEY = 'q8Zr2LmXv0Pd7KsT4wYc9Hn1Bf6Ug3Ja'"
OTHER_KEY = "server/src/db.ts: const DB_PASSWORD = 'Vt3nR8wQ1zLk5PbX9sYe2HmC7Ja4Gd6U'"


def _secret(n: int, *, status: str = "open", created: str = "2026-10-02",
            evidence: str = KEY, fixture: bool | None = False,
            found: str | None = None) -> dict:
    item = {"id": f"rotate-secret:{n}", "kind": "rotate-secret", "pr": n, "status": status,
            "created": created, "evidence": evidence, "summary": "s"}
    if fixture is not None:
        item["fixture"] = fixture
    if found is not None:
        item["evidence_found"] = found
    return item


class TestDueAlerts:
    def test_an_open_pr_flagged_malicious_today_is_announced(self):
        alerts = notify.due_alerts([_incident(12063)], [], {12063: _pr(12063)}, TODAY)
        assert [a.key for a in alerts] == ["malicious:12063"]
        text = alerts[0].text
        assert "<https://github.com/o/r/pull/12063|#12063>" in text
        assert "obfuscated-self-decoder" in text and "mallory" in text

    @pytest.mark.parametrize("pr, noticed", [(_pr(1, state="closed"), "2026-10-02"),
                                             (_pr(1), "2026-09-29")])
    def test_a_closed_or_old_incident_is_not_announced(self, pr, noticed):
        assert notify.due_alerts([_incident(1, noticed)], [], {1: pr}, TODAY) == []

    def test_a_maintainer_s_live_looking_secret_is_announced_by_file_alone(self):
        prs = {8939: _pr(8939, author="dotta", association="MEMBER")}
        [alert] = notify.due_alerts([], [_secret(8939)], prs, TODAY)
        assert "server/src/config.ts" in alert.text and "dotta" in alert.text
        assert "q8Zr2LmXv0Pd7KsT4wYc9Hn1Bf6Ug3Ja" not in alert.text and "API_KEY" not in alert.text

    def test_each_credential_a_pr_holds_is_its_own_alert(self):
        prs = {8939: _pr(8939, author="dotta", association="MEMBER")}
        keys = [notify.due_alerts([], [_secret(8939, evidence=ev)], prs, TODAY)[0].key
                for ev in (KEY, OTHER_KEY, KEY)]
        assert keys[0] == keys[2] != keys[1]
        assert all(k.startswith("secret:8939:") and "q8Zr2" not in k for k in keys)

    def test_a_credential_found_on_an_old_item_is_announced(self):
        prs = {8939: _pr(8939, author="dotta", association="MEMBER")}
        item = _secret(8939, created="2026-09-20", found="2026-10-02")
        assert len(notify.due_alerts([], [item], prs, TODAY)) == 1

    def test_a_credential_alert_names_the_deployment(self, monkeypatch):
        monkeypatch.setenv("TRIAGE_DISPLAY_NAME", "Acme")
        prs = {8939: _pr(8939, author="dotta", association="MEMBER")}
        text = notify.due_alerts([], [_secret(8939)], prs, TODAY)[0].text
        assert "Possible Acme credential leaked" in text

    @pytest.mark.parametrize("pr, item", [
        (_pr(5, association="CONTRIBUTOR"), _secret(5)),                       # not a maintainer
        (_pr(5, association="MEMBER"), _secret(5, fixture=True)),               # a fixture
        (_pr(5, association="MEMBER"), _secret(5, fixture=None,                 # reads as one
                                                evidence="tests/fixtures/k.ts: KEY='dummy'")),
        (_pr(5, association="MEMBER"), _secret(5, status="dismissed")),          # handled
        (_pr(5, association="MEMBER"), _secret(5, created="2026-09-20")),        # old
        (_pr(5, association="MEMBER", state="merged"), _secret(5)),             # not open
    ])
    def test_other_secrets_stay_out_of_slack(self, pr, item):
        assert notify.due_alerts([], [item], {5: pr}, TODAY) == []

    def test_outside_text_cannot_mention_the_channel_or_forge_a_link(self):
        pr = _pr(7, title="<!channel> urgent <https://evil.example|click> & more")
        text = notify.due_alerts([_incident(7)], [], {7: pr}, TODAY)[0].text
        assert "<!channel>" not in text and "<https://evil.example" not in text
        assert "&lt;!channel&gt;" in text and "&amp;" in text

    def test_a_pr_the_store_lacks_is_skipped(self):
        assert notify.due_alerts([_incident(3)], [_secret(3)], {}, TODAY) == []


class TestSendDue:
    def _store(self, tmp_path) -> Store:
        st = Store(tmp_path)
        st.save_pr({"pr": 12063, "meta": {"title": "t", "author": "mallory",
                                          "state": "open", "head_sha": "h",
                                          "url": "https://github.com/o/r/pull/12063"}})
        return st

    def test_an_alert_is_posted_once_across_passes(self, tmp_path):
        st = self._store(tmp_path)
        posted: list[str] = []
        reg = {"actors": {}, "incidents": [_incident(12063)]}
        for _ in range(2):
            notify.send_due(st, reg, [], url="https://hooks.slack.com/services/T/B/x",
                            post=lambda url, text: posted.append(text) or True, today=TODAY)
        assert len(posted) == 1

    def test_a_failed_post_is_tried_again_on_the_next_pass(self, tmp_path):
        st = self._store(tmp_path)
        reg = {"actors": {}, "incidents": [_incident(12063)]}
        results = iter([False, True])
        sent = [notify.send_due(st, reg, [], url="https://hooks.slack.com/services/T/B/x",
                                post=lambda url, text: next(results), today=TODAY)
                for _ in range(2)]
        assert sent == [[], ["malicious:12063"]]

    def test_a_credential_found_after_an_alert_is_posted_too(self, tmp_path):
        st = Store(tmp_path)
        st.save_pr({"pr": 8939, "meta": {"title": "t", "author": "dotta",
                                         "author_association": "MEMBER", "state": "open",
                                         "head_sha": "h"}})
        posted: list[str] = []
        for item in (_secret(8939), _secret(8939, evidence=OTHER_KEY, found="2026-10-02"),
                     _secret(8939, evidence=OTHER_KEY, found="2026-10-02")):
            notify.send_due(st, {}, [item], url="https://hooks.slack.com/services/T/B/x",
                            post=lambda url, text: posted.append(text) or True, today=TODAY)
        assert len(posted) == 2 and "server/src/db.ts" in posted[1]

    def test_with_no_webhook_nothing_is_read_or_posted(self, monkeypatch):
        monkeypatch.delenv("TRIAGE_SLACK_WEBHOOK_URL", raising=False)
        assert notify.send_due(None, {"incidents": [_incident(1)]}, [],  # type: ignore[arg-type]
                               post=lambda url, text: pytest.fail("posted")) == []


class TestPostWebhook:
    def _respond(self, monkeypatch, status: int | None) -> list[dict]:
        import httpx
        sent: list[dict] = []

        def fake_post(url: str, *, json: dict, timeout: float):
            sent.append(json)
            if status is None:
                raise httpx.ConnectError("down")
            return httpx.Response(status)
        monkeypatch.setattr(httpx, "post", fake_post)
        return sent

    def test_slack_taking_the_post_reads_true(self, monkeypatch):
        sent = self._respond(monkeypatch, 200)
        assert notify.post_webhook("https://hooks.slack.com/x", "hi") is True
        assert sent == [{"text": "hi"}]

    @pytest.mark.parametrize("status", [404, 500, None])
    def test_a_refused_or_unreachable_post_reads_false(self, monkeypatch, status):
        self._respond(monkeypatch, status)
        assert notify.post_webhook("https://hooks.slack.com/x", "hi") is False
