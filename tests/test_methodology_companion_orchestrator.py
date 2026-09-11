"""PSR-008 Finding 1: methodology companion must fire in orchestrator mode.

Context: PSR-008 surfaced 0 events with `query_source: "methodology"` despite
the agent running in Work mode. Root cause: writ-rag-inject.sh's orchestrator
short-circuit (`exit 0` at the IS_ORCHESTRATOR=true branch) bypasses the
methodology companion block entirely.

Fix contract (this test pins): when a session has both `mode=work` AND
`is_orchestrator=true`, the inject hook MUST still fire the methodology
companion query before short-circuiting. The status-line output and the
`exit 0` are preserved -- only the methodology block runs additionally.

This is checked structurally (the orchestrator branch of the hook source
references the methodology query) AND end-to-end (running the hook against
a seeded orchestrator cache produces a `query_source: "methodology"` line
in the friction log).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests._hook_runner import (  # noqa: E402
    hook_env,
    instrumented_daemon,
    verify_seeded_mode,
)
from writ.shared.logging import read_streams, resolve_project  # noqa: E402

# Exercises the router's cwd-based project-scope resolution to a tmp subdir;
# opt out of the autouse WRIT_FRICTION_LOG redirect so the rag_query telemetry
# routes to the split per-project streams under WRIT_LOG_ROOT (P1 router).
pytestmark = pytest.mark.no_friction_isolation


SKILL_DIR = str(Path(__file__).resolve().parent.parent)
HOOK = f"{SKILL_DIR}/hooks/scripts/writ-rag-inject.sh"


class TestOrchestratorMethodologyCompanionStructural:
    """Structural: the orchestrator branch must not short-circuit before the
    shared /prompt-bundle call that now carries both the always-on floor and
    the methodology companion (plan dfacff61, Decision 2).

    RE-KEYED (was: "the word 'methodology' appears anywhere in the branch").
    That guard passed for a branch that mentions methodology only in a
    comment, including the CURRENT, pre-fix branch, which says "methodology
    context" in a comment two lines above the `exit 0` that stops it from
    ever being delivered for the always-on floor. Per HARD CONSTRAINT
    (dedup-before-name-swap), the replacement is keyed on the MECHANISM that
    now guarantees delivery: the branch falls through past its own closing
    `fi` into the shared bundle call instead of returning early, not on a
    second name to grep for.

    MUTATION: restoring `exit 0` inside the `IS_ORCHESTRATOR` branch (the
    pre-fix shape, which short-circuited immediately after the hand-rolled
    companion call) turns this red. The OLD "methodology" substring guard
    would NOT have caught that exact mutation, because the word still
    appears in a comment two lines above the exit it is meant to catch.
    """

    def test_orchestrator_branch_has_no_early_exit(self) -> None:
        with open(HOOK) as f:
            body = f.read()

        # Locate the orchestrator branch: everything between the
        # `if [ "$IS_ORCHESTRATOR" = "true" ]; then` and its closing `fi`
        # before the next major block.
        m = re.search(
            r'if \[ "\$IS_ORCHESTRATOR" = "true" \]; then(.+?)\nfi\n',
            body,
            re.DOTALL,
        )
        assert m is not None, "could not locate orchestrator branch in hook source"
        branch_body = m.group(1)

        assert "exit 0" not in branch_body, (
            "the orchestrator branch still short-circuits before reaching the "
            "shared /prompt-bundle call, so a master would keep receiving zero "
            "always-on rules per turn regardless of what the branch's comments "
            "say. Branch body:\n" + branch_body[:1500]
        )


@pytest.fixture(scope="module")
def own_daemon(tmp_path_factory):
    """This module's own isolated daemon, behind an `ensure_corpus()`
    precondition, replacing the hand-rolled `_ensure_aligned_daemon` /
    `_stop_daemon` pair that used to start and stop a daemon on the SUITE
    port (matched by `pkill -f "writ serve --port {_port()}"`, defect D2).

    `ensure_corpus()` RATHER THAN `require_population`, deliberately: the
    population this test needs is the methodology one, whose predicate is a
    PRIVATE constant in `tests/_prompt_turn.py` (`Playbook|Technique` with
    `domain='process'`); copying that predicate here is the duplication this
    program keeps paying for, and promoting it is a change to a module with
    three other consumers. `ensure_corpus()` is the established repair (the
    sibling module's own fixture, `tests/test_orchestrator_injection.py::corpus`,
    documents it the same way) and it runs BEFORE the start, because the
    daemon builds its retrieval indexes AT STARTUP: a repair after it came up
    would leave it warm on an empty graph.
    """
    from tests._corpus import ensure_corpus

    ensure_corpus()
    with instrumented_daemon(tmp_path_factory.mktemp("methodology-orch")) as daemon:
        yield daemon


class TestOrchestratorMethodologyCompanionEndToEnd:
    """End-to-end: run the hook with a seeded orchestrator cache and a
    user prompt, verify the friction log gets a methodology rag_query."""

    def _seed_orchestrator_cache(
        self, cache_dir: str, session_id: str
    ) -> None:
        path = os.path.join(cache_dir, f"writ-session-{session_id}.json")
        with open(path, "w") as f:
            json.dump(
                {
                    "mode": "work",
                    "is_orchestrator": True,
                    "is_subagent": False,
                    "current_phase": "implementation",
                    "loaded_rule_ids": [],
                    "loaded_rule_ids_by_phase": {},
                    "remaining_budget": 8000,
                    "context_percent": 0,
                    "queries": 0,
                    "files_written": [],
                    "loaded_rules": [],
                },
                f,
            )

    def test_orchestrator_fires_methodology_companion(
        self, own_daemon, tmp_path
    ) -> None:
        """End-to-end: invoke the hook against this module's OWN isolated
        daemon with a seeded orchestrator cache. The hook delegates session
        reads to that daemon via HTTP, so the cache must live in the dir the
        daemon's own /health reports, not the test's tmp_path. Use a unique
        session ID and clean up after.

        Pass criterion: the project's P1 metrics stream gets at least one
        rag_query with query_source=methodology."""
        import uuid
        sid = f"orch-method-e2e-{uuid.uuid4().hex[:8]}"
        cache_dir = own_daemon["health"]["cache_dir"]
        cache_path = os.path.join(cache_dir, f"writ-session-{sid}.json")
        self._seed_orchestrator_cache(cache_dir, sid)
        verify_seeded_mode(own_daemon, sid, "work")

        try:
            # Project root the hook runs in; a .git marker makes the router's
            # project-scope resolution derive a stable identity from this cwd.
            project_root = tmp_path / "proj"
            project_root.mkdir()
            (project_root / ".git").mkdir()  # marker

            envelope = json.dumps({
                "session_id": sid,
                "prompt": (
                    "I want to implement a small Python function that takes "
                    "a list of integers and returns their sum. Please plan "
                    "this carefully and write tests first."
                ),
            })

            result = subprocess.run(
                ["bash", HOOK],
                input=envelope,
                capture_output=True,
                text=True,
                cwd=str(project_root),
                env=hook_env(own_daemon),
                timeout=15,
            )
        finally:
            # own_daemon's own teardown stops the daemon and asserts the
            # verdict; this cleans up only the seeded cache this test wrote.
            try:
                os.unlink(cache_path)
            except FileNotFoundError:
                pass
        # Hook must not error out.
        assert result.returncode == 0, (
            f"hook returned {result.returncode}; "
            f"stderr={result.stderr[:1000]}"
        )

        # Drain the session event buffer first, which is what a real turn's Stop hook
        # does. Since the orchestrator branch stopped hand-rolling its own companion
        # call and started using the shared /prompt-bundle path, these rows are
        # BUFFERED (writ_friction_buffer_append) rather than appended synchronously, so
        # reading the stream without a drain measures the buffer, not the router.
        subprocess.run(
            [sys.executable, os.path.join(SKILL_DIR, "bin", "lib", "writ-flush-events.py"), sid],
            capture_output=True, text=True, cwd=str(project_root),
            env=hook_env(own_daemon), timeout=60, check=False,
        )

        # Inspect the router's metrics stream (rag_query -> metrics) for the
        # project scope derived from the hook's cwd, for a methodology rag_query.
        events = read_streams(resolve_project(str(project_root)), ["metrics"])

        methodology = [
            e for e in events
            if e.get("event") == "rag_query"
            and e.get("query_source") == "methodology"
        ]

        assert methodology, (
            "no rag_query with query_source=methodology in orchestrator "
            f"hook run. All events:\n{json.dumps(events, indent=2)}"
        )
