"""security_sweep: runs the four steps in order and keeps going past a failure."""
from alert_triage import security_sweep


def test_sweep_runs_every_step_in_order_and_survives_failures(monkeypatch, capsys):
    calls: list[tuple[str, list[str]]] = []

    def ok(name: str):
        def run(argv: list[str] | None = None) -> int:
            calls.append((name, list(argv or [])))
            return 0
        return run

    def boom(argv: list[str] | None = None) -> int:
        calls.append(("alert-ingest", list(argv or [])))
        raise SystemExit("no token")

    monkeypatch.setattr(security_sweep, "STEPS", [
        ("alert-ingest", boom, False),
        ("alert-find-fixed", ok("alert-find-fixed"), True),
        ("advisory-ingest", ok("advisory-ingest"), False),
        ("advisory-find-fixed", ok("advisory-find-fixed"), True),
    ])
    rc = security_sweep.main(["--limit", "5", "--store", "/tmp/x"])
    assert rc == 1
    assert [c[0] for c in calls] == ["alert-ingest", "alert-find-fixed",
                                     "advisory-ingest", "advisory-find-fixed"]
    assert calls[1][1] == ["--limit", "5", "--store", "/tmp/x"]
    assert calls[2][1] == ["--store", "/tmp/x"]
    out = capsys.readouterr().out
    assert "alert-ingest failed: no token" in out
