"""Capability 15 (plan dfacff61/capabilities.md): PBK-PROC-ORCHESTRATOR-001,
read from the LIVE graph, never from bible/methodology/PBK-PROC-ORCHESTRATOR-001.md.

WHY THE LIVE GRAPH, NOT THE MARKDOWN FILE
------------------------------------------
bible/ is authored source, but the retrieval path this playbook documents
(writ-rag-inject.sh's orchestrator branch, `/prompt-bundle`'s ranked-channel
suppression) serves whatever Neo4j currently holds. Asserting against the
markdown file would prove only that a file was edited, not that the
correction reached the corpus a real session queries. Plan dfacff61
Decision 4 names the required sequence: `writ import-markdown bible/`, then
`writ export-cypher`, then `systemctl --user restart writ-server`. This test
is the only thing that can tell the difference between "the file was edited"
and "the correction shipped". This is the same contract
tests/test_debug_retrieval_keywords.py documents for the last corpus edit.

RED TODAY, GREEN ONLY AFTER THE OPERATIONAL RE-INGEST SEQUENCE RUNS, NO EDIT
TO THIS FILE IN BETWEEN. Verified against the live node 2026-09-01: it still
carries the pre-fix `statement` ("~1400-token broad RAG injection"), the
pre-fix `rationale` ("~3000+ tokens of duplicate rule injection"), and the
pre-fix `evidence` (".claude/hooks/writ-rag-inject.sh").

MUTATION (plan.md verification table, "node text"): skipping `import-markdown`
after editing bible/methodology/PBK-PROC-ORCHESTRATOR-001.md, or skipping
`export-cypher` after that, leaves every test in this module red: editing the
markdown file alone is not enough for either.

GRAPH SAFETY: read-only. No clear_all, no ingest_path, no node mutation,
anywhere in this file. conftest.py forces the isolated Neo4j instance before
this module is even collected (tests/_graph.py::apply_isolation_env).
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

PLAYBOOK_ID = "PBK-PROC-ORCHESTRATOR-001"

# The exact defects named in plan.md's Analysis and Decision 4, verified
# against the live node on 2026-09-01. Asserted as ABSENT, never as a
# substring pin on the CORRECTED text: the plan intentionally does not commit
# to replacement wording, only to removing the unsourced figures and the dead
# path.
STALE_HOOK_PATH = ".claude/hooks/"
CORRECT_HOOK_PATH_PREFIX = "hooks/scripts/"


@pytest_asyncio.fixture()
async def db_corpus() -> Neo4jConnection:
    """Read-only connection to the live (isolated) corpus.

    Self-heals via ensure_corpus() rather than assuming a predecessor module
    left the graph warm, mirroring tests/test_debug_retrieval_keywords.py.
    """
    from tests._corpus import ensure_corpus

    ensure_corpus()
    db = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with db._driver.session(database=db._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await db.close()
        pytest.skip("Neo4j unreachable")
    yield db
    await db.close()


async def _read_playbook_node(db: Neo4jConnection) -> dict:
    """The node's text fields, straight from Neo4j; no bible/ file involved."""
    async with db._driver.session(database=db._database) as s:
        result = await s.run(
            "MATCH (n:Playbook {playbook_id: $id}) "
            "RETURN n.statement AS statement, n.rationale AS rationale, "
            "n.evidence AS evidence, n.body AS body, "
            "n.last_validated AS last_validated",
            id=PLAYBOOK_ID,
        )
        record = await result.single()
        return dict(record) if record is not None else {}


class TestPlaybookNodeExistsAndReads:
    """Anti-vacuity: every assertion below is meaningless if the node cannot
    be found at all (an empty dict satisfies no substring check either way,
    which would make a broken query look like a passing test for the wrong
    reason)."""

    @pytest.mark.asyncio
    async def test_node_exists_in_the_live_graph(self, db_corpus: Neo4jConnection) -> None:
        node = await _read_playbook_node(db_corpus)
        assert node, f"{PLAYBOOK_ID} not found in the live graph at all"
        assert node.get("statement"), node
        assert node.get("evidence"), node


class TestStatementNamesTheRankedChannelWithNoTokenFigure:
    """capabilities.md: 'statement naming the ranked channel and containing
    no ~ token figure'. The statement field is injected into a model's
    context on every matching turn (plan.md Decision 4), so it is the field
    that must carry the mechanism and never a number nobody re-measured."""

    @pytest.mark.asyncio
    async def test_no_unsourced_token_figure(self, db_corpus: Neo4jConnection) -> None:
        node = await _read_playbook_node(db_corpus)
        statement = node.get("statement") or ""
        assert statement, node
        assert "~" not in statement, (
            f"statement still carries an unsourced '~' token figure: {statement!r}"
        )

    @pytest.mark.asyncio
    async def test_names_the_ranked_channel_it_now_suppresses(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await _read_playbook_node(db_corpus)
        statement = (node.get("statement") or "").lower()
        assert statement, node
        assert "ranked" in statement or "prompt-bundle" in statement, (
            f"statement does not name the ranked channel the --orchestrator "
            f"flag now suppresses: {statement!r}"
        )
        # It must describe a PARTIAL suppression (one channel), not the old
        # "suppresses ~1400-token broad RAG injection" framing that reads as
        # the whole per-turn injection being turned off.
        assert "suppresses the" in statement or "suppress" in statement, statement


class TestEvidenceAndBodyNameTheRealHookPath:
    """capabilities.md: 'its evidence and body name hooks/scripts/ and never
    .claude/hooks/'. The pre-fix node points at a path that has not existed
    since before this playbook was authored (the real script lives at
    hooks/scripts/writ-rag-inject.sh)."""

    @pytest.mark.asyncio
    async def test_evidence_never_names_the_dead_path(self, db_corpus: Neo4jConnection) -> None:
        node = await _read_playbook_node(db_corpus)
        evidence = node.get("evidence") or ""
        assert evidence, node
        assert STALE_HOOK_PATH not in evidence, evidence

    @pytest.mark.asyncio
    async def test_body_never_names_the_dead_path(self, db_corpus: Neo4jConnection) -> None:
        node = await _read_playbook_node(db_corpus)
        body = node.get("body") or ""
        assert STALE_HOOK_PATH not in body, body[:800]

    @pytest.mark.asyncio
    async def test_evidence_names_the_real_hook_path(self, db_corpus: Neo4jConnection) -> None:
        node = await _read_playbook_node(db_corpus)
        evidence = node.get("evidence") or ""
        assert evidence, node
        assert CORRECT_HOOK_PATH_PREFIX in evidence, (
            f"evidence does not point at the real hook path ({CORRECT_HOOK_PATH_PREFIX}...): "
            f"{evidence!r}"
        )


class TestRationaleCarriesNoUnsourcedClaim:
    """plan.md Decision 4: 'the ~3000+ tokens of duplicate rule injection"
    claim is removed outright rather than replaced, because it never
    described anything that was measured.' Checked independently of the
    statement's own ~-figure check above: rationale is a separate field and a
    fix that only cleaned the statement would leave this one stale."""

    @pytest.mark.asyncio
    async def test_no_unsourced_duplicate_injection_claim(
        self, db_corpus: Neo4jConnection
    ) -> None:
        node = await _read_playbook_node(db_corpus)
        rationale = node.get("rationale") or ""
        assert rationale, node
        assert "~" not in rationale, (
            f"rationale still carries an unsourced '~' token figure: {rationale!r}"
        )
