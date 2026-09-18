"""Nothing guards the INVOCATION of a benchmark that wipes the whole graph.

Finding 6 of the containment audit (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3). On
2026-08-05 invoking `benchmarks/*.py` blind wiped the live graph. The live protection
today is exactly one runtime assertion, `assert_safe_to_wipe`, called at
`run_benchmarks.py:146,165` and `scale_benchmark.py:567`, and it is stronger than it
looks: it refuses unless `WRIT_TEST_GRAPH=1` AND the connected instance is not the
production one, compared by (host, port). What nothing guards is the invocation, and
nothing pins the assertion's presence.

TWO PREMISES FROM THE DISPATCH DID NOT SURVIVE READING THE SOURCES, and both change what
is tested here. `benchmarks/run_benchmarks.py` has no `main` and no `if __name__ ==
"__main__"`: it is a pytest module whose own docstring says "Run with: pytest
benchmarks/run_benchmarks.py -v", so `python3 benchmarks/run_benchmarks.py` runs no test
and wipes nothing, and an argparse confirmation flag there would be dead code. The
destructive spelling is the PYTEST one, which the gate's script arm exempts on purpose
(`-m` disqualifies the whole command so the suite can run). And
`benchmarks/scale_benchmark.py` already carries the `--run` flag the dispatch proposes,
parsed before any heavy import and pinned by
`tests/test_graph_dump.py::TestScaleBenchmarkRequiresExplicitRun`.

THE POPULATION IS DERIVED AND KEYED BY PATH, never enumerated:
`tests/_inventory.py::wiping_benchmark_entrypoints()` parses `benchmarks/*.py` with `ast`
and returns every module that really CALLS `clear_all`. A new wiping benchmark joins this
matrix by existing, and a member that loses its guard fails by name. ast rather than text
because `benchmarks/_corpus_safety.py` names `clear_all()` only inside docstrings and is
the module that REFUSES an unsafe wipe: a text scan files the safety module with the
destroyers.

WHAT THE STRUCTURAL PIN CANNOT DO, disclosed rather than implied: it proves the module
CALLS the assertion, not that the call precedes every wipe on every path. That is a
reachability analysis; this is a population guard.

NO TEST HERE RUNS A BENCHMARK. The gate cases feed command strings to the hook, and the
collection case runs `--collect-only`, which imports the module and executes no test.
"""
from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

# ONE RUNNER, NOT A SECOND COPY. `_run` feeds a command string to the real
# writ-bash-write-gate.sh under an isolated cache and friction log; duplicating it here
# is how the two modules would come to disagree about what "the gate said" means, which
# is the duplication tests/_inventory.py exists to delete.
from tests.test_bash_irreversible_gate import _denied, _reason, _run

REPO = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO / ".venv" / "bin" / "python"

# The refusal calls a module-level guard can end in. `parser.error` is how
# `scale_benchmark.py` already refuses; `pytest.skip(allow_module_level=True)` is the
# shape a pytest module needs, since it has no main to guard.
_REFUSAL_CALLS = ("skip", "exit", "error")

_UNDERIVED = "<wiping_benchmark_entrypoints() derived nothing>"


def _population() -> dict[str, list[int]]:
    """The derived population, or a loud failure. Never silently empty.

    ANTI-VACUITY: an empty derivation would make every per-member assertion below pass on
    any tree, which is the failure mode this repo has shipped before. It is asserted here
    rather than in `test_count_pin_discipline.py::test_each_derivation_is_non_empty`,
    whose floor is three members; this population has two.
    """
    from tests._inventory import wiping_benchmark_entrypoints

    members = wiping_benchmark_entrypoints()
    assert members, (
        "wiping_benchmark_entrypoints() derived no benchmark at all; two modules under "
        "benchmarks/ call clear_all today, so an empty map means the derivation went "
        "blind and every pin in this module would pass on any tree"
    )
    return members


def _members() -> list:
    """Collection-time params, with a sentinel rather than an empty list.

    `tests/test_corpus_floor.py::_floor_labels` is the shape: an empty `argvalues` makes
    pytest report a SKIP, which reads exactly like coverage that ran.
    """
    try:
        from tests._inventory import wiping_benchmark_entrypoints

        paths = sorted(wiping_benchmark_entrypoints())
    except Exception:  # noqa: BLE001 - collection must not abort the session
        return [pytest.param(_UNDERIVED, id="population-underived")]
    return [pytest.param(p, id=p.replace("/", "-")) for p in paths] or [
        pytest.param(_UNDERIVED, id="population-empty")
    ]


def _tree(rel: str) -> ast.Module:
    return ast.parse((REPO / rel).read_text(encoding="utf-8", errors="replace"))


def _calls(node: ast.AST, names: tuple[str, ...]) -> bool:
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        called = getattr(child.func, "attr", None) or getattr(child.func, "id", None)
        if called in names:
            return True
    return False


def _raises_system_exit(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            exc = getattr(child.exc, "func", child.exc)
            if getattr(exc, "id", None) == "SystemExit":
                return True
    return False


def _invocation_guard_line(tree: ast.Module) -> int | None:
    """The line of the first MODULE-LEVEL `if` that refuses, or None.

    Module level is the whole property: a guard inside a function runs only when
    something calls that function, and the incident was an invocation nobody intended to
    make. Two shapes qualify and both are live in this tree: `if __name__ ==
    "__main__": ... parser.error(...)` (scale_benchmark.py, before the heavy imports) and
    a module-level skip for a pytest module that has no main to guard.

    KNOWN LIMIT, stated rather than worked around: this matches a refusal CALL by name,
    so a top-level `if` whose body merely logs through something called `error` would
    satisfy it. The population is two modules and each is read by a human at review; the
    alternative (resolving the callee) buys nothing here.
    """
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        if _calls(node, _REFUSAL_CALLS) or _raises_system_exit(node):
            return node.lineno
    return None


# --------------------------------------------------------------------------- #
# Capability 18, first half: the population is derived, keyed by path
# --------------------------------------------------------------------------- #

class TestTheWipingBenchmarkPopulationIsDerived:

    def test_it_holds_the_modules_that_really_call_the_wipe(self) -> None:
        assert set(_population()) == {
            "benchmarks/run_benchmarks.py",
            "benchmarks/scale_benchmark.py",
        }, _population()

    def test_the_safety_module_is_not_in_the_population(self) -> None:
        """`benchmarks/_corpus_safety.py` mentions the wipe four times, all inside
        docstrings, and its job is to REFUSE one. A text scan files it with the
        destroyers, which is the whole reason the derivation parses."""
        assert "benchmarks/_corpus_safety.py" not in _population()

    def test_a_scoped_delete_is_not_a_whole_graph_wipe(self) -> None:
        """`benchmarks/bench_targets.py` carries a scoped `DETACH DELETE` of its own
        synthetic node and calls no wipe. It stays out of this population, which is what
        keeps `make bench` and the existing pytest allow-pin green."""
        assert "benchmarks/bench_targets.py" not in _population()

    def test_the_derivation_reads_calls_and_not_text(self, tmp_path) -> None:
        """CONDITIONALITY, on synthetic modules under tmp_path rather than the real
        tree."""
        from tests._inventory import wiping_benchmark_entrypoints

        (tmp_path / "wipes.py").write_text("def go(db):\n    db.clear_all()\n")
        (tmp_path / "talks.py").write_text('"""Mentions clear_all() in prose."""\n')
        derived = wiping_benchmark_entrypoints(benchmarks_dir=tmp_path)
        assert [Path(p).name for p in derived] == ["wipes.py"], derived


# --------------------------------------------------------------------------- #
# Capability 18: every member carries both guards, each failing by name
# --------------------------------------------------------------------------- #

class TestEveryWipingBenchmarkDeclaresAModuleLevelInvocationGuard:

    @pytest.mark.parametrize("rel", _members())
    def test_the_module_refuses_before_it_can_be_invoked(self, rel) -> None:
        assert rel != _UNDERIVED, (
            "the wiping-benchmark population derived nothing, so this class ran against "
            "no member at all"
        )
        line = _invocation_guard_line(_tree(rel))
        assert line is not None, (
            f"{rel} wipes the whole graph and declares no module-level invocation guard, "
            f"so a bare run (or a bare `pytest` from the repo root, which collects it: "
            f"pyproject.toml sets no testpaths) reaches the wipe with nothing in the way"
        )

    @pytest.mark.parametrize("rel", _members())
    def test_the_guard_comes_before_the_first_wipe(self, rel) -> None:
        assert rel != _UNDERIVED, "population underived"
        line = _invocation_guard_line(_tree(rel))
        first_wipe = _population()[rel][0]
        assert line is not None and line < first_wipe, (
            f"{rel}: the guard is at line {line} and the first wipe at {first_wipe}; a "
            f"refusal that runs after the destruction is not a guard"
        )


class TestEveryWipingBenchmarkCallsTheRuntimeAssertion:
    """The pin buys down "the one runtime assertion is refactored away and nothing
    notices". Its limit is disclosed in the module docstring rather than papered over.
    """

    @pytest.mark.parametrize("rel", _members())
    def test_it_calls_assert_safe_to_wipe(self, rel) -> None:
        assert rel != _UNDERIVED, "population underived"
        assert _calls(_tree(rel), ("assert_safe_to_wipe",)), (
            f"{rel} wipes the graph without calling assert_safe_to_wipe, the one runtime "
            f"check that refuses unless the connected instance is disposable"
        )


class TestEveryWipingBenchmarkIsRefusedByTheGateUnderBothSpellings:
    """Each member, each spelling, failing BY NAME. The pytest spelling is the one the
    interpreter walk deliberately skips (`-m` disqualifies the whole command so the suite
    can run) and the one `run_benchmarks.py`'s own docstring recommends.
    """

    @pytest.mark.parametrize("rel", _members())
    def test_the_interpreter_spelling_is_refused(self, tmp_path, rel) -> None:
        assert rel != _UNDERIVED, "population underived"
        payload, _rows = _run(f"python3 {rel}", tmp_path)
        assert _denied(payload), f"not refused: python3 {rel} -> {payload!r}"
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)

    @pytest.mark.parametrize("rel", _members())
    def test_the_pytest_spelling_is_refused(self, tmp_path, rel) -> None:
        assert rel != _UNDERIVED, "population underived"
        payload, _rows = _run(f".venv/bin/python -m pytest {rel}", tmp_path)
        assert _denied(payload), f"not refused: pytest {rel} -> {payload!r}"
        assert "ENF-IRREVERSIBLE" in _reason(payload), _reason(payload)

    @pytest.mark.parametrize("rel", _members())
    def test_the_refusal_names_the_file_and_the_mechanism(self, tmp_path, rel) -> None:
        """An operator must be able to tell WHY without reading the hook, and the two
        facts that matter are which file would run and what it does."""
        payload, _rows = _run(f"python3 {rel}", tmp_path)
        reason = _reason(payload)
        assert Path(rel).name in reason, reason
        assert "graph" in reason.lower() or "wipe" in reason.lower(), reason


# --------------------------------------------------------------------------- #
# Capability 19: a bare collection skips, carrying the shared instructions
# --------------------------------------------------------------------------- #

class TestABareCollectionOfTheBenchmarkSkips:
    """`pyproject.toml` sets no `testpaths`, so a bare `pytest` from the repo root
    collects `benchmarks/run_benchmarks.py` and imports it. The guard is a module-level
    skip unless the connected instance is marked disposable, reusing `full_wipe_allowed`
    and `how_to_run_safely` instead of inventing a second consent mechanism.

    `--collect-only`, never a real run: collecting imports the module and executes no
    test, which is exactly the boundary this guard defends.
    """

    REL = "benchmarks/run_benchmarks.py"

    def _collect(self, tmp_path: Path) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "COLUMNS": "200",
            "WRIT_CACHE_DIR": str(tmp_path / "cache"),
            "WRIT_NO_AUTOSTART": "1",
        }
        env.pop("WRIT_TEST_GRAPH", None)
        return subprocess.run(
            [str(VENV_PYTHON), "-m", "pytest", self.REL,
             "--collect-only", "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=str(REPO), capture_output=True, text=True, env=env, timeout=300,
        )

    def test_the_module_is_skipped_rather_than_collected(self, tmp_path) -> None:
        """CASE-INSENSITIVE, because the case is not the mechanism and all three flag
        combinations were measured rather than assumed. Under `--collect-only -q -rs`,
        pytest 8.4.2 reports the skip as `SKIPPED [1] benchmarks/run_benchmarks.py:40:
        ...` plus `no tests collected`; the other two combinations print the lowercase
        word. Either spelling is the same observation: the module refused collection
        instead of yielding tests.

        THE REJECTED ALTERNATIVE, recorded so nobody reaches for it: putting the word
        "skipped" into the skip reason would make this pass by word-matching a message
        the operator reads, which is not what this test is about. The reason's own text
        is pinned against `how_to_run_safely()` by the sibling below.
        """
        proc = self._collect(tmp_path)
        assert "skipped" in proc.stdout.lower(), (
            "a bare collection of the wiping benchmark collected its tests instead of "
            f"skipping: {proc.stdout[-2000:]!r}"
        )

    def test_the_skip_carries_the_shared_instructions(self, tmp_path) -> None:
        """READ FROM THE ARTIFACT, not restated here: the first sentence comes from
        `how_to_run_safely()` itself, so a reworded message cannot make this pass while
        the operator reads something else."""
        from writ.graph.db._safety import how_to_run_safely

        sentence = how_to_run_safely().split(".")[0]
        proc = self._collect(tmp_path)
        assert sentence in proc.stdout, (
            f"the skip reason does not carry {sentence!r}: {proc.stdout[-2000:]!r}"
        )

    def test_the_guard_names_the_shared_helpers_rather_than_its_own_text(self) -> None:
        """The structural half, and the reason there is one: a terminal can truncate a
        reason, but a second consent mechanism written by hand is invisible to any
        runtime assertion until it drifts from the one `assert_safe_to_wipe` enforces."""
        source = (REPO / self.REL).read_text(encoding="utf-8")
        for helper in ("full_wipe_allowed", "how_to_run_safely"):
            assert helper in source, (
                f"{self.REL} does not use {helper}; the guard must reuse the shared "
                f"consent surface rather than inventing a second one"
            )
