"""3.6a: mandatory retrieval coupling -- UNION injection + loud validate invariant.

WRIT-BLUEPRINT 3.5/3.6a. The bug: 29 of 32 mandatory rules reached the agent via
NEITHER the ranked pool (they are excluded by design) NOR the /always-on
injection bundle (which keyed on `always_on`, not `mandatory`) -- a silent
no-op-gate on security-critical rules. The fix:

- /always-on selects the UNION `mandatory OR always_on` (single-source predicate
  shared by the endpoint and the validator so they cannot drift).
- mandatory is EXEMPT from the process-domain mode-strip (server.py), so all
  mandatory rules inject in every mode; non-mandatory always-on process rules
  (e.g. ENF-PROC-DEBUG-001) keep their existing strip behavior.
- `writ validate` fails loud if any mandatory rule is stranded, if
  {excluded-from-ranked} != {mandatory}, or if the summary bundle exceeds the cap.

Endpoint tests hit the LIVE daemon (skip if unreachable). Validator tests run the
IntegrityChecker against the live corpus, read-only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest
import pytest_asyncio

from tests._daemon import _port
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

from tests._bible_guard import requires_bible

pytestmark = requires_bible


SERVER = f"http://localhost:{_port()}"

# The pre-fix injection selection. The validator, fed this, must still report the
# stranded set -- proving it is not a predicate-minus-itself tautology and can
# never silently pass if someone reverts the endpoint to this predicate.
OLD_PREDICATE = "r.always_on = true"

# A representative slice of the 29 stranded mandatory rules (all severity=critical
# security rules). If the union fix regresses, these are the casualties.
KNOWN_STRANDED = {"SEC-AUTH-HASH-001", "SEC-AUTHZ-DEFAULT-001", "ENF-SEC-001"}


def _get_always_on(mode: str | None = None) -> dict:
    url = f"{SERVER}/always-on" + (f"?mode={mode}" if mode else "")
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, OSError) as e:
        pytest.skip(f"Writ server unreachable: {e}")


@pytest_asyncio.fixture()
async def conn(corpus_ready):
    c = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    yield c
    await c.close()


@pytest_asyncio.fixture()
async def mandatory_ids(conn: Neo4jConnection) -> set[str]:
    async with conn._driver.session(database=conn._database) as s:
        result = await s.run(
            "MATCH (r:Rule) WHERE r.mandatory = true RETURN r.rule_id AS id"
        )
        return {rec["id"] async for rec in result}


class TestMandatoryInjectionEndpoint:
    """Live-daemon: every mandatory rule reaches the agent via /always-on.

    RED before the union fix (29 mandatory missing); GREEN after the endpoint is
    wired to the union predicate and the daemon is restarted.
    """

    @pytest.mark.asyncio
    async def test_all_mandatory_in_universal_bundle(self, mandatory_ids: set[str]) -> None:
        data = _get_always_on()  # universal: no mode-strip applied
        ids = {r["rule_id"] for r in data.get("rules", [])}
        missing = mandatory_ids - ids
        assert not missing, (
            f"{len(missing)} mandatory rules stranded from /always-on: {sorted(missing)}"
        )

    @pytest.mark.asyncio
    async def test_all_mandatory_in_conversation_mode(self, mandatory_ids: set[str]) -> None:
        # Mandatory is EXEMPT from the process-domain mode-strip: all mandatory
        # rules inject even in conversation (no-code) mode.
        data = _get_always_on("conversation")
        ids = {r["rule_id"] for r in data.get("rules", [])}
        missing = mandatory_ids - ids
        assert not missing, f"mandatory stranded in conversation mode: {sorted(missing)}"

    def test_advisory_process_rule_still_stripped_in_conversation(self) -> None:
        # ENF-PROC-DEBUG-001 is always_on + process + NOT mandatory: the strip
        # must still remove it outside work/debug (we exempt mandatory only).
        data = _get_always_on("conversation")
        ids = {r["rule_id"] for r in data.get("rules", [])}
        assert "ENF-PROC-DEBUG-001" not in ids, (
            "non-mandatory process advisory leaked into conversation mode"
        )

    def test_render_mode_summary_and_under_cap(self) -> None:
        # 3.6a2: render pinned to summary-form (trigger+statement); bundle under
        # cap. A full-prose refactor (~205% of cap) breaks total<cap loud.
        data = _get_always_on("work")
        assert data.get("render_mode") == "summary"
        cap = data.get("cap", 5000)
        total = data.get("total_tokens", 0)
        assert 0 < total < cap, f"bundle blew the cap: {total} >= {cap}"
        for r in data.get("rules", []):
            trig = (r.get("trigger") or "").strip()
            stmt = (r.get("statement") or "").strip()
            assert r.get("est_tokens") == (len(trig) + len(stmt)) // 4, (
                f"{r['rule_id']} render is not summary-form (trigger+statement only)"
            )


class TestMandatoryValidator:
    """writ-validate invariant against the live corpus."""

    @pytest.mark.asyncio
    async def test_union_predicate_strands_nobody(self, conn: Neo4jConnection) -> None:
        checker = IntegrityChecker(conn._driver, conn._database)
        stranded = await checker.detect_stranded_mandatory()  # default = union
        assert stranded == [], f"union injection still strands mandatory rules: {stranded}"

    @pytest.mark.asyncio
    async def test_old_always_on_predicate_strands_mandatory(self, conn: Neo4jConnection) -> None:
        # Non-vacuity: fed the OLD predicate, the validator reports the stranded
        # set (proves it detects the 29-bug, not a tautology).
        checker = IntegrityChecker(conn._driver, conn._database)
        stranded = await checker.detect_stranded_mandatory(OLD_PREDICATE)
        assert len(stranded) >= 20, (
            f"expected ~29 mandatory stranded under always_on-only, got {len(stranded)}"
        )
        assert KNOWN_STRANDED <= set(stranded), (
            f"known stranded security rules not detected: {KNOWN_STRANDED - set(stranded)}"
        )

    @pytest.mark.asyncio
    async def test_ranked_exclusion_equals_mandatory(self, conn: Neo4jConnection) -> None:
        checker = IntegrityChecker(conn._driver, conn._database)
        assert await checker.detect_ranked_exclusion_mismatch() is None

    @pytest.mark.asyncio
    async def test_budget_under_cap(self, conn: Neo4jConnection) -> None:
        checker = IntegrityChecker(conn._driver, conn._database)
        assert await checker.detect_always_on_budget_breach() is None

    @pytest.mark.asyncio
    async def test_budget_guard_fires_when_over_cap(self, conn: Neo4jConnection) -> None:
        # Non-vacuity: a tiny cap must trip the guard.
        checker = IntegrityChecker(conn._driver, conn._database)
        breach = await checker.detect_always_on_budget_breach(cap=1)
        assert breach is not None and breach["total_tokens"] > 1

    @pytest.mark.asyncio
    async def test_run_all_checks_includes_3_6a_findings(self, conn: Neo4jConnection) -> None:
        checker = IntegrityChecker(conn._driver, conn._database)
        findings = await checker.run_all_checks()
        assert findings["stranded_mandatory"] == []
        assert findings["ranked_exclusion_mismatch"] is None
        assert findings["always_on_budget_breach"] is None
