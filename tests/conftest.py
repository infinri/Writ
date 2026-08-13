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

# GRAPH ISOLATION (cycle 8), forced here for exactly the same reason WRIT_PORT is:
# it has to happen before pytest imports a single test module. At least seventeen
# modules bind `NEO4J_URI = get_neo4j_uri()` at their OWN import
# (test_authoring.py:21, test_infrastructure.py:18, test_graph_dump.py:20,
# test_db_category.py:23, test_retrieval.py:31, ...), and pytest imports the rootdir
# conftest first, so an override applied any later is read by a module that already
# cached the production URI. writ/config.py caches nothing (get_neo4j_uri reads
# os.environ, then writ.toml, on every call), so the assignment takes effect
# immediately, and every subprocess the suite spawns inherits it and re-resolves
# through the same path.
#
# WHY THE SUITE NEEDS ITS OWN INSTANCE AT ALL: ~45 test files call clear_all, and
# "which server is the suite pointed at" is ONE fact, where "is every one of those 45
# call sites safe" is 45 facts that go stale the next time somebody adds a fixture.
#
# tests/_graph.py keeps a stdlib-only module top level and does every writ.* import
# inside a function, so this line costs no driver import at conftest time.
from tests._graph import apply_isolation_env  # noqa: E402

WRIT_SUITE_IS_ISOLATED = apply_isolation_env(_os.environ)

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

    Cycle 8: skipped entirely on an isolated run. This restore exists to repair
    the PRODUCTION corpus the suite used to wipe on every run; on a throwaway
    instance there is nothing to repair, the next run's preflight refills it
    from the same dump, and dropping it removes one full replay from every run.
    It could not restore production during an isolated run in any case, since
    it inherits the redirected environment -- so skipping removes the question
    rather than answering it. Kept exactly as-is under WRIT_TEST_NO_ISOLATION=1.
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

    if not WRIT_SUITE_IS_ISOLATED:
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


def _preflight_isolated_graph() -> None:
    """Refuse to start a run that cannot be isolated. (cycle 8)

    Three refusals, all before the first test:

      * the resolved URI IS the production (host, port). From inside the process
        a leaking run looks exactly like a healthy one: a graph that answers.
        This is the transport-independent half of what the deleted tripwire
        attempted per connection, and it is stronger, because it inspects the
        CONFIGURATION every transport reads rather than one class's constructor.
        Note that this branch never opens a connection at all -- if the target
        is production, the correct amount of traffic to send it is none.
      * the disposable instance is not answering. The suite does NOT fall back
        to production and does NOT quietly mass-skip. An empty-or-absent graph
        reading as a skip is the masking class tests/_corpus.py exists to
        forbid: a mass skip is cheap to produce and indistinguishable from a
        green run, and this suite has paid for that lesson twice. The accepted
        cost, stated plainly: without docker you cannot run even the pure unit
        tests until you set WRIT_TEST_NO_ISOLATION=1 once, and the message names
        the variable.
      * the instance answers but the corpus warm left it incomplete, so the
        anti-masking contract ("reachable but empty must FAIL") is honoured
        once, loudly, with the per-label census attached, instead of by two
        hundred individual failures with no cause on them.

    Every graph read happens in a worker thread, for the reason the bible/ warm
    below already documents: calling asyncio.run on the MAIN thread here, before
    pytest's event-loop policy is set up, leaves the main thread's current loop
    unset on 3.12 and breaks later asyncio.get_event_loop() users. The refusing
    happens on the main thread.
    """
    import concurrent.futures

    from tests._corpus import ensure_corpus, is_complete, methodology_counts, neo4j_reachable
    from tests._graph import (
        STATE_ISOLATED,
        classify_isolation,
        isolation_refusal_message,
        resolved_uri,
        targets_production,
    )

    uri = resolved_uri()
    is_production = targets_production(uri)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        # Deliberately not probed when the target is production: classify_isolation
        # reports production-target regardless of reachability, so the probe would
        # buy nothing and would be one connection to the graph we are protecting.
        reachable = False
        if not is_production:
            try:
                reachable = ex.submit(neo4j_reachable).result(timeout=30)
            except Exception:  # noqa: BLE001
                reachable = False

        # WRIT_SUITE_IS_ISOLATED is already False when opted out, and this function
        # is not called in that case -- so opted_out is False by construction here.
        state = classify_isolation(
            opted_out=False, is_production=is_production, reachable=reachable
        )
        if state != STATE_ISOLATED:
            raise pytest.UsageError(f"graph isolation: {state}\n{isolation_refusal_message(uri)}")

        # Warm a cold instance. ensure_corpus is a no-op when the graph is already
        # complete (one census read), so a warm instance costs nothing here.
        try:
            ex.submit(ensure_corpus).result(timeout=180)
        except Exception:  # noqa: BLE001
            pass  # the completeness check below is the verdict, not this call

        # Refuse rather than propagate: any exception escaping pytest_sessionstart
        # that is not a UsageError becomes an INTERNALERROR with a traceback and no
        # remedy on it. An instance that answered a moment ago and cannot be
        # censused now is the unreachable case arriving late, so it gets the
        # unreachable refusal.
        try:
            counts = ex.submit(methodology_counts).result(timeout=60)
        except Exception as exc:  # noqa: BLE001
            raise pytest.UsageError(
                f"graph isolation: census read failed ({exc})\n"
                f"{isolation_refusal_message(uri)}"
            ) from exc

    if not is_complete(counts):
        raise pytest.UsageError(
            "graph isolation: corpus incomplete after warm\n"
            f"{isolation_refusal_message(uri, counts=counts)}"
        )


def pytest_sessionstart(session):
    """INC-1: begin the suite from a complete graph (symmetric to sessionfinish).

    Without this, the first graph-dependent test runs against whatever stale/partial state
    the previous run or a sibling fixture left in the shared Neo4j. A single MERGE-only
    `import-markdown bible/` (idempotent, <2s) guarantees a known-complete starting point so
    a green suite cannot hide a graph-state regression as a skip.

    POL-2b/E3: skip the import when the graph is ALREADY complete -- the MERGE would be a no-op,
    so the complete-start guarantee still holds and we save ~2-4s on warm runs. An empty/partial
    graph (or any Neo4j error) is NOT warm and still triggers the import.

    Cycle 8: on an isolated run the isolation preflight REPLACES the bible/ warm below
    rather than joining it -- running both would ingest the same corpus twice by two
    different routes on every run, and the preflight both warms and verifies. Under
    WRIT_TEST_NO_ISOLATION=1 the bible/ path below is reached unchanged, early return
    and all.
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

    if WRIT_SUITE_IS_ISOLATED:
        # Refuse a run that cannot be isolated, and warm the disposable instance from
        # the tracked dump. Runs BEFORE any bible/ check on purpose: a clean checkout
        # has no bible/ tree, and it must still be refused rather than silently
        # allowed to proceed against whatever answers.
        _preflight_isolated_graph()
    else:
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
