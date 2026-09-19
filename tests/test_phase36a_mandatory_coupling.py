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

Endpoint tests drive the real app in-process (httpx.AsyncClient over
ASGITransport) against a REAL connection to the isolated test Neo4j instance
(tests/fixtures/server_routes.py); no daemon process required. Validator tests
run the IntegrityChecker against the live corpus, read-only.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.graph.integrity import IntegrityChecker

from tests._bible_guard import requires_bible
from tests.fixtures.server_routes import always_on, mandatory_rule_ids, route_db

pytestmark = requires_bible

# `route_db` is named by no test below. `always_on` and `mandatory_rule_ids`
# (tests/fixtures/server_routes.py) depend on it, and a fixture dependency
# resolves only when its name is present in the REQUESTING module's
# namespace: these fixtures are imported explicitly, never registered in a
# root conftest, so nothing else makes `route_db` visible here.

# The pre-fix injection selection. The validator, fed this, must still report the
# stranded set -- proving it is not a predicate-minus-itself tautology and can
# never silently pass if someone reverts the endpoint to this predicate.
OLD_PREDICATE = "r.always_on = true"

# A representative slice of the 29 stranded mandatory rules (all severity=critical
# security rules). If the union fix regresses, these are the casualties.
KNOWN_STRANDED = {"SEC-AUTH-HASH-001", "SEC-AUTHZ-DEFAULT-001", "ENF-SEC-001"}


@pytest_asyncio.fixture()
async def conn(corpus_ready):
    c = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    yield c
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "conversation"])
async def test_all_mandatory_present(always_on, mandatory_rule_ids, mode) -> None:
    """Every mandatory rule id reaches /always-on, in the universal bundle
    (mode=None) and in conversation mode (mandatory is exempt from the
    process-domain strip).

    RED cause: reverting the injection predicate (writ/graph/predicates.py:16,
    interpolated at query.py:705) to `r.always_on = true` only, dropping the
    `r.mandatory = true OR` arm; or, for mode='conversation' specifically,
    removing the mandatory exemption from the process-domain strip
    (query.py:753-758).
    """
    assert mandatory_rule_ids, "expected at least one mandatory rule id in the live graph"
    data = await always_on(mode=mode)
    ids = {r["rule_id"] for r in data["rules"]}
    assert ids, f"expected a non-empty rules list for mode={mode!r}"
    missing = mandatory_rule_ids - ids
    assert not missing, (
        f"mandatory rules stranded from /always-on at mode={mode!r}: {sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_conversation_bundle_is_a_strict_subset_of_universal(always_on) -> None:
    """The conversation bundle is a STRICT subset of the universal bundle.

    Proves the process-domain strip is live without naming any rule id (a
    control derived from the property rather than from
    ENF-PROC-DEBUG-001, so it keeps working if that rule is renamed). The
    strip (query.py:753-758) only ever removes, so a strict inequality
    proves a removal happened, while equality would mean the strip is off.

    RED cause: the process-domain strip stops removing anything in
    conversation mode, so `conv_ids` grows to equal `universal_ids` instead
    of shrinking under it.
    """
    universal = await always_on()
    conversation = await always_on(mode="conversation")
    universal_ids = {r["rule_id"] for r in universal["rules"]}
    conv_ids = {r["rule_id"] for r in conversation["rules"]}
    assert conv_ids < universal_ids, (
        f"expected conversation ids to be a STRICT subset of universal ids; "
        f"conversation={sorted(conv_ids)} universal={sorted(universal_ids)}"
    )


@pytest.mark.asyncio
async def test_render_mode_summary_and_under_cap(always_on) -> None:
    """3.6a2: render pinned to summary-form (trigger+statement); bundle under
    cap. A full-prose refactor (~205% of cap) breaks total<cap loud.

    RED cause: the render path (query.py:772-786) stops emitting
    summary-form (trigger+statement only), est_tokens/total_tokens drifts
    from the (len(trigger)+len(statement))//4 estimate, or the cap is
    exceeded.
    """
    data = await always_on(mode="work")
    assert data["render_mode"] == "summary"
    cap = data["cap"]
    total = data["total_tokens"]
    assert 0 < total < cap, f"bundle blew the cap: {total} >= {cap}"
    for r in data["rules"]:
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
