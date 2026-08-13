"""Cycle 8: the suite stops sharing the production graph.

Pins capabilities 2-11 and 20-22 of capabilities.md / plan.md's Capabilities
section (item 1, 12-19, 23-26 and every `(real graph)` / `(operational)` item
are out of scope for this file; those need the disposable instance itself,
which does not exist yet).

None of the following exists in the tree yet: `tests/_graph.py`,
`scripts/test-graph.sh`, or the conftest.py / tests/_corpus.py changes this
cycle adds. Every test below is expected to fail today, for that reason.

SAFETY, read before touching anything in this file. This suite must never
open, write to, or wipe `bolt://localhost:7687` -- the live production graph a
running interactive daemon serves right now. A previous attempt at this exact
cycle leaked and wiped it via a `docker exec cypher-shell` subprocess that
bypassed the driver and the env override entirely. Two things follow:

  * Capabilities 2, 3, 4, 20, 21 and 22 need no Neo4j at all -- plain env
    mappings, a pure classifier, and text/AST inspection of two files -- and
    are tested that way, with zero I/O.
  * Capabilities 5, 6, 7, 8, 9, 10 and 11 touch the session hooks in
    tests/conftest.py and tests/_corpus.py. This checkout HAS a bible/ tree,
    so `tests.conftest.pytest_sessionstart`, called for real and unmocked,
    reaches a real Neo4j reachability probe today.
    tests/test_post_suite_neo4j_restoration.py already documents that these
    session hooks cannot be safely re-invoked from a test ("we can't directly
    re-invoke it from a test"); this file follows the same discipline. The
    session-hook WIRING is pinned by source inspection (AST extraction of the
    named function's own text, never execution), while the LOGIC that wiring
    is expected to call (`apply_isolation_env`, `classify_isolation`,
    `isolation_refusal_message`, `ensure_corpus`) is pinned by calling those
    functions directly, with the one still-existing real dependency
    (`ensure_corpus`'s Neo4j/subprocess calls) monkeypatched out.

Every `tests._graph` import happens INSIDE a test function, and every
`scripts/test-graph.sh` / `tests/_graph.py` read happens inside a fixture or
test body, never at module scope -- so this file COLLECTS today even though
every test in it fails.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFTEST = REPO_ROOT / "tests" / "conftest.py"
GRAPH_MODULE = REPO_ROOT / "tests" / "_graph.py"
TEST_GRAPH_SCRIPT = REPO_ROOT / "scripts" / "test-graph.sh"
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

PRODUCTION_URI = "bolt://localhost:7687"
# Prefer a bogus, never-served port over a real scratch instance so an
# "unreachable" case can never accidentally resolve to something real.
BOGUS_UNREACHABLE_URI = "bolt://localhost:7699"


def _extract_function_source(module_source: str, func_name: str) -> str:
    """AST-based extraction of a top-level function's source text, or "" if it
    is not defined.

    Mirrors the role `_extract_bash_function` plays for shell in
    tests/test_no_tool_prereqs.py, for Python instead of bash: a real parse
    rather than a regex over text, so nesting/formatting/decorators cannot
    fool it.
    """
    try:
        tree = ast.parse(module_source)
    except SyntaxError:
        return ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return ast.get_source_segment(module_source, node) or ""
    return ""


def _guarded_by_flag(body: str, call_marker: str, flag_name: str) -> bool:
    """True if some `if` line mentioning `flag_name` appears before the line
    containing `call_marker`, within the same function body.

    A light heuristic for "the call is conditional on the flag", deliberately
    tolerant of which branch/indentation/polarity the implementation picks.
    """
    lines = body.splitlines()
    call_idx = next((i for i, line in enumerate(lines) if call_marker in line), None)
    if call_idx is None:
        return False
    return any("if" in line and flag_name in line for line in lines[:call_idx])


# ---------------------------------------------------------------------------
# Capability 2: apply_isolation_env sets the three connection vars + forces
# WRIT_TEST_GRAPH=1, leaving any caller-set value untouched.
# ---------------------------------------------------------------------------


class TestApplyIsolationEnvSetsDefaults:
    def test_sets_all_three_connection_vars_on_an_empty_mapping(self) -> None:
        from tests._graph import (
            ISOLATED_NEO4J_PASSWORD,
            ISOLATED_NEO4J_URI,
            ISOLATED_NEO4J_USER,
            apply_isolation_env,
        )

        env: dict[str, str] = {}
        applied = apply_isolation_env(env)

        assert applied is True
        assert env["WRIT_NEO4J_URI"] == ISOLATED_NEO4J_URI
        assert env["WRIT_NEO4J_USER"] == ISOLATED_NEO4J_USER
        assert env["WRIT_NEO4J_PASSWORD"] == ISOLATED_NEO4J_PASSWORD

    def test_forces_writ_test_graph_flag_to_one_on_an_empty_mapping(self) -> None:
        from tests._graph import apply_isolation_env

        env: dict[str, str] = {}
        apply_isolation_env(env)

        assert env["WRIT_TEST_GRAPH"] == "1"

    def test_leaves_a_caller_set_uri_untouched(self) -> None:
        from tests._graph import apply_isolation_env

        env = {"WRIT_NEO4J_URI": "bolt://scratch-host:9999"}
        apply_isolation_env(env)

        assert env["WRIT_NEO4J_URI"] == "bolt://scratch-host:9999"

    def test_leaves_a_caller_set_user_and_password_untouched(self) -> None:
        from tests._graph import apply_isolation_env

        env = {"WRIT_NEO4J_USER": "scratch-user", "WRIT_NEO4J_PASSWORD": "scratch-pass"}
        apply_isolation_env(env)

        assert env["WRIT_NEO4J_USER"] == "scratch-user"
        assert env["WRIT_NEO4J_PASSWORD"] == "scratch-pass"

    def test_still_forces_test_graph_flag_even_when_caller_set_the_uri(self) -> None:
        """WRIT_TEST_GRAPH is an assignment, not a setdefault: an operator's
        own scratch instance still needs the marker that makes clear_all's
        guard treat the connected graph as disposable."""
        from tests._graph import apply_isolation_env

        env = {"WRIT_NEO4J_URI": "bolt://scratch-host:9999", "WRIT_TEST_GRAPH": "0"}
        apply_isolation_env(env)

        assert env["WRIT_TEST_GRAPH"] == "1"


# ---------------------------------------------------------------------------
# Capability 3: WRIT_TEST_NO_ISOLATION=1 -> apply_isolation_env returns False
# and changes NOTHING, including not setting WRIT_TEST_GRAPH.
# ---------------------------------------------------------------------------


class TestApplyIsolationEnvOptOut:
    def test_returns_false_when_opted_out(self) -> None:
        from tests._graph import apply_isolation_env

        env = {"WRIT_TEST_NO_ISOLATION": "1"}
        assert apply_isolation_env(env) is False

    def test_changes_nothing_at_all_when_opted_out(self) -> None:
        from tests._graph import apply_isolation_env

        env = {"WRIT_TEST_NO_ISOLATION": "1", "SOME_OTHER_VAR": "kept"}
        before = dict(env)

        apply_isolation_env(env)

        assert env == before, "opted-out must not mutate the mapping at all"

    def test_does_not_set_writ_test_graph_when_opted_out(self) -> None:
        from tests._graph import apply_isolation_env

        env = {"WRIT_TEST_NO_ISOLATION": "1"}
        apply_isolation_env(env)

        assert "WRIT_TEST_GRAPH" not in env

    def test_opt_out_wins_even_if_a_uri_is_also_pre_set(self) -> None:
        from tests._graph import apply_isolation_env

        env = {"WRIT_TEST_NO_ISOLATION": "1", "WRIT_NEO4J_URI": PRODUCTION_URI}
        before = dict(env)

        assert apply_isolation_env(env) is False
        assert env == before


# ---------------------------------------------------------------------------
# Capability 4: classify_isolation is pure and returns exactly one of
# "opted-out" | "production-target" | "unreachable" | "isolated".
#
# Assumed signature: classify_isolation(*, opted_out: bool, is_production:
# bool, reachable: bool) -> str. plan.md names the function and its four
# return values but not its parameter names; the four-state CONTRACT below
# (the return values and their precedence) is what this class pins.
# ---------------------------------------------------------------------------


class TestClassifyIsolation:
    def test_opted_out_wins_regardless_of_the_other_two(self) -> None:
        from tests._graph import classify_isolation

        assert (
            classify_isolation(opted_out=True, is_production=True, reachable=True)
            == "opted-out"
        )
        assert (
            classify_isolation(opted_out=True, is_production=False, reachable=False)
            == "opted-out"
        )

    def test_production_target_when_unreachable(self) -> None:
        from tests._graph import classify_isolation

        assert (
            classify_isolation(opted_out=False, is_production=True, reachable=False)
            == "production-target"
        )

    def test_production_target_even_when_reachable(self) -> None:
        """The discriminating case: a classifier that only checked
        reachability would call a reachable production instance 'isolated'
        and would pass every other case in this class. It must not."""
        from tests._graph import classify_isolation

        assert (
            classify_isolation(opted_out=False, is_production=True, reachable=True)
            == "production-target"
        )

    def test_unreachable(self) -> None:
        from tests._graph import classify_isolation

        assert (
            classify_isolation(opted_out=False, is_production=False, reachable=False)
            == "unreachable"
        )

    def test_isolated(self) -> None:
        from tests._graph import classify_isolation

        assert (
            classify_isolation(opted_out=False, is_production=False, reachable=True)
            == "isolated"
        )

    def test_returns_only_one_of_the_four_literal_strings(self) -> None:
        from tests._graph import classify_isolation

        allowed = {"opted-out", "production-target", "unreachable", "isolated"}
        for opted_out in (True, False):
            for is_production in (True, False):
                for reachable in (True, False):
                    result = classify_isolation(
                        opted_out=opted_out,
                        is_production=is_production,
                        reachable=reachable,
                    )
                    assert result in allowed, f"unexpected classification: {result!r}"


# ---------------------------------------------------------------------------
# Capability 5: the isolation env is applied at CONFTEST IMPORT (module top
# level), before pytest imports any test module. Tests the ORDERING property.
# ---------------------------------------------------------------------------


class TestIsolationEnvAppliedAtConftestImport:
    def test_a_module_binding_neo4j_uri_at_its_own_import_sees_the_isolated_value(
        self,
    ) -> None:
        """At least 17 test modules do `NEO4J_URI = get_neo4j_uri()` at their
        OWN import (test_authoring.py:21, test_graph_dump.py:20, ...). pytest
        imports the rootdir conftest before any test module, so the isolation
        env has to be applied at conftest's MODULE TOP LEVEL, not inside a
        fixture or a pytest_configure hook that runs later.

        Proved with a fresh subprocess interpreter (not this process, which
        may already be mid-session): it imports tests.conftest and then reads
        get_neo4j_uri(), which is pure config resolution -- no network
        connection is opened by this test at any point.
        """
        script = textwrap.dedent(
            f"""
            import os, sys
            sys.path.insert(0, {str(REPO_ROOT)!r})
            for var in ("WRIT_NEO4J_URI", "WRIT_NEO4J_USER", "WRIT_NEO4J_PASSWORD",
                        "WRIT_TEST_GRAPH", "WRIT_TEST_NO_ISOLATION"):
                os.environ.pop(var, None)
            import tests.conftest  # noqa: F401  (must apply isolation env AT IMPORT)
            from writ.config import get_neo4j_uri
            sys.stdout.write(get_neo4j_uri())
            """
        )
        python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
        result = subprocess.run(
            [python, "-c", script],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

        from tests._graph import ISOLATED_NEO4J_URI

        assert result.stdout.strip() == ISOLATED_NEO4J_URI, (
            "a module that reads get_neo4j_uri() at its own import must see the "
            "isolated instance -- which only holds if tests/conftest.py applies "
            "apply_isolation_env at its own module top level, before any test "
            f"module import\nstderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# Capabilities 6, 7, 8: isolation_refusal_message's content contract.
#
# Assumed signature: isolation_refusal_message(resolved_uri: str, counts:
# dict[str, int] | None = None) -> str. counts is None for the
# production-target and unreachable refusals (plan.md: "the same message
# shape"), and a per-label dict for the incomplete-corpus refusal (plan.md:
# "the per-label counts in the message"). If the real signature differs, the
# CONTENT contract below -- the resolved URI, `make test-graph-up`, and the
# opt-out -- is what must not move.
# ---------------------------------------------------------------------------


class TestIsolationRefusalMessage:
    def test_names_the_resolved_uri(self) -> None:
        from tests._graph import isolation_refusal_message

        msg = isolation_refusal_message(PRODUCTION_URI)
        assert PRODUCTION_URI in msg

    def test_names_make_test_graph_up(self) -> None:
        from tests._graph import isolation_refusal_message

        msg = isolation_refusal_message(PRODUCTION_URI)
        assert "make test-graph-up" in msg

    def test_names_the_opt_out(self) -> None:
        from tests._graph import isolation_refusal_message

        msg = isolation_refusal_message(PRODUCTION_URI)
        assert "WRIT_TEST_NO_ISOLATION" in msg

    def test_same_shape_for_an_unreachable_instance(self) -> None:
        """Cap 7: the refusal shape must not change when the reason is
        'unreachable' instead of 'production-target'."""
        from tests._graph import isolation_refusal_message

        msg = isolation_refusal_message(BOGUS_UNREACHABLE_URI)
        assert BOGUS_UNREACHABLE_URI in msg
        assert "make test-graph-up" in msg
        assert "WRIT_TEST_NO_ISOLATION" in msg

    def test_includes_per_label_counts_when_a_replay_leaves_the_corpus_incomplete(
        self,
    ) -> None:
        """Cap 8: when a replay leaves the isolated instance incomplete, the
        refusal must show the actual per-label counts (the same shape
        tests/_corpus.methodology_counts() already returns), not just say
        'incomplete'."""
        from tests._graph import isolation_refusal_message

        counts = {"Rule": 250, "Playbook": 14, "SubagentRole": 5}
        msg = isolation_refusal_message("bolt://localhost:7688", counts=counts)

        for label, n in counts.items():
            assert label in msg
            assert str(n) in msg

    def test_counts_are_absent_from_the_message_when_not_supplied(self) -> None:
        """The production-target / unreachable refusals never had a census to
        report; the message must not fabricate one."""
        from tests._graph import isolation_refusal_message

        msg = isolation_refusal_message(PRODUCTION_URI)
        assert "Rule" not in msg


# ---------------------------------------------------------------------------
# Capabilities 6, 7, 8 (wiring half): the session-start preflight in
# tests/conftest.py routes its decision through classify_isolation and
# isolation_refusal_message, and warms via tests._corpus before checking
# completeness.
#
# Source inspection only -- see the module docstring for why these tests
# never call tests.conftest.pytest_sessionstart directly.
# ---------------------------------------------------------------------------


class TestSessionStartPreflightWiring:
    @pytest.fixture
    def conftest_source(self) -> str:
        assert CONFTEST.exists(), "tests/conftest.py must exist"
        return CONFTEST.read_text()

    def test_preflight_function_is_defined(self, conftest_source: str) -> None:
        body = _extract_function_source(conftest_source, "_preflight_isolated_graph")
        assert body, (
            "tests/conftest.py must define _preflight_isolated_graph: the "
            "session-start refusal named in plan.md Decision 3"
        )

    def test_preflight_classifies_before_deciding_to_refuse(
        self, conftest_source: str
    ) -> None:
        body = _extract_function_source(conftest_source, "_preflight_isolated_graph")
        assert "classify_isolation" in body, (
            "_preflight_isolated_graph must route its decision through "
            "tests._graph.classify_isolation, not a bespoke reachability check"
        )

    def test_preflight_builds_its_refusal_from_the_shared_message_helper(
        self, conftest_source: str
    ) -> None:
        body = _extract_function_source(conftest_source, "_preflight_isolated_graph")
        assert "isolation_refusal_message" in body, (
            "the refusal text must come from tests._graph.isolation_refusal_message, "
            "so the production-target, unreachable and incomplete-corpus refusals "
            "cannot drift into three different messages"
        )

    def test_preflight_warms_the_corpus_before_checking_completeness(
        self, conftest_source: str
    ) -> None:
        """Cap 8: a reachable-but-empty instance is warmed from
        writ-corpus.cypher (via tests._corpus.ensure_corpus, which this cycle
        teaches the writ-corpus.cypher fallback) before the completeness
        check runs."""
        body = _extract_function_source(conftest_source, "_preflight_isolated_graph")
        assert "ensure_corpus" in body
        assert "is_complete" in body

    def test_pytest_sessionstart_calls_the_preflight(self, conftest_source: str) -> None:
        body = _extract_function_source(conftest_source, "pytest_sessionstart")
        assert "_preflight_isolated_graph" in body, (
            "pytest_sessionstart must call _preflight_isolated_graph, and it must "
            "run before the pre-existing bible/ early return so it also refuses "
            "on a clean checkout that never had a bible/ tree"
        )

    def test_tripwire_fixture_is_deleted(self, conftest_source: str) -> None:
        """Decision 4: the tripwire is deleted, not kept as defence in depth --
        it monkeypatched Neo4jConnection.__init__, which a docker-exec
        subprocess never touches, so it was structurally blind to the
        transport that actually leaked."""
        assert "_refuse_production_graph_when_isolated" not in conftest_source
        assert "Neo4jConnection.__init__" not in conftest_source


# ---------------------------------------------------------------------------
# Capability 9: ensure_corpus refills from writ-corpus.cypher when bible/ is
# absent, instead of returning silently.
# ---------------------------------------------------------------------------


class TestEnsureCorpusDumpFallback:
    """ensure_corpus() already exists; this cycle adds ONE new branch: when
    bible/ is absent, fall back to writ-corpus.cypher via `import-cypher`
    instead of returning silently (tests/_corpus.py, plan.md Decision 5).
    Every dependency that could touch a real graph or spawn a real process is
    monkeypatched: this proves the BRANCH is taken, not that a real replay
    succeeds (that half is `(real graph)`, out of this file's scope)."""

    def test_falls_back_to_writ_corpus_cypher_when_bible_is_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import tests._corpus as corpus_mod

        fake_root = tmp_path
        (fake_root / "writ-corpus.cypher").write_text("// dump\n")
        # deliberately no "bible" dir under fake_root

        monkeypatch.setattr(corpus_mod, "_REPO_ROOT", fake_root)
        monkeypatch.setattr(corpus_mod, "neo4j_reachable", lambda: True)
        monkeypatch.setattr(corpus_mod, "is_complete", lambda *a, **k: False)

        calls: list[list[str]] = []

        def _fake_run(argv, **kwargs):
            calls.append(list(argv))

            class _Result:
                returncode = 0

            return _Result()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        corpus_mod.ensure_corpus()

        assert calls, "ensure_corpus must attempt a replay when bible/ is absent"
        joined = " ".join(calls[0])
        assert "import-cypher" in joined
        assert "writ-corpus.cypher" in joined


# ---------------------------------------------------------------------------
# Capability 10: pytest_sessionfinish skips the corpus restore on an isolated
# run, and still performs it under WRIT_TEST_NO_ISOLATION=1.
# ---------------------------------------------------------------------------


class TestSessionFinishRestoreWiring:
    @pytest.fixture
    def conftest_source(self) -> str:
        assert CONFTEST.exists(), "tests/conftest.py must exist"
        return CONFTEST.read_text()

    def test_sessionfinish_checks_isolation_state_before_restoring(
        self, conftest_source: str
    ) -> None:
        body = _extract_function_source(conftest_source, "pytest_sessionfinish")
        assert "WRIT_SUITE_IS_ISOLATED" in body, (
            "pytest_sessionfinish must consult the isolation flag conftest sets "
            "at import (WRIT_SUITE_IS_ISOLATED = apply_isolation_env(...)) before "
            "replaying writ-corpus.cypher, so it can skip the restore on an "
            "isolated run and still perform it under the opt-out"
        )

    def test_the_module_level_isolation_flag_is_bound_at_import(
        self, conftest_source: str
    ) -> None:
        assert re.search(
            r"^WRIT_SUITE_IS_ISOLATED\s*=\s*apply_isolation_env\(",
            conftest_source,
            re.M,
        ), (
            "tests/conftest.py must bind WRIT_SUITE_IS_ISOLATED = "
            "apply_isolation_env(_os.environ) at module top level (plan.md "
            "Decision 2), before any test module is imported"
        )


# ---------------------------------------------------------------------------
# Capability 11: under WRIT_TEST_NO_ISOLATION=1 the suite behaves exactly as
# it did before this cycle: no env forced (TestApplyIsolationEnvOptOut), no
# preflight, restore on finish (TestSessionFinishRestoreWiring covers the
# "still restores" half), disposable_graph skipping unchanged.
# ---------------------------------------------------------------------------


class TestOptOutParity:
    @pytest.fixture
    def conftest_source(self) -> str:
        assert CONFTEST.exists(), "tests/conftest.py must exist"
        return CONFTEST.read_text()

    def test_preflight_call_is_conditional_on_the_isolation_flag(
        self, conftest_source: str
    ) -> None:
        """The opt-out must be able to reach a run where the preflight never
        fires at all -- it cannot be an unconditional call, or WRIT_TEST_NO_
        ISOLATION=1 would still refuse a run it was told to leave alone."""
        body = _extract_function_source(conftest_source, "pytest_sessionstart")
        assert "_preflight_isolated_graph" in body
        assert "WRIT_SUITE_IS_ISOLATED" in body
        assert _guarded_by_flag(
            body, "_preflight_isolated_graph", "WRIT_SUITE_IS_ISOLATED"
        ), (
            "the call to _preflight_isolated_graph() must be gated by an `if` "
            "referencing WRIT_SUITE_IS_ISOLATED, so the opt-out reaches a run "
            "with no preflight at all"
        )


# ---------------------------------------------------------------------------
# Capabilities 20 + 21: scripts/test-graph.sh's lifecycle and safety guards.
#
# Source-inspection only, matching this repo's own convention for
# docker-touching shell scripts (tests/test_bootstrap.py's
# TestBootstrapSections: "these are source-inspection tests -- no real
# Docker/pip invocation"). This file never executes scripts/test-graph.sh:
# doing so manipulates a real container, which is out of bounds for a test
# suite regardless of whether the script exists yet.
# ---------------------------------------------------------------------------


class TestTestGraphScriptExists:
    def test_script_exists(self) -> None:
        assert TEST_GRAPH_SCRIPT.exists(), "scripts/test-graph.sh must exist"

    def test_script_is_executable(self) -> None:
        assert TEST_GRAPH_SCRIPT.exists(), "scripts/test-graph.sh must exist"
        assert os.access(TEST_GRAPH_SCRIPT, os.X_OK), (
            "scripts/test-graph.sh must be executable"
        )


class TestTestGraphScriptUpIsIdempotent:
    @pytest.fixture
    def content(self) -> str:
        assert TEST_GRAPH_SCRIPT.exists(), "scripts/test-graph.sh must exist"
        return TEST_GRAPH_SCRIPT.read_text()

    def test_dispatches_up_and_down_subcommands(self, content: str) -> None:
        assert re.search(r"\bup\b", content) and re.search(r"\bdown\b", content), (
            "scripts/test-graph.sh must dispatch on an 'up' and a 'down' subcommand"
        )

    def test_checks_container_state_before_creating_or_starting(
        self, content: str
    ) -> None:
        """Idempotent 'up' has to look before it leaps: it must inspect
        whether the container exists / is running before deciding whether to
        create or start it, or every invocation would try to `docker run` a
        name that already exists."""
        assert any(
            marker in content
            for marker in ("docker inspect", "docker ps", "docker container inspect")
        ), (
            "scripts/test-graph.sh must check existing container state before "
            "creating or starting one"
        )

    def test_waits_for_bolt_before_declaring_success(self, content: str) -> None:
        assert "7688" in content
        assert any(marker in content for marker in ("until", "sleep", "wait")), (
            "scripts/test-graph.sh must wait for bolt to answer, not assume it is "
            "up immediately after `docker run` / `docker start`"
        )

    def test_replay_references_the_tracked_dump(self, content: str) -> None:
        assert "writ-corpus.cypher" in content, (
            "scripts/test-graph.sh must warm the instance from writ-corpus.cypher "
            "when it had to create or start the container"
        )

    def test_published_port_is_7688_not_7687(self, content: str) -> None:
        assert re.search(r"7688:7687\b", content), (
            "scripts/test-graph.sh must publish the container's fixed bolt port "
            "7687 on host port 7688 (a '-p 7688:7687' style mapping)"
        )


class TestTestGraphScriptDownAndSafety:
    @pytest.fixture
    def content(self) -> str:
        assert TEST_GRAPH_SCRIPT.exists(), "scripts/test-graph.sh must exist"
        return TEST_GRAPH_SCRIPT.read_text()

    def test_down_stops_the_container(self, content: str) -> None:
        assert "docker stop" in content, (
            "scripts/test-graph.sh's `down` must stop the container"
        )

    def test_down_does_not_remove_the_volume(self, content: str) -> None:
        assert "docker volume rm" not in content
        assert not re.search(r"docker\s+rm\s+[^\n]*-v\b", content), (
            "scripts/test-graph.sh's `down` must keep the data (no "
            "volume-destroying `docker rm -v` / `docker volume rm`)"
        )

    def test_refuses_if_its_own_published_port_is_ever_7687(self, content: str) -> None:
        assert "7687" in content, (
            "scripts/test-graph.sh must guard against ever publishing the "
            "production bolt port 7687"
        )
        assert re.search(r"7687.{0,200}exit\s+[1-9]|exit\s+[1-9].{0,200}7687", content, re.S), (
            "scripts/test-graph.sh must exit non-zero if its bolt port is ever 7687 "
            "-- a bare mention of 7687 is not a guard"
        )

    def test_does_nothing_under_the_opt_out(self, content: str) -> None:
        assert "WRIT_TEST_NO_ISOLATION" in content, (
            "scripts/test-graph.sh must honor WRIT_TEST_NO_ISOLATION=1 and do "
            "nothing at all"
        )


# ---------------------------------------------------------------------------
# Capability 22: the bolt port in scripts/test-graph.sh equals the port in
# tests/_graph.py::ISOLATED_NEO4J_URI, read from BOTH FILES.
# ---------------------------------------------------------------------------


class TestPortParity:
    """The one place a scripts/*.sh <-> tests/*.py drift would go unnoticed
    otherwise: the script and the module each hardcode the disposable bolt
    port, and nothing else compares them."""

    _MAPPED_PORT_RE = re.compile(r"(\d+):7687\b")

    def test_script_and_module_bolt_ports_match(self) -> None:
        assert GRAPH_MODULE.exists(), "tests/_graph.py must exist"
        assert TEST_GRAPH_SCRIPT.exists(), "scripts/test-graph.sh must exist"

        from urllib.parse import urlsplit

        from tests._graph import ISOLATED_NEO4J_URI

        uri = (
            ISOLATED_NEO4J_URI
            if "://" in ISOLATED_NEO4J_URI
            else f"bolt://{ISOLATED_NEO4J_URI}"
        )
        module_port = urlsplit(uri).port
        assert module_port is not None, (
            f"could not parse a port out of ISOLATED_NEO4J_URI={ISOLATED_NEO4J_URI!r}"
        )

        script_text = TEST_GRAPH_SCRIPT.read_text()
        mapped_ports = {int(m.group(1)) for m in self._MAPPED_PORT_RE.finditer(script_text)}
        assert mapped_ports, (
            "scripts/test-graph.sh must publish its bolt port via a "
            "'<host-port>:7687' style docker port mapping"
        )
        assert mapped_ports == {module_port}, (
            f"scripts/test-graph.sh publishes bolt port(s) {sorted(mapped_ports)} but "
            f"tests/_graph.py::ISOLATED_NEO4J_URI resolves to port {module_port}: the "
            "script and the module are the two halves of the same fact and must agree"
        )

    def test_module_port_is_not_the_production_port(self) -> None:
        from tests._graph import ISOLATED_NEO4J_URI

        assert "7687" not in ISOLATED_NEO4J_URI
        assert "7688" in ISOLATED_NEO4J_URI
