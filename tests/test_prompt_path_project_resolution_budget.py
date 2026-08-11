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


class _SlowCountingResolverDB(_CountingResolverDB):
    """Same real resolver, same counted I/O boundary, plus an explicit await
    inside get_projects.

    The await is the point, not a delay: a Neo4j round trip suspends, and the
    stampede exists precisely because the event loop can hand control to another
    caller between "the cache is expired" and "the cache is written". An
    instantaneous stub would never yield, so a lock-less implementation could
    pass a concurrency test by accident.
    """

    async def get_projects(self) -> list[dict]:
        import asyncio

        self.get_projects_calls += 1
        await asyncio.sleep(0.01)
        return self._projects


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
    async def test_concurrent_callers_on_a_cold_cache_read_the_registry_once(self) -> None:
        """SINGLE FLIGHT, proven with real concurrency.

        Every test above awaits SEQUENTIALLY, so the read-check-write around the
        cache was structurally untested and a stampede was invisible: N callers
        arriving on an expired (or cold) cache each saw `projects is None`, and
        each awaited get_projects, because there is a suspension point between the
        check and the write. Measured before the fix, at N=5: five queries. The
        invariant this file opens with -- "bounded by daemon restarts, not request
        volume" -- is false under concurrency without a lock, and the daemon is
        exactly where concurrency happens (one FastAPI process, many in-flight
        requests, one shared server._db).

        Per ENF-SYS-005 a sequential or mocked test is not evidence for a
        concurrency claim, so this uses asyncio.gather over one shared connection
        object and stubs ONLY the get_projects I/O boundary, whose await is what
        creates the interleaving window.
        """
        import asyncio

        db = _SlowCountingResolverDB([
            {"name": "proj-a", "repo_root": "/repo/proj-a", "bible_root": "bible"},
            {"name": "proj-b", "repo_root": "/repo/proj-b", "bible_root": "bible"},
        ])

        results = await asyncio.gather(*[
            db.resolve_project_for_cwd("/repo/proj-a/app.py") for _ in range(5)
        ])

        assert db.get_projects_calls == 1, (
            f"{db.get_projects_calls} registry reads for 5 concurrent callers on a cold "
            f"cache; expected exactly 1. Without single-flight every concurrent caller "
            f"that arrives while the cache is expired pays its own Neo4j round trip, so "
            f"the cost scales with request volume -- the opposite of this file's "
            f"documented budget"
        )
        assert results == ["proj-a"] * 5, (
            f"single-flight must not cost correctness: every waiter has to receive the "
            f"resolved project, got {results!r}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_callers_on_an_expired_cache_also_read_it_once(self) -> None:
        """The TTL path, not just the cold path. A populated-but-stale cache is the
        steady-state shape in a long-lived daemon: PROJECTS_CACHE_TTL_S expires
        every 30 seconds forever, so this is the stampede that actually recurs,
        while the cold-cache one happens once per restart."""
        import asyncio

        from writ.graph.db.project_store import PROJECTS_CACHE_TTL_S

        db = _SlowCountingResolverDB([
            {"name": "proj-a", "repo_root": "/repo/proj-a", "bible_root": "bible"},
        ])
        await db.resolve_project_for_cwd("/repo/proj-a/app.py")
        assert db.get_projects_calls == 1
        # Age the cache past its TTL by rewriting the stamp, not by sleeping 30s.
        db._projects_cache_at -= PROJECTS_CACHE_TTL_S + 1

        results = await asyncio.gather(*[
            db.resolve_project_for_cwd("/repo/proj-a/lib/util.py") for _ in range(5)
        ])

        assert db.get_projects_calls == 2, (
            f"{db.get_projects_calls} total registry reads; expected 2 (the first, "
            f"warming call plus exactly ONE refresh shared by the five concurrent "
            f"callers that found the cache expired)"
        )
        assert results == ["proj-a"] * 5

    def test_one_connection_object_survives_a_second_event_loop(self) -> None:
        """The single-flight lock must not turn a reused connection into a hard error.

        An asyncio.Lock binds to a loop the first time it is CONTENDED (uncontended
        acquires never call _get_loop), and awaiting it from any other loop then raises
        `RuntimeError: <Lock ...> is bound to a different event loop`. Verified on this
        interpreter, 3.12.3. That is not hypothetical here: one connection object shared
        across pytest-asyncio's function-scoped loops hits it, and inside the daemon it
        would turn project resolution into an exception rather than a cache miss --
        breaking scoping for every /query instead of degrading it. Deliberately a SYNC
        test with two asyncio.run calls, because a single loop cannot express it.
        """
        import asyncio

        db = _SlowCountingResolverDB([
            {"name": "proj-a", "repo_root": "/repo/proj-a", "bible_root": "bible"},
        ])

        async def contend() -> list[str]:
            return await asyncio.gather(*[
                db.resolve_project_for_cwd("/repo/proj-a/app.py") for _ in range(3)
            ])

        assert asyncio.run(contend()) == ["proj-a"] * 3
        db._projects_cache = None  # force the second loop to contend the lock too
        assert asyncio.run(contend()) == ["proj-a"] * 3, (
            "resolution failed in a second event loop; the lock is bound to the first"
        )
        assert db.get_projects_calls == 2, (
            f"expected one read per loop ({db.get_projects_calls} seen), so both loops "
            f"single-flighted rather than one of them stampeding"
        )

    @pytest.mark.asyncio
    async def test_the_unbound_call_shape_still_works(self) -> None:
        """The constraint that shaped the original inline cache, pinned so a
        single-flight fix cannot break it: resolve_project_for_cwd is invoked
        UNBOUND against an object that supplies only get_projects (the fixture
        class in this very file does it), so the lock has to be created lazily on
        `self` rather than in an __init__ this mixin does not own."""
        from writ.graph.db.project_store import ProjectStoreMixin

        class _BareStub:
            def __init__(self) -> None:
                self.calls = 0

            async def get_projects(self) -> list[dict]:
                self.calls += 1
                return [{"name": "proj-a", "repo_root": "/repo/proj-a", "bible_root": "b"}]

        stub = _BareStub()
        name = await ProjectStoreMixin.resolve_project_for_cwd(stub, "/repo/proj-a/x.py")

        assert name == "proj-a"
        assert stub.calls == 1

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


class TestOneProjectRootAnswerPerHook:
    """writ-rag-inject.sh computes `_PROJECT_ROOT` once near the top and every
    project_root sender added by the scoping cycle reads it. The `/recall` request
    body predates that hoist by three weeks and built its own answer from `$PWD`.

    Two things make the divergence a defect rather than a style wrinkle. `$PWD` is
    bash's LOGICAL cwd (symlinked components unresolved) while `_PROJECT_ROOT` comes
    from `detect_project_root "$(pwd -P)"`, and /recall feeds the value to
    resolve_project_for_cwd, a raw string longest-prefix comparison against the
    registered repo_root -- a logical path resolves to NO project, which is now an
    empty briefing. And in a nested-repo tree the two can resolve to DIFFERENT
    projects: the retrieval requests carry the inner repo root while a deep `$PWD`
    prefix-matches the registered OUTER project, so one hook invocation would scope
    rules to one project and brief decisions from another.
    """

    def test_the_recall_request_uses_the_hoisted_project_root(self) -> None:
        source = (REPO_ROOT / "hooks" / "scripts" / "writ-rag-inject.sh").read_text()
        # Anchored on the assignment and the POST that consumes it, NOT on a
        # fixed-size window before the first "/recall" in the file: prose mentioning
        # the route (a comment, a docstring) would silently move that window off the
        # code and the assertions below would stop reading anything.
        start = source.find("RECALL_REQ=")
        assert start != -1, "writ-rag-inject.sh no longer builds a RECALL_REQ body"
        end = source.find('/recall"', start)
        assert end > start, "the RECALL_REQ body is no longer followed by the /recall POST"
        window = source[start:end]
        assert "_PROJECT_ROOT" in window, (
            "the /recall request body does not use the hook's single _PROJECT_ROOT "
            "answer, so this hook can send one project root to /prompt-bundle and a "
            "different one to /recall"
        )
        assert 'WRIT_ROOT="${PWD}"' not in window, (
            "the /recall request body still builds its project root from $PWD (the "
            "logical cwd, symlinks unresolved, and a subdirectory rather than the "
            "root); reuse _PROJECT_ROOT"
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
