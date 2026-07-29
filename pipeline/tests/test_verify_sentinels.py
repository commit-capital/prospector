"""The sentinel exit codes are declared once in gates.py; sandbox/run-phase.sh
mirrors them. A drift between the two would let the host misread a phase, so the
shell script's literals are pinned to the Python declaration here."""
import re
from pathlib import Path

from pipeline import gates

RUN_PHASE = Path(__file__).resolve().parents[2] / "sandbox" / "run-phase.sh"


def _declared(name: str) -> int:
    m = re.search(rf"^{name}=(\d+)$", RUN_PHASE.read_text(), re.MULTILINE)
    assert m is not None, f"{name} not declared in run-phase.sh"
    return int(m.group(1))


def test_sentinels_match_gates():
    assert _declared("SENTINEL_PROBE_FAIL") == gates.SENTINEL_PROBE_FAIL
    assert _declared("SENTINEL_TEST_FAIL") == gates.SENTINEL_TEST_FAIL
    assert _declared("SENTINEL_PATCH_CONFLICT") == gates.SENTINEL_PATCH_CONFLICT


def test_sentinels_are_distinct_and_nonzero():
    codes = (gates.SENTINEL_PROBE_FAIL, gates.SENTINEL_TEST_FAIL,
             gates.SENTINEL_PATCH_CONFLICT)
    assert len(set(codes)) == 3
    assert gates.SENTINEL_PASS == 0 and all(c != 0 for c in codes)
