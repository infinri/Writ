"""Shared test fixtures for Writ test suite."""

from __future__ import annotations

import os as _os
import tempfile as _tempfile

import pytest

# F4b/option C: the suite runs its OWN daemon on a dedicated port, never the
# interactive 8765 singleton. Forcing WRIT_PORT here at conftest import (before any
# test or tests._daemon import) routes every port resolution to the test daemon:
# tests._daemon._port(), and the hook/_writ_session curls that inherit WRIT_PORT via
# subprocess env. This eliminates the shared-daemon bug class (cache desync, the F4
# friction bleed) -- the interactive daemon is structurally untouched by the suite.
TEST_DAEMON_PORT = "8799"
_os.environ["WRIT_PORT"] = TEST_DAEMON_PORT

# Force WRIT_CACHE_DIR to a session-owned temp dir, at import (before any test or
# subprocess). The session-cache default moved off /tmp to <skill>/var/session so it
# survives a reboot -- but that made the install dir the fallback, so any subprocess
# test that does NOT set WRIT_CACHE_DIR (many build their own env from os.environ)
# would now write real session caches into var/session, polluting live state. /tmp
# used to absorb those harmlessly. This restores that: a stable non-production dir
# for the whole run (the daemon reads it once at start via expected_cache_dir(), so
# it must not change per-test), off the install tree. Tests that set their own
# WRIT_CACHE_DIR via monkeypatch still override it and monkeypatch restores this
# default afterward. mkdtemp (not a fixed name) so parallel `pytest` invocations do
# not share one dir.
_os.environ.setdefault("WRIT_CACHE_DIR", _tempfile.mkdtemp(prefix="writ-test-cache-"))

# Never let a hook auto-spawn a daemon during the suite. writ-rag-inject.sh
# auto-starts the Writ server when its health check fails, guarded by
# WRIT_NO_AUTOSTART. Tests that invoke that hook with a deliberately-unreachable
# WRIT_PORT (e.g. 19999) but forget to set this guard cause it to LAUNCH a real
# daemon on that port -- which then outlives the run holding a deleted pytest
# tmpdir as its cache, answers {"mode":""} for every session, and silently
# disables mode-gated hooks in later runs (the order-dependent failures). The
# suite starts its own daemon explicitly (start_test_daemon on WRIT_PORT), so no
# hook ever needs to; forcing this closes the whole leak class rather than
# per-test. Individual per-test settings become redundant but harmless.
_os.environ["WRIT_NO_AUTOSTART"] = "1"


def writ_server_source() -> str:
    """Layout-agnostic reader for the `writ.server` module/package source text.

    Wave 2 Cycle 1 (branch refactor/w2-server-split) turns writ/server.py from a
    single 2186-line module into a writ/server/ package (routes/*.py + models.py +
    __init__.py facade). Content-based source-scan tests (grep-for-a-string /
    forbidden-literal assertions) care about WHAT the server code contains, not
    WHERE it lives, so they read through this helper: it concatenates every *.py
    under writ/server/ if that directory exists (post-split), else falls back to
    reading the single writ/server.py file (pre-split). This keeps those tests
    GREEN across the refactor -- only tests/test_server_split_seam.py (and the
    endpoint-count test, which needs the per-route decorator granularity) assert
    on the package LAYOUT itself.
    """
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parent.parent
    pkg_dir = repo_root / "writ" / "server"
    if pkg_dir.is_dir():
        return "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(pkg_dir.rglob("*.py"))
        )
    return (repo_root / "writ" / "server.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_friction_log(request, tmp_path, monkeypatch):
    """Redirect every friction writer to a per-test tmp log so the suite never
    pollutes the repo's workflow-friction.log (Phase 1.2).

    Points WRIT_FRICTION_LOG at `tmp_path / "workflow-friction.log"`, which is the
    dominant location emit-then-read tests already use, so they keep passing while
    leak-only tests stop polluting the repo log. Subprocesses (hooks, the daemon)
    inherit the env var, so all writers route there.

    Files that exercise path RESOLUTION itself (marker-walk, unwritable-path
    fallback) opt out via `pytestmark = pytest.mark.no_friction_isolation`.
    """
    # Sandbox the P1 router's central log root for EVERY test (including
    # no_friction_isolation): when WRIT_FRICTION_LOG is unset the router writes to
    # ~/.claude/writ/logs/<project>/<stream>.jsonl, so without this a test would leak
    # real events into the operator's home log store. Separate concern from the
    # WRIT_FRICTION_LOG isolation below.
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    if request.node.get_closest_marker("no_friction_isolation"):
        # These tests assert on marker-walk / unwritable-path resolution, so force
        # the env var OFF (a stray session-level value would defeat that).
        monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
        yield
        return
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(tmp_path / "workflow-friction.log"))
    yield


def pytest_sessionfinish(session, exitstatus):
    """Re-migrate rules after test suite completes so CLI queries work
    immediately.

    Pre-2026-05-09 this hook had inline migration logic gated on
    `if count == 0`. That gate skipped re-migration whenever ANY test
    re-loaded core rules (most do), leaving methodology nodes
    (Skill / Playbook / etc.) missing post-suite -- the symptom was
    `/always-on?mode=work` returning empty after `pytest -q`.

    New approach: shell out to `writ import-cypher writ-corpus.cypher`
    unconditionally. `bible/` is no longer shipped/tracked (writ-corpus.cypher
    is); the command replays the shipped dump and is the canonical import
    path used in production. Single source of truth -- the inline duplicate
    is gone.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    from tests._writ_cmd import WRIT_CMD_PREFIX

    skill_dir = Path(__file__).resolve().parent.parent
    dump_file = skill_dir / "writ-corpus.cypher"
    if not dump_file.exists():
        return  # not a writ checkout; nothing to restore.

    try:
        subprocess.run(
            [*WRIT_CMD_PREFIX, "import-cypher", "writ-corpus.cypher"],
            cwd=str(skill_dir),
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        # Neo4j may not be running, migrate.py may have changed
        # signature, etc. End-of-suite is best-effort -- we don't
        # raise out of pytest_sessionfinish because doing so flips
        # exitstatus and masks the actual test results.
        pass

    # F4b/option C: stop the suite's dedicated test daemon (on the test WRIT_PORT) so it
    # is not left running. Supersedes F4's restore-the-shared-daemon dance: the suite never
    # touched the interactive 8765 daemon, so there is nothing to restore. Best-effort.
    try:
        from tests._daemon import stop_test_daemon

        stop_test_daemon()
    except Exception:  # noqa: BLE001
        pass


def pytest_sessionstart(session):
    """INC-1: begin the suite from a complete graph (symmetric to sessionfinish).

    Without this, the first graph-dependent test runs against whatever stale/partial state
    the previous run or a sibling fixture left in the shared Neo4j. A single MERGE-only
    `import-markdown bible/` (idempotent, <2s) guarantees a known-complete starting point so
    a green suite cannot hide a graph-state regression as a skip.

    POL-2b/E3: skip the import when the graph is ALREADY complete -- the MERGE would be a no-op,
    so the complete-start guarantee still holds and we save ~2-4s on warm runs. An empty/partial
    graph (or any Neo4j error) is NOT warm and still triggers the import.
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from tests._writ_cmd import WRIT_CMD_PREFIX

    # Phase 1.2: redirect the shared test daemon's friction telemetry off the repo
    # log for the whole session. Per-test in-process writers are isolated by the
    # autouse _isolate_friction_log fixture; this covers events the daemon emits for
    # test sessions (daemon-first _writ_session calls). setdefault respects an explicit
    # override. ensure_daemon_aligned() below restarts the daemon onto this path.
    os.environ.setdefault(
        "WRIT_FRICTION_LOG",
        os.path.join(tempfile.gettempdir(), "writ-test-daemon-friction.log"),
    )

    root = Path(__file__).resolve().parent.parent
    if not (root / "bible").exists():
        return

    # Run the warmth probe (which uses asyncio.run for its Neo4j queries) in a worker thread.
    # Calling asyncio.run on the MAIN thread here -- before pytest's event-loop policy is set up
    # -- leaves the main-thread "current loop" unset on 3.12, which breaks later tests that use
    # the legacy asyncio.get_event_loop(). The worker thread isolates that side effect.
    import concurrent.futures

    try:
        from tests._corpus import graph_is_warm

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            warm = ex.submit(graph_is_warm).result(timeout=30)
    except Exception:  # noqa: BLE001
        warm = False

    if not warm:
        try:
            subprocess.run(
                [*WRIT_CMD_PREFIX, "import-markdown", "bible/", "--no-export"],
                cwd=str(root),
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            pass

    # F4b/option C: isolation is achieved by forcing WRIT_PORT to the dedicated test
    # port at conftest import -- the suite NEVER targets the interactive 8765 daemon.
    # We deliberately do NOT start a session-wide test daemon here: a second (cold)
    # daemon adds background CPU that tips fragile perf floors (test_retrieval's 15ms
    # p95), and daemon-dependent tests already degrade gracefully (skip / subprocess
    # fallback) when no daemon answers on the test port -- same as CI without a daemon.
    # ensure_daemon_aligned() realigns ONLY if a daemon is already up on the test port
    # (e.g. a leftover or a module-scoped fixture started one); otherwise it is a no-op.
    try:
        from tests._daemon import ensure_daemon_aligned

        ensure_daemon_aligned()
    except Exception:  # noqa: BLE001
        pass


# --- POL-2: shared, session-scoped methodology-benchmark fixtures ----------------------------
# The corpus is loaded and (expensively) encoded ONCE for the whole suite, instead of per-test
# inside each INC file's bundle_completeness check. Heavy imports are lazy (inside the fixtures)
# so non-benchmark tests and onnxruntime-absent envs are unaffected.


@pytest.fixture(scope="session")
def methodology_corpus():
    from tests.fixtures.methodology_loader import load_corpus

    return load_corpus()


@pytest.fixture(scope="session")
def methodology_ground_truth() -> dict:
    from tests.fixtures.methodology_loader import load_ground_truth

    return load_ground_truth()


@pytest.fixture(scope="session")
def methodology_kindex(methodology_corpus):
    from tests.fixtures.methodology_loader import build_methodology_index

    return build_methodology_index(methodology_corpus)


@pytest.fixture(scope="session")
def methodology_adjacency(methodology_corpus) -> dict:
    from tests.fixtures.methodology_loader import build_adjacency

    return build_adjacency(methodology_corpus)


@pytest.fixture(scope="session")
def methodology_model():
    pytest.importorskip("onnxruntime")
    from writ.retrieval.embeddings import CachedEncoder, OnnxEmbeddingModel

    return CachedEncoder(OnnxEmbeddingModel())


@pytest.fixture(scope="session")
def methodology_node_vectors(methodology_corpus, methodology_model) -> dict:
    """One encode_batch over the retrievable corpus, shared by every benchmark test."""
    import numpy as np

    retrievable = [n for n in methodology_corpus if n.is_retrievable]
    vecs = methodology_model.encode_batch([f"{n.trigger} {n.statement}" for n in retrievable])
    return {n.node_id: np.asarray(vecs[i], dtype=np.float32) for i, n in enumerate(retrievable)}


@pytest.fixture(scope="session")
def live_pipeline():
    """Shared build_pipeline over the live Neo4j graph (POL-2: was duplicated in INC-9..12).

    Skips when Neo4j is unreachable; self-heals a partial graph via ensure_corpus first.
    """
    import asyncio

    from tests._corpus import _connection, ensure_corpus, neo4j_reachable

    if not neo4j_reachable():
        pytest.skip("Neo4j unreachable")
    ensure_corpus()
    from writ.retrieval.pipeline import build_pipeline

    async def _build():
        db = _connection()
        try:
            return await build_pipeline(db)
        finally:
            await db.close()

    return asyncio.run(_build())


@pytest.fixture()
def disposable_graph():
    """Gate a test that performs a whole-graph wipe on an explicitly disposable Neo4j.

    Skips, with the exact commands to stand one up, when the connected instance is
    not marked disposable. A test that wipes everything (`clear_all` with an empty
    preserve set) destroys the runtime records -- Memory, Decision, FileChange,
    Commit -- and a Decision record has no bible/ or dump source to rebuild from.
    Running the suite against the interactive instance did exactly that.

    This fixture is the operator-facing half of the protection, NOT the protection
    itself. The enforcement lives in `clear_all` (writ/graph/db/_safety.py), because
    a fixture can only defend the tests that remember to request it, and the two
    fixtures that caused the incident would each have had to remember. What this
    adds is a clean, actionable skip instead of an error traceback: without it the
    guard still refuses, it just refuses less legibly.

    Verified by tests/test_graph_wipe_guard.py, including that a stubbed
    always-allow guard makes the refusal test fail.
    """
    from writ.config import get_neo4j_uri
    from writ.graph.db._safety import full_wipe_allowed, how_to_run_safely

    if not full_wipe_allowed(get_neo4j_uri()):
        pytest.skip(
            "destructive whole-graph test: no disposable Neo4j instance configured.\n"
            + how_to_run_safely()
        )
    yield


@pytest.fixture()
def corpus_ready():
    """INC-1: guarantee the live graph holds the full methodology corpus before a
    graph-dependent test, self-healing a wiped/partial graph (re-import bible/) rather than
    letting the test skip on an empty graph (the FIX-5 masking class). Skips ONLY when Neo4j
    is genuinely unreachable -- never on 'graph empty'."""
    from tests._corpus import ensure_corpus, neo4j_reachable

    if not neo4j_reachable():
        pytest.skip("Neo4j unreachable")
    ensure_corpus()
    yield


@pytest.fixture()
def valid_rule_data() -> dict:
    """A well-formed rule with all required fields."""
    return {
        "rule_id": "ARCH-ORG-001",
        "domain": "Architecture",
        "severity": "critical",
        "scope": "component",
        "trigger": "When creating a class that contains logic from a different layer.",
        "statement": "Each class must belong to exactly one architectural layer.",
        "violation": "Controller contains SQL query.",
        "pass_example": "Controller delegates to service, service delegates to repository.",
        "enforcement": "Per-slice findings table must verify layer separation.",
        "rationale": "Mixed layers create untestable, unreusable, fragile classes.",
        "last_validated": "2026-03-15",
    }


@pytest.fixture()
def valid_enf_rule_data() -> dict:
    """An ENF-* rule with mandatory=true."""
    return {
        "rule_id": "ENF-GATE-001",
        "domain": "AI Enforcement",
        "severity": "critical",
        "scope": "session",
        "trigger": "When the AI completes Phase A analysis.",
        "statement": "Phase A output must be approved before Phase B begins.",
        "violation": "AI proceeds to Phase B without human approval of Phase A.",
        "pass_example": "AI halts after Phase A and waits for approval.",
        "enforcement": "Gate file must exist before Phase B output is generated.",
        "rationale": "Human review catches incorrect call-path declarations.",
        "mandatory": True,
        "last_validated": "2026-03-15",
    }


@pytest.fixture()
def minimal_rule_data() -> dict:
    """Rule with only required fields -- graph-only fields use defaults."""
    return {
        "rule_id": "TEST-TDD-001",
        "domain": "Testing",
        "severity": "high",
        "scope": "slice",
        "trigger": "When generating implementation code for a new class.",
        "statement": "Test skeletons must exist before the implementation they test.",
        "violation": "Implementation written first, tests added after.",
        "pass_example": "Test skeleton written and approved before implementation.",
        "enforcement": "ENF-GATE-007 test-first gate.",
        "rationale": "Tests written after implementation confirm what was built, not what should be built.",
        "last_validated": "2026-03-15",
    }


@pytest.fixture()
def compound_id_rule_data(valid_rule_data: dict) -> dict:
    """Rule with a multi-segment ID like FW-M2-RT-003."""
    return {**valid_rule_data, "rule_id": "FW-M2-RT-003"}


@pytest.fixture()
def enf_gate_final_data(valid_rule_data: dict) -> dict:
    """Rule with non-numeric suffix: ENF-GATE-FINAL."""
    return {**valid_rule_data, "rule_id": "ENF-GATE-FINAL"}


# ---------------------------------------------------------------------------
# Production-graph tripwire (isolation cycle, 2026-08-08)
# ---------------------------------------------------------------------------
#
# WHY A TRIPWIRE AND NOT A REVIEW. Pointing the suite at a disposable instance via
# WRIT_NEO4J_URI mostly works, and "mostly" is the problem: on 2026-08-08 a full run with
# the redirect exported still emptied the production corpus (Rule 287 -> 0), while the two
# files most suspected of it were provably clean when run alone. A leak that only appears
# in a 200-file run cannot be found by reading, and every hour spent auditing call sites
# gives an answer that goes stale the next time someone adds a test.
#
# So the connection itself refuses. When the suite is explicitly running isolated
# (WRIT_TEST_GRAPH=1 plus a WRIT_NEO4J_URI that is not production), any Neo4jConnection
# opened against the production host:port raises immediately, and the error names the test
# that did it. The leak stops being a silent wipe and becomes one failing test with an
# address on it.
#
# In-process only, by construction: a subprocess gets its own interpreter and is covered
# instead by inheriting the redirected environment. That is why the message says which of
# the two applies.
@pytest.fixture(autouse=True, scope="session")
def _refuse_production_graph_when_isolated():
    import os as _o

    if _o.environ.get("WRIT_TEST_GRAPH") != "1" or not _o.environ.get("WRIT_NEO4J_URI"):
        yield  # not an isolated run; the suite owns the default instance as before
        return

    from writ.config import get_production_neo4j_uri
    from writ.graph.db import Neo4jConnection

    def _hostport(uri):
        from urllib.parse import urlparse

        p = urlparse(uri if "://" in uri else f"bolt://{uri}")
        return (p.hostname or "").lower(), p.port or 7687

    production = _hostport(get_production_neo4j_uri())
    original = Neo4jConnection.__init__

    def guarded(self, uri, *a, **kw):
        if _hostport(uri) == production:
            raise AssertionError(
                f"test opened the PRODUCTION graph at {uri} during an isolated run "
                f"(WRIT_NEO4J_URI={_o.environ['WRIT_NEO4J_URI']}). Something resolved the "
                f"URI without the env override, or hardcoded it. Fix the call site; do not "
                f"relax this guard."
            )
        return original(self, uri, *a, **kw)

    Neo4jConnection.__init__ = guarded
    try:
        yield
    finally:
        Neo4jConnection.__init__ = original
