"""Dispatcher tests: subcommands map to the right tool main()s and forward argv verbatim."""
from __future__ import annotations

import importlib

import pytest
import uvicorn

from pipeline import cli

FORWARDING = [
    ("ingest", "pipeline.ingest"),
    ("threat-scan", "pipeline.threat_scan"),
    ("triage-cluster", "pipeline.triage_cluster"),
    ("recluster", "pipeline.recluster"),
    ("security-review", "pipeline.security_review"),
]


@pytest.mark.parametrize("sub,modname", FORWARDING)
def test_subcommand_forwards_argv(monkeypatch, sub, modname):
    mod = importlib.import_module(modname)
    seen = []

    def fake_main(argv=None):
        seen.append(argv)
        return 7

    monkeypatch.setattr(mod, "main", fake_main)
    assert cli.main([sub, "--flag", "x"]) == 7
    assert seen == [["--flag", "x"]]


def test_status_runs_views_main(monkeypatch):
    views = importlib.import_module("pipeline.views")
    called = []
    monkeypatch.setattr(views, "main", lambda: called.append(True) or 0)
    assert cli.main(["status"]) == 0
    assert called == [True]


def test_status_rejects_arguments(capsys):
    assert cli.main(["status", "extra"]) == 2
    assert "no arguments" in capsys.readouterr().err


def test_serve_invokes_uvicorn_with_port(monkeypatch):
    calls = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["serve", "--port", "9999"]) == 0
    assert calls["app"] == "app.backend.app:app"
    assert calls["port"] == 9999


def test_serve_dev_rejects_port(capsys):
    assert cli.main(["serve", "--dev", "--port", "9999"]) == 2
    assert "--port" in capsys.readouterr().err


def test_unknown_subcommand_is_usage_error(capsys):
    assert cli.main(["frobnicate"]) == 2
    assert "usage" in capsys.readouterr().err


def test_no_arguments_is_usage_error(capsys):
    assert cli.main([]) == 2
    assert "usage" in capsys.readouterr().err


def test_help_prints_usage(capsys):
    assert cli.main(["--help"]) == 0
    assert "pr-triager" in capsys.readouterr().out
