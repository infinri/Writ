"""Timing gates must run alone, not inside the loaded suite.

Pins the capabilities.md section "The perf gate measures the hook, not the machine".

Why this exists: two timing tests pass in isolation and fail inside `pytest tests/`,
because p95 is deliberately sensitive to a few slow samples and the full suite keeps
the machine busy with Neo4j, the daemon, and thousands of subprocesses. Measured on
identical code: p95 200/213/203ms alone earlier in the day, 219/230ms alone an hour
later. The gauge moves ~30ms while the code moves zero, so inside the suite it
measures the machine rather than the hook.

The fix is a marker, not a floor change. The floors stay where they are; they simply
get read somewhere the reading means something.

Per TEST-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"
MAKEFILE = REPO / "Makefile"

# Every timing gate that must move behind the marker, with the class or test that
# carries it.
TIMED = {
    "tests/test_hook_perf_floors.py": "perf",
    "tests/test_retrieval.py": "perf",
}


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as f:
        return tomllib.load(f)


class TestMarkerIsRegistered:
    def test_perf_marker_is_declared(self) -> None:
        """An unregistered marker is a warning, and under -W error a failure, so it
        has to be declared rather than just used."""
        markers = _pyproject().get("tool", {}).get("pytest", {}).get("ini_options", {}).get(
            "markers", []
        )
        assert any(m.split(":")[0].strip() == "perf" for m in markers), (
            f"'perf' not declared in pyproject markers: {markers}"
        )

    def test_default_selection_excludes_perf(self) -> None:
        """The whole point: a bare `pytest` must not run the timing gates."""
        addopts = _pyproject().get("tool", {}).get("pytest", {}).get("ini_options", {}).get(
            "addopts", ""
        )
        if isinstance(addopts, list):
            addopts = " ".join(addopts)
        assert "not perf" in addopts, (
            f"default addopts must deselect perf-marked tests; got {addopts!r}"
        )


class TestTimingTestsCarryTheMarker:
    @pytest.mark.parametrize("path", sorted(TIMED))
    def test_file_declares_the_marker(self, path: str) -> None:
        text = (REPO / path).read_text()
        assert re.search(r"pytest\.mark\.perf|pytestmark\s*=.*perf", text), (
            f"{path} contains a timing gate but does not carry the perf marker"
        )

    def test_pre_write_dispatch_floor_is_deselected_by_default(self) -> None:
        """Behavioural, not textual: collect with the default options and assert the
        timing test is not among them."""
        # sys.executable, never bare "python3": the PATH interpreter is host state
        # (CI's hostedtoolcache python has no pytest), the suite's own interpreter
        # by definition does.
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_hook_perf_floors.py",
             "--collect-only", "-q"],
            capture_output=True, text=True, cwd=str(REPO), timeout=180,
        )
        # Guard against the vacuous pass this test shipped with: a subprocess that
        # failed to run pytest at all produces empty stdout, which satisfies a
        # not-in assertion trivially. The collection must have actually happened.
        assert "No module named" not in proc.stderr, (
            "the collection subprocess did not run pytest:\n" + proc.stderr[-300:]
        )
        assert proc.stdout.strip(), (
            "empty collection output; the deselection was never exercised"
        )
        assert "test_pre_write_dispatch_p95_under_floor" not in proc.stdout, (
            "the timing gate is still collected by the default selection:\n"
            + proc.stdout[-500:]
        )

    def test_marker_selection_still_finds_them(self) -> None:
        """Deselected by default must not mean unreachable: `-m perf` has to collect
        them, or the gate has been deleted rather than isolated."""
        # sys.executable for the same reason as above: this is the line that
        # failed in CI (run 31848974902) when bare "python3" had no pytest.
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_hook_perf_floors.py",
             "--collect-only", "-q", "-m", "perf", "-o", "addopts="],
            capture_output=True, text=True, cwd=str(REPO), timeout=180,
        )
        assert "test_pre_write_dispatch_p95_under_floor" in proc.stdout, (
            "the timing gate is not collectable via -m perf:\n" + proc.stdout[-500:]
        )


class TestMakeTarget:
    def test_makefile_has_a_perf_target(self) -> None:
        assert re.search(r"^perf:", MAKEFILE.read_text(), re.MULTILINE), (
            "Makefile needs a `perf` target so the gates have somewhere to run"
        )

    def test_perf_target_selects_the_marker(self) -> None:
        text = MAKEFILE.read_text()
        block = re.search(r"^perf:.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
        assert block and "-m perf" in block.group(0), (
            "the perf target must select the marker, otherwise it runs the whole suite"
        )

    def test_perf_target_does_not_run_the_full_suite(self) -> None:
        """A target that runs everything reintroduces the load this cycle removes."""
        text = MAKEFILE.read_text()
        block = re.search(r"^perf:.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
        assert block is not None
        body = block.group(0)
        assert not re.search(r"pytest\s+tests/\s*$", body, re.MULTILINE), (
            "the perf target must not invoke the whole tests/ directory"
        )
