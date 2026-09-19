"""FIX-3: self-reporting metric bugs.

#5 -- /health silently reported rule_count:0 over a DB/index split (a FIX-2 stale-daemon
symptom). count_rules() is correct; FIX-3 adds a self-detecting `degraded` status so the
split (index warm, but Neo4j count 0) can never again read as a bland "healthy".

#4 -- the integrity staleness query referenced `last_seen`, a telemetry field never set on a
fresh corpus (0/280), causing a Neo4j UnknownPropertyKey warning + a useless all-rules "stale"
dump. The clause is dead (gated behind times_seen==0). FIX-3 drops it.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tests.conftest import writ_server_source

SKILL = Path(__file__).resolve().parent.parent
CLI_PY = SKILL / "writ" / "cli.py"
VALIDATE_REPORT_PY = SKILL / "writ" / "graph" / "validate_report.py"


def _integrity_source() -> str:
    """integrity is a single file OR (Wave-2 split) a writ/graph/integrity/
    package; read whichever exists so this source-scan is layout-agnostic."""
    integ_dir = SKILL / "writ" / "graph" / "integrity"
    if integ_dir.is_dir():
        return "\n".join(p.read_text() for p in sorted(integ_dir.glob("*.py")))
    return (SKILL / "writ" / "graph" / "integrity.py").read_text()


@pytest.fixture(scope="module")
def owned_daemon(tmp_path_factory):
    """OWNED DAEMON over HTTP (Decision 4, plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3): this module's /health assertion
    turns on `rule_count` (a live DB count) and `index_state` (the pipeline the
    LIFESPAN builds), so an ASGI transport cannot reach it -- only a real
    daemon PROCESS can. The Rule population precondition runs BEFORE the
    start, in this module's OWN fixture, because the daemon indexes once at
    startup and a repair afterwards is invisible to it.
    """
    from tests._corpus import require_population
    from tests._hook_runner import instrumented_daemon

    require_population(
        "MATCH (r:Rule) RETURN count(r)",
        "the Rule population /health's rule_count and status conjunction depend on",
    )
    tmp_dir = tmp_path_factory.mktemp("fix3")
    with instrumented_daemon(tmp_dir) as daemon:
        yield daemon


class TestHealthStatusHelper:
    def test_degraded_when_warm_but_zero(self) -> None:
        from writ.server import _health_status
        assert _health_status(rule_count=0, index_warm=True) == "degraded"

    def test_healthy_when_warm_and_rules(self) -> None:
        from writ.server import _health_status
        assert _health_status(280, True) == "healthy"

    def test_healthy_when_cold(self) -> None:
        from writ.server import _health_status
        assert _health_status(0, False) == "healthy"
        assert _health_status(280, False) == "healthy"


class TestHealthLive:
    """OWNED DAEMON over HTTP (Decision 4/5, plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3, module 5). Converts the module's
    two-skip-site test: a reachable daemon serving an empty graph used to
    read as a skip ("degraded by design; not the loaded case"), the exact
    FIX-5 masking class this test was written to detect. The corpus
    precondition (non-empty Rule count) now runs BEFORE the start and FAILS
    loud instead.
    """

    def test_loaded_warm_daemon_reports_the_healthy_conjunction(self, owned_daemon) -> None:
        h = owned_daemon["health"]
        assert (
            h.get("status") == "healthy"
            and h.get("index_state") == "warm"
            and h.get("rule_count", 0) >= 1
        ), (
            f"a loaded, warm daemon must report the conjunction status=healthy "
            f"AND index_state=warm AND rule_count>=1, attributable to the two "
            f"inputs _health_status actually takes; got {h}"
        )
    # MUTATION: inverting _health_status's condition (writ/server/routes/
    # query.py:490-495) so a warm, loaded daemon reports 'degraded' reddens
    # this conjunction. The old form could not be reddened at all: it never ran.

    def test_server_health_has_degraded_path(self) -> None:
        # Feature marker: /health must implement the degraded status (warm + 0 rules).
        assert "degraded" in writ_server_source(), \
            "server /health must implement a 'degraded' status for the DB/index split"


class TestStalenessQueryNoLastSeen:
    def test_detect_frequency_stale_drops_last_seen(self) -> None:
        src = _integrity_source()
        m = re.search(r"def detect_frequency_stale.*?(?=\n    async def |\n    def )", src, re.S)
        assert m, "detect_frequency_stale not found"
        assert "last_seen" not in m.group(0), \
            "detect_frequency_stale must not reference the never-populated last_seen (kills Neo4j warning)"

    def test_renderer_drops_last_seen(self) -> None:
        # The frequency-stale renderer moved from cli.py to
        # writ/graph/validate_report.py (Wave 2 Cycle 3 extraction); the
        # source-text guard follows it to its new home.
        src = VALIDATE_REPORT_PY.read_text()
        idx = src.find("Frequency stale")
        assert idx != -1, "Frequency-stale renderer not found"
        assert "last_seen" not in src[idx:idx + 400], \
            "the Frequency-stale renderer must not print the meaningless last_seen"


class TestStalenessLive:
    def test_returns_rule_ids_without_last_seen(self) -> None:
        try:
            from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
            from writ.graph.db import Neo4jConnection
            from writ.graph.integrity import IntegrityChecker
        except Exception as e:  # pragma: no cover
            pytest.skip(f"imports unavailable: {e}")

        async def _run():
            db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
            try:
                return await IntegrityChecker(db._driver, db._database).detect_frequency_stale()
            finally:
                await db.close()

        try:
            rows = asyncio.run(_run())
        except Exception as e:
            pytest.skip(f"Neo4j unavailable: {e}")
        assert isinstance(rows, list)
        for row in rows[:5]:
            assert "rule_id" in row
            assert "last_seen" not in row, "frequency-stale rows must not carry last_seen after FIX-3"
