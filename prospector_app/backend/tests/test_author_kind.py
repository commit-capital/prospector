from __future__ import annotations

import pytest

from pipeline.model import Pr
from prospector_app.backend import service


@pytest.mark.parametrize("author, association, kind", [
    ("app/prospector-bot", None, "prospector"),
    ("prospector-bot[bot]", None, "prospector"),
    ("maint", "MEMBER", "maintainer"),
    ("rando", "NONE", "community"),
    ("dependabot[bot]", None, "community"),
])
def test_author_kind(monkeypatch, author, association, kind):
    monkeypatch.setattr(service.settings, "bot_login", lambda: "prospector-bot")
    pr = Pr(None, {"pr": 1, "meta": {"author": author, "author_association": association}})
    assert service.author_kind(pr) == kind
