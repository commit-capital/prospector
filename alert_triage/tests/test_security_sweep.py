"""security_sweep: runs the four steps in order and keeps going past a failure."""
from alert_triage import security_sweep


def test_sweep_runs_every_step_in_order_and_survives_failures(monkeypatch, capsys, tmp_path):
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
    root = str(tmp_path)
    rc = security_sweep.main(["--limit", "5", "--store", root])
    assert rc == 1
    assert [c[0] for c in calls] == ["alert-ingest", "alert-find-fixed",
                                     "advisory-ingest", "advisory-find-fixed"]
    assert calls[1][1] == ["--limit", "5", "--store", root]
    assert calls[2][1] == ["--store", root]
    out = capsys.readouterr().out
    assert "alert-ingest failed: no token" in out


def test_sweep_counts_a_nonzero_step_return_as_failure(monkeypatch, capsys):
    def ok(argv: list[str] | None = None) -> int:
        return 0

    def crashed(argv: list[str] | None = None) -> int:
        return 1

    monkeypatch.setattr(security_sweep, "STEPS", [
        ("alert-ingest", ok, False),
        ("alert-find-fixed", crashed, True),
    ])
    assert security_sweep.main([]) == 1
    assert "alert-find-fixed exited 1" in capsys.readouterr().out
