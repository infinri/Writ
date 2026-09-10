"""always-on bundle: methodology cutover, mode scoping, and the mandatory exemption.

The /always-on endpoint historically queried only Rule (where always_on=true) and
ALL ForbiddenResponse nodes. After Phase 6 methodology absorption and the 1.7
cutover (writ/server/routes/query.py:729-736), /always-on is RULES +
ForbiddenResponse ONLY: Skill/Playbook/Technique moved entirely to
/methodology-companion. This module pins:

1. The Rule + ForbiddenResponse path still surfaces
   (test_always_on_includes_existing_rule_and_frb_nodes).
2. No SKL-/PBK-/TEC- prefixed id ever appears in /always-on, for any of five
   modes (test_no_methodology_prefix_in_any_mode).
3. The always-on bundle is non-empty and stays under its token cap, for the
   same five modes (test_bundle_is_non_empty_and_under_cap).
4. The process-domain mode-strip carve-out: ENF-PROC-DEBUG-001
   (always_on=true, domain=process, not mandatory) reaches debug and work,
   and is stripped from review and conversation, each absence paired with a
   companion mandatory-id signal so it cannot pass on an empty bundle
   (test_process_domain_advisory_follows_process_modes).

These tests drive the real FastAPI app in-process (httpx.AsyncClient over
ASGITransport) against a REAL connection to the isolated test Neo4j instance
(tests/fixtures/server_routes.py). No daemon process, no mock, and no skip on
an empty graph: the corpus precondition fails loud instead of masking as a
daemon-unreachable skip.
"""

from __future__ import annotations

import pytest

from tests.fixtures.server_routes import always_on, mandatory_rule_ids, route_db

# `route_db` is named by no test below. `always_on` and `mandatory_rule_ids`
# (tests/fixtures/server_routes.py) depend on it, and a fixture dependency
# resolves only when its name is present in the REQUESTING module's
# namespace: these fixtures are imported explicitly, never registered in a
# root conftest, so nothing else makes `route_db` visible here.

# Increment 1: debug mode must receive process-domain always-on doctrine.
# The /always-on filter strips ALL domain==process nodes in non-work modes
# unless the mode is also carved into _ALWAYS_ON_PROCESS_MODES
# (writ/server/routes/query.py:667 = {"work", "debug"}), which is what lets
# ENF-PROC-DEBUG-001 (always_on=true, domain=process) reach debug mode -- the
# one mode it is authored for -- while review/conversation still strip it.
DEBUG_DOCTRINE_RULE_ID = "ENF-PROC-DEBUG-001"

# The five mode values /always-on is exercised over below: None is the
# universal bundle (mode_scope echoes "universal"; query.py:792) plus the
# four named session modes.
_FIVE_MODES = (None, "work", "debug", "review", "conversation")


@pytest.mark.asyncio
async def test_always_on_includes_existing_rule_and_frb_nodes(always_on) -> None:
    """Don't regress the existing Rule + ForbiddenResponse path.

    RED cause: the Rule query (query.py:703-712) or the ForbiddenResponse
    query (query.py:718-724) stops running, or its rows never reach
    `combined` (query.py:736).
    """
    data = await always_on(mode="work")
    ids = [r["rule_id"] for r in data["rules"]]
    assert any(i.startswith("ENF-") for i in ids), (
        f"ENF- rules missing from always-on: {ids}"
    )
    assert any(i.startswith("FRB-") for i in ids), (
        f"FRB- nodes missing from always-on: {ids}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _FIVE_MODES)
async def test_no_methodology_prefix_in_any_mode(always_on, mode) -> None:
    """1.7 CUTOVER (D1): no SKL-/PBK-/TEC- id may appear in /always-on, ever.

    Subsumes the pre-cutover 'process-domain skills excluded in conversation
    mode' assertion (a strict subset of this check) and adds the two modes
    (universal, debug) nothing previously covered.

    RED cause: a Skill/Playbook/Technique row re-enters `combined` upstream
    of the mode strip (the 1.7 cutover deletion at query.py:729-736 is
    reverted, or a new source is joined into `combined`).
    """
    data = await always_on(mode=mode)
    ids = [r["rule_id"] for r in data["rules"]]
    assert ids, f"expected a non-empty rules list for mode={mode!r}"
    methodology = [i for i in ids if i.startswith(("SKL-", "PBK-", "TEC-"))]
    assert not methodology, (
        f"methodology ids leaked into /always-on for mode={mode!r}: {methodology}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", _FIVE_MODES)
async def test_bundle_is_non_empty_and_under_cap(always_on, mode) -> None:
    """The bundle is neither empty nor over budget, for every mode.

    Strictly more than the old work-only `total < cap`, which an empty
    bundle satisfied vacuously.

    RED cause: `cap` (query.py:791) shrinks below the live bundle's
    total_tokens (upper bound), or the render loop (query.py:775-786) stops
    accumulating est_tokens into total_tokens (lower bound: a bundle that
    reads as empty).
    """
    data = await always_on(mode=mode)
    cap = data["cap"]
    total = data["total_tokens"]
    assert 0 < total < cap, (
        f"mode={mode!r}: expected 0 < total_tokens < cap, got "
        f"total_tokens={total} cap={cap}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, expected",
    [("debug", True), ("work", True), ("review", False), ("conversation", False)],
)
async def test_process_domain_advisory_follows_process_modes(
    always_on, mandatory_rule_ids, mode, expected,
) -> None:
    """Increment 1 contract: process-domain always-on rules reach debug and
    work, while conversation/review remain blast-radius-pinned to the old
    (excluded) behavior.

    RED cause per row: debug/work (expected True) -- _ALWAYS_ON_PROCESS_MODES
    (query.py:667) shrinks to drop that mode. review/conversation (expected
    False) -- the strip's `not in ("process",)` clause (query.py:753-758) is
    removed, or that mode is added to _ALWAYS_ON_PROCESS_MODES.
    """
    data = await always_on(mode=mode)
    ids = {r["rule_id"] for r in data["rules"]}
    present = DEBUG_DOCTRINE_RULE_ID in ids
    assert present is expected, (
        f"{DEBUG_DOCTRINE_RULE_ID} presence in mode={mode!r}: expected "
        f"{expected}, got {present}; ids={sorted(ids)}"
    )
    if not expected:
        # Companion positive signal: the absence must not be a vacuous empty
        # bundle. Mandatory ids are exempt from this same strip
        # (query.py:753-758), so at least one must still be present in the
        # SAME response, proving the query ran and the strip is scoped
        # rather than total.
        assert mandatory_rule_ids & ids, (
            f"expected at least one mandatory rule id alongside the "
            f"{DEBUG_DOCTRINE_RULE_ID} absence in mode={mode!r}; "
            f"ids={sorted(ids)}"
        )
