"""Cycle D (plan.md ## Analysis > "Item 2: RECOMMENDATION -- fix the pull
channel (keywords)", ## Complete code item 12, cycle-D lines under
## Capabilities).

Four methodology nodes (PBK-PROC-DEBUG-001, TEC-PROC-HYPOTHESIS-001,
TEC-PROC-ROOTCAUSE-001, SKL-PROC-REVRECV-001) declare `floor_modes` of only
`[debug]` or `[review]`. `classify_mode_hint` (bin/lib/writ_mode_hint.py:62-72)
emits only `investigate`, `work` or `None`, so floor never fires
automatically for these nodes -- their only automatic delivery channel is
keyword PULL (writ/retrieval/trigger_index.py,
MethodologyTriggerIndex._score_pull, budget-capped at :100-111, lowest
match-count dropped first). Today their `trigger_keywords` are methodology
vocabulary ("hypothesis", "root-cause") rather than the vocabulary of a
person reporting a bug or receiving review feedback, so real prompts miss
them. Plan item 12 widens `trigger_keywords` on the four nodes, keeping the
existing entries so nothing that matches today stops matching.

RED TODAY, GREEN AFTER THE CORPUS SHIPS, NO EDIT TO THIS FILE IN BETWEEN:
these tests read the LIVE graph via `MethodologyTriggerIndex.build_from_db`,
never a fixture copy of the keywords, so a green run is proof the corpus
re-import (`import-markdown` + `export-cypher` + `systemctl --user restart
writ-server`) actually shipped the new vocabulary rather than proof of a
test-side stub. Expected split when run today: the new-vocabulary tests
(TestNewVocabularyPulls*) FAIL (node not pulled); the old-vocabulary test
(TestOldVocabularyStillMatches) and the guard test
(TestPullBudgetGuardAgainstOverBroadKeywords) PASS.

GRAPH SAFETY: read-only. No clear_all, no ingest_path, no node mutation,
anywhere in this file -- pattern copied from
tests/test_phase19_route_delivery_closure.py's `db_corpus` fixture.
conftest.py forces the isolated 7688 instance before this module is even
collected (tests/_graph.py::apply_isolation_env), so `get_neo4j_uri()` below
never resolves to production 7687.

`mode=None` in every `idx.match(...)` call below is deliberate, not a
simplification: it is the actual condition these four nodes are stuck in in
production (the auto-router never emits `debug` or `review`), so with
`mode=None` the floor channel is structurally excluded
(`MethodologyTriggerIndex._select_floor` requires `mode and mode in
floor_modes`) and PULL is the only channel that can possibly deliver these
nodes on an unrouted prompt -- exactly the channel cycle D's keyword change
targets. `action` is left `None` for the same reason (push is a separate,
unrelated channel).

Budget is passed as an effectively unlimited `budget_tokens` in the positive
pull assertions, on purpose: the claim under test is "the keyword matched",
not "the keyword matched AND survived budget truncation against whatever
else the live corpus also matches for this prompt". Budget-cap behavior
itself is already covered by tests/test_phase16b_trigger_index.py and
tests/test_phase18b_resurface.py.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.retrieval.trigger_index import MethodologyTriggerIndex

# Effectively unlimited: isolates "did the keyword match" from "did it survive
# the budget cap", which is a separate, already-tested concern.
_UNLIMITED_BUDGET = 1_000_000

# Verified against the CURRENT (pre-cycle-D) frontmatter, read 2026-08-13:
#   bible/methodology/PBK-PROC-DEBUG-001.md:31
OLD_DEBUG_KEYWORDS = [
    "hypothesis", "root-cause", "debugging", "quick fix",
    "stop patching", "skip the process", "fails unexpectedly",
]

# bible/methodology/TEC-PROC-ROOTCAUSE-001.md:22
OLD_ROOTCAUSE_KEYWORDS = [
    "call stack", "backward", "root-cause", "failure point",
    "component boundary", "diagnostic instrumentation", "far from the cause",
]

# bible/methodology/SKL-PROC-REVRECV-001.md carries NO trigger_keywords key at
# all today (only action_triggers: ["review-feedback"]) -- so there is no old
# list for a review-shaped prompt to stay disjoint from, and correspondingly
# no "old vocabulary still matches" regression test for this node in this
# file: widening cannot regress a channel that does not yet exist.


def _word_in(prompt: str, phrase: str) -> bool:
    """Whole-word/phrase, case-insensitive match -- the same semantics
    MethodologyTriggerIndex._keyword_matches uses for pull, so a disjointness
    assertion is checked under the exact rule that decides whether pull
    fires (not a looser or stricter stand-in that could pass or fail for the
    wrong reason)."""
    return re.search(rf"\b{re.escape(phrase)}\b", prompt, re.IGNORECASE) is not None


def _pulled_ids(idx: MethodologyTriggerIndex, prompt: str) -> list[str]:
    """Node ids the PULL channel alone delivers for this prompt, with
    mode=None (see module docstring for why that is the real production
    condition, not a simplification)."""
    result = idx.match(None, prompt, budget_tokens=_UNLIMITED_BUDGET)
    return [n["id"] for n in result["nodes"] if n["channel"] == "pull"]


@pytest_asyncio.fixture()
async def db_corpus() -> Neo4jConnection:
    # Self-heal before reading: a predecessor module may have wiped the shared
    # graph and abandoned it (the order-pollution class found at the cycle F
    # commit gate). ensure_corpus is loop-safe and a no-op when already complete,
    # so a corpus-READING fixture heals rather than depending on collection order.
    from tests._corpus import ensure_corpus
    ensure_corpus()
    """Read-only connection to the live corpus (isolated 7688 per conftest).
    No clear_all, no ingest_path, no write of any kind: this fixture only
    ever reads whatever trigger_keywords the graph currently carries."""
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    yield db
    await db.close()


@pytest_asyncio.fixture()
async def trigger_index(db_corpus: Neo4jConnection) -> MethodologyTriggerIndex:
    """The real pull-matching substrate, built from whatever trigger_keywords
    the live graph currently holds -- not a fixture copy of the keyword
    lists, so this index reflects exactly what cycle D's corpus re-import
    ships (or has not yet shipped)."""
    return await MethodologyTriggerIndex.build_from_db(db_corpus)


class TestNewVocabularyPullsDebugPlaybook:
    """THE CENTERPIECE. A debugging-shaped prompt built only from plan item
    12's new PBK-PROC-DEBUG-001 vocabulary ("test is failing", "stack
    trace", "reproduce") must pull the node via PULL alone, with mode=None
    (floor structurally excluded). The disjointness assertion against
    OLD_DEBUG_KEYWORDS is inline, not a separate test, so a pass here proves
    specifically that the NEW keywords did the work."""

    @pytest.mark.asyncio
    async def test_debugging_shaped_prompt_pulls_debug_playbook(
        self, trigger_index: MethodologyTriggerIndex
    ) -> None:
        prompt = "the test is failing with a stack trace I cannot reproduce"
        for kw in OLD_DEBUG_KEYWORDS:
            assert not _word_in(prompt, kw), (
                f"prompt must not contain the OLD keyword {kw!r} -- a pass "
                "here would prove nothing about the NEW keywords cycle D adds"
            )
        pulled = _pulled_ids(trigger_index, prompt)
        assert "PBK-PROC-DEBUG-001" in pulled, (
            "RED before cycle D ships: PBK-PROC-DEBUG-001's live "
            "trigger_keywords do not yet include 'test is failing' / 'stack "
            "trace' / 'reproduce' (plan.md item 12). GREEN only after the "
            "bible/ edit + import-markdown + export-cypher + daemon restart. "
            f"Pulled nodes were: {pulled}"
        )


class TestNewVocabularyPullsRootCauseTechnique:
    """Same claim, for one of the two technique nodes: TEC-PROC-ROOTCAUSE-001
    gains "actual cause", "symptom", "keeps coming back", "patched the
    symptom" (plan item 12). Disjointness against OLD_ROOTCAUSE_KEYWORDS is
    inline for the same reason as above."""

    @pytest.mark.asyncio
    async def test_recurring_bug_prompt_pulls_rootcause_technique(
        self, trigger_index: MethodologyTriggerIndex
    ) -> None:
        prompt = (
            "this bug keeps coming back because we only patched the symptom "
            "instead of finding the actual cause"
        )
        for kw in OLD_ROOTCAUSE_KEYWORDS:
            assert not _word_in(prompt, kw), (
                f"prompt must not contain the OLD keyword {kw!r} -- a pass "
                "here would prove nothing about the NEW keywords cycle D adds"
            )
        pulled = _pulled_ids(trigger_index, prompt)
        assert "TEC-PROC-ROOTCAUSE-001" in pulled, (
            "RED before cycle D ships: TEC-PROC-ROOTCAUSE-001's live "
            "trigger_keywords do not yet include 'keeps coming back' / "
            "'patched the symptom' / 'symptom' / 'actual cause' (plan.md "
            f"item 12). Pulled nodes were: {pulled}"
        )


class TestNewVocabularyPullsReviewReceivingSkill:
    """SKL-PROC-REVRECV-001 gains its first-ever trigger_keywords (today it
    has none, only action_triggers: ["review-feedback"]): "review feedback",
    "the reviewer said", "address the comments", "disagree with the review",
    "pushback" (plan item 12). No old-vocabulary disjointness check applies
    here (see the OLD_* comment above the constants): there is no prior pull
    channel for this widening to have replaced."""

    @pytest.mark.asyncio
    async def test_review_shaped_prompt_pulls_revrecv_skill(
        self, trigger_index: MethodologyTriggerIndex
    ) -> None:
        prompt = (
            "the reviewer said the approach was wrong; I want to address "
            "the comments before continuing"
        )
        pulled = _pulled_ids(trigger_index, prompt)
        assert "SKL-PROC-REVRECV-001" in pulled, (
            "RED before cycle D ships: SKL-PROC-REVRECV-001 has no "
            "trigger_keywords at all yet, so a review-shaped prompt cannot "
            "reach it via pull (plan.md item 12 adds its first keyword "
            f"list). Pulled nodes were: {pulled}"
        )


class TestOldVocabularyStillMatches:
    """Widening must not replace: an old-vocabulary prompt for
    PBK-PROC-DEBUG-001 ("root-cause", already in trigger_keywords today)
    must keep pulling the node both before and after cycle D ships. This
    test is expected to PASS today (it is the control for the RED tests
    above)."""

    @pytest.mark.asyncio
    async def test_old_keyword_root_cause_still_pulls_debug_playbook(
        self, trigger_index: MethodologyTriggerIndex
    ) -> None:
        prompt = "we need to find the root-cause of this issue"
        pulled = _pulled_ids(trigger_index, prompt)
        assert "PBK-PROC-DEBUG-001" in pulled, (
            "the pre-existing keyword 'root-cause' must still pull "
            f"PBK-PROC-DEBUG-001 today; pulled nodes were: {pulled}"
        )


class TestPullBudgetGuardAgainstOverBroadKeywords:
    """Guard against budget displacement (trigger_index.py:100-111: pull is
    budget-capped, lowest match-count dropped first). A prompt matching
    NOTHING debug-related must not pull PBK-PROC-DEBUG-001 -- this is the
    base-rate check that a broadened keyword set (e.g. the new, fairly
    generic "throws") has not turned the node into a default match that
    would crowd the pull budget on unrelated turns. Expected to PASS both
    before and after cycle D ships: this prompt is chosen to share zero
    vocabulary with either the old or the new keyword lists."""

    @pytest.mark.asyncio
    async def test_unrelated_prompt_does_not_pull_debug_playbook(
        self, trigger_index: MethodologyTriggerIndex
    ) -> None:
        prompt = "what's a good recipe for banana bread?"
        pulled = _pulled_ids(trigger_index, prompt)
        assert "PBK-PROC-DEBUG-001" not in pulled, (
            "an unrelated prompt must not pull PBK-PROC-DEBUG-001 -- if it "
            "does, a keyword widened for cycle D is over-broad and is "
            f"flooding the pull budget on irrelevant turns. Pulled: {pulled}"
        )
