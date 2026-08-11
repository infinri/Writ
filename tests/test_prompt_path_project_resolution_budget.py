"""Part 3 (isolation cycle v2): project resolution must not cost the hot path.

plan.md's Query Budget Plan (PERF-QBUDGET-001) for this decision:
  1. Expected additional DB queries per retrieval request: ZERO. get_projects()
     is a registry read the daemon holds in memory for its lifetime, and
     resolution is a longest-prefix string comparison over a handful of rows.
  2. Worst case: ONE MATCH (p:Project) on the first request after a daemon
     start, plus one per registry invalidation. Bounded by daemon restarts,
     not request volume.
  3. Caching: the registry is cached in the daemon process, invalidated by
     create_project.
  4. Fallback: registry unreadable means no project resolved -> doctrine-only,
     never an error to the hook.
  5. Measurement: a test asserts the hooks add no python spawn.

These tests pin (1) and (5) directly. They deliberately do NOT re-implement
tests/test_prompt_path_process_budget.py's strace-based ratchet
(PYTHON_BUDGET = 10) -- that file already owns "how many python interpreters
does writ-rag-inject.sh start", and PYTHON_BUDGET must NOT be raised by this
cycle (an instruction from the dispatch brief, not just a style preference).
Instead this file imports that constant and pins it unchanged, and separately
proves the STRUCTURAL reason project-root computation is free on that path:
detect_project_root (bin/lib/common.sh) is pure bash with zero python3 calls
in its own body, so adding a call to it to writ-read-rag.sh/writ-posttool-rag.sh
(capability 33) cannot itself add an interpreter start.

RED today: there is no in-process registry cache at all -- every call to
resolve_project_for_cwd re-reads the :Project registry from Neo4j
(writ/graph/db/project_store.py:70, `projects = await self.get_projects()`),
so two /query requests in the same daemon lifetime issue two registry reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMON_SH = REPO_ROOT / "bin" / "lib" / "common.sh"


# ---------------------------------------------------------------------------
# 1. No additional Neo4j query per retrieval request (in-process caching)
# ---------------------------------------------------------------------------


class _CountingResolverDB:
    """Mixes in the REAL resolve_project_for_cwd (longest-prefix match logic
    is exercised for real), overriding only get_projects -- the one method
    that is an actual Neo4j round trip -- with a call-counting stub. This lets
    the test observe the real seam a daemon-level cache has to sit in front
    of, without needing to name whatever internal caching function the
    implementation adds."""

    def __init__(self, projects: list[dict]) -> None:
        self._projects = projects
        self.get_projects_calls = 0

    async def get_projects(self) -> list[dict]:
        self.get_projects_calls += 1
        return self._projects

    async def resolve_project_for_cwd(self, cwd: str) -> str:
        from writ.graph.db.project_store import ProjectStoreMixin

        return await ProjectStoreMixin.resolve_project_for_cwd(self, cwd)


@pytest.fixture()
def registry_db() -> _CountingResolverDB:
    return _CountingResolverDB([
        {"name": "proj-a", "repo_root": "/repo/proj-a", "bible_root": "bible"},
        {"name": "proj-b", "repo_root": "/repo/proj-b", "bible_root": "bible"},
    ])


class TestProjectResolutionAddsNoNeo4jQueryPerRequest:
    @pytest.mark.asyncio
    async def test_resolving_the_same_root_twice_reads_the_registry_at_most_once(
        self, registry_db,
    ) -> None:
        """Simulates two retrieval requests inside one long-lived daemon
        process (server._db is a single persistent object across requests).
        A per-request registry read would make this counter grow with every
        call; a cached one stays flat after the first."""
        first = await registry_db.resolve_project_for_cwd("/repo/proj-a/app.py")
        second = await registry_db.resolve_project_for_cwd("/repo/proj-a/lib/util.py")

        assert first == "proj-a" and second == "proj-a"
        assert registry_db.get_projects_calls <= 1, (
            f"the registry was read {registry_db.get_projects_calls} times for two "
            f"requests against the same project; expected at most one Neo4j round "
            f"trip per daemon lifetime, not one per request"
        )

    @pytest.mark.asyncio
    async def test_resolving_a_different_root_also_reads_the_registry_at_most_once(
        self, registry_db,
    ) -> None:
        """A DIFFERENT project root on the second request must still be served
        from the cached registry, not trigger a fresh read -- the cache is
        keyed on nothing narrower than the whole registry."""
        first = await registry_db.resolve_project_for_cwd("/repo/proj-a/app.py")
        second = await registry_db.resolve_project_for_cwd("/repo/proj-b/app.py")

        assert first == "proj-a" and second == "proj-b"
        assert registry_db.get_projects_calls <= 1, (
            f"registry read {registry_db.get_projects_calls} times across two "
            f"requests for different projects; expected the cached registry to "
            f"serve both"
        )

    @pytest.mark.asyncio
    async def test_the_counter_itself_is_not_vacuously_stuck_at_zero(self, registry_db) -> None:
        """Anti-vacuity: a broken (or accidentally never-called) resolver would
        also report get_projects_calls <= 1 -- trivially, at 0. This proves at
        least one real registry read happens somewhere in a cold daemon."""
        await registry_db.resolve_project_for_cwd("/repo/proj-a/app.py")
        assert registry_db.get_projects_calls >= 1, (
            "resolve_project_for_cwd never read the registry at all -- the "
            "counter above would pass vacuously"
        )


# ---------------------------------------------------------------------------
# 2. No additional python spawn per hook invocation
# ---------------------------------------------------------------------------


def _extract_bash_function(source: str, name: str) -> str | None:
    """Same brace-counting extraction as tests/test_rag_query_helper.py."""
    marker = f"{name}() {{"
    idx = source.find(marker)
    if idx == -1:
        return None
    brace_idx = idx + len(marker) - 1
    depth = 0
    i = brace_idx
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[idx : i + 1]
        i += 1
    return None


class TestProjectRootComputationIsPureBashOnTheHotPath:
    """The structural reason capability 33 (rag_query sends the project root)
    costs nothing on the per-prompt/per-write path: detect_project_root is
    pure shell (no python3 spawn), so calling it from writ-read-rag.sh and
    writ-posttool-rag.sh -- which do not compute a project root today -- adds
    no interpreter start."""

    def test_detect_project_root_body_spawns_no_python_interpreter(self) -> None:
        source = COMMON_SH.read_text()
        body = _extract_bash_function(source, "detect_project_root")
        assert body is not None, "bin/lib/common.sh must still define detect_project_root"
        assert "python3" not in body, (
            "detect_project_root must stay pure bash -- a python3 call here would "
            "reintroduce the exact per-call interpreter-start cost this function "
            "was written to remove (see its own docstring: 'Pure bash: no processes at all')"
        )

    def test_detect_project_root_is_still_the_function_the_hooks_would_call(self) -> None:
        """Anti-vacuity: pins that writ-rag-inject.sh (which already computes a
        project root today) calls this exact function, so the guard above is
        checking the function the OTHER two hooks will be pointed at too, not
        an unrelated same-named stub."""
        rag_inject = (REPO_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh").read_text()
        assert re.search(r"detect_project_root\s+", rag_inject), (
            "writ-rag-inject.sh no longer calls detect_project_root; the "
            "pure-bash guard above would be pinning an unused function"
        )


class TestPythonBudgetRatchetIsUnchangedByProjectResolution:
    """This cycle must not raise tests/test_prompt_path_process_budget.py's
    PYTHON_BUDGET (currently 10) to pay for project-root scoping. Importing
    the constant (rather than re-deriving it) means a change to that file's
    ratchet is what this test reacts to, not a second, independently-drifting
    copy of the number."""

    def test_python_budget_constant_is_still_ten(self) -> None:
        from tests.test_prompt_path_process_budget import PYTHON_BUDGET

        assert PYTHON_BUDGET == 10, (
            f"PYTHON_BUDGET changed to {PYTHON_BUDGET}; if project-root scoping "
            f"(capability 33/34) raised this ratchet to pay for a new python spawn, "
            f"that is the regression this test exists to catch. If it was lowered "
            f"by an unrelated improvement, update this pinned value deliberately."
        )
