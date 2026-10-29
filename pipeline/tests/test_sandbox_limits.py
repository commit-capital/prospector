"""The per-phase container limits are declared in sandbox/sandbox-run.sh; the
whole-repo phases (compile, build, baseline, regress, e2e) need whole-repo headroom
— a full-suite run's node processes are thread-heavy and threads count against
the pids cgroup, so the bounded-selection limits starve it (uv_thread_create
assertion failures, forks-pool EPIPE). The shell literals are pinned here the
same way run-phase.sh's sentinels are."""
import re
import subprocess
from pathlib import Path

import pytest

SANDBOX_RUN = Path(__file__).resolve().parents[2] / "sandbox" / "sandbox-run.sh"


def _script() -> str:
    return SANDBOX_RUN.read_text()


def test_bounded_phases_default_to_the_small_limits():
    text = _script()
    assert re.search(r"^MEM=2g$", text, re.MULTILINE)
    assert re.search(r"^PIDS=512$", text, re.MULTILINE)


def test_whole_repo_phases_get_whole_repo_limits():
    # One case arm covers all five whole-repo phases: they run a command over
    # the entire tree, so they share one limit class.
    m = re.search(r"^case \"\$PHASE\" in ([^)]*)\) MEM=10g; PIDS=2048;; esac$",
                  _script(), re.MULTILINE)
    assert m is not None, "whole-repo limit arm not found in sandbox-run.sh"
    assert set(m.group(1).split("|")) == {"compile", "build", "baseline", "regress", "e2e"}


def test_docker_run_uses_the_declared_variables():
    text = _script()
    assert '--pids-limit "$PIDS" --memory "$MEM"' in text


def test_e2e_gets_shared_memory_for_chromium():
    assert re.search(r"^\s*e2e\)\s*shm_args=\( --shm-size 1g \)", _script(), re.MULTILINE)


def test_keep_is_the_only_way_to_skip_rm():
    text = _script()
    assert "rm_args=( --rm )" in text and '[ "$KEEP" = 1 ] && rm_args=()' in text


@pytest.mark.parametrize("args", [
    ["--phase", "red", "--keep", "--container-name", "prospector-verify-1-2"],
    ["--phase", "e2e", "--keep"],
])
def test_keep_is_refused_unless_a_named_e2e_phase(args, tmp_path):
    # The refusal comes before any docker call; an empty PATH proves it.
    p = subprocess.run(["/bin/bash", str(SANDBOX_RUN), "--image", "img", *args],
                       capture_output=True, text=True, env={"PATH": str(tmp_path)})
    assert p.returncode == 2
    assert "--keep" in p.stderr
