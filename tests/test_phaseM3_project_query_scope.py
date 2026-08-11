"""Phase M.3: project-scoped retrieval + :Project registry.

RED-FIRST. Today query() is unconditionally search-all -- at project 2 it would
inject repo B's rules into repo A's agent. M.3 adds:
  - a :Project registry ({name, repo_root, bible_root}) + cwd->project resolver
    (longest repo_root prefix match; default 'writ').
  - pipeline.query(project=...) post-filtering ranked results to
    {caller_project, '_shared'} (a no-op at single-project = backward compatible;
    the anti-leak guarantee at project 2). Mirrors the existing domain post-filter.

Setup-for-the-future: dormant-but-correct with one project, fully tested.
Each test isolated (TEST-ISO-001).

--- Isolation cycle v2 (plan.md, Part 3), 2026-08-11 ---

Two contracts pinned above are DELIBERATELY INVERTED here, not drifted:

1. test_no_project_is_search_all_backward_compatible used to assert that
   project=None disables the post-filter entirely (both the "writ" and "proj2"
   rules come back). That file's own header called it "a no-op at single-project",
   and the plan's Analysis section notes there are now ten projects in the graph,
   so the condition the clause was written for no longer holds and the clause
   became the defect: pipeline.py:437 defaults an untagged node's project to
   "writ" too, so project=None was leaking doctrine AND records alike. The new
   contract is TestNoProjectIsDoctrineOnly below: doctrine reaches an unscoped
   caller, non-shared records do not.

2. test_resolve_unknown_cwd_defaults_writ used to assert the registry's fallback
   for an unregistered cwd is the literal string "writ". That default mislabels
   another project's query AS this one's -- the opposite direction of leak from
   the doctrine one, but still wrong. The new contract is
   TestResolveUnknownCwdReturnsNoProject below.
"""

from __future__ import annotations

import numpy as np
import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection


# --- pipeline project post-filter (stub pipeline, no Neo4j/ONNX) -------------

def _stub_pipeline(metadata: dict):
    from unittest.mock import MagicMock

    from writ.retrieval.embeddings import ScoredResult
    from writ.retrieval.pipeline import RetrievalPipeline
    from writ.retrieval.traversal import AdjacencyCache

    ids = list(metadata.keys())
    keyword_stub = MagicMock()
    keyword_stub.search.return_value = [{"rule_id": r, "score": 0.9} for r in ids]
    vector_stub = MagicMock()
    vector_stub.search.return_value = [ScoredResult(rule_id=r, score=0.9) for r in ids]
    encoder_stub = MagicMock()
    encoder_stub.encode.return_value = np.zeros(384, dtype=np.float32)
    return RetrievalPipeline(
        keyword_index=keyword_stub,
        vector_store=vector_stub,
        adjacency_cache=AdjacencyCache(),
        embedding_model=encoder_stub,
        rule_metadata=metadata,
    )


def _meta(node_type="Rule", domain="security", project="writ"):
    return {
        "node_type": node_type, "routes": ["semantic"], "domain": domain,
        "severity": "high", "confidence": "production-validated",
        "statement": "s.", "trigger": "t.", "project": project,
    }


def _result_ids(out: dict) -> set:
    return {r["rule_id"] for r in out.get("rules", [])}


class TestProjectQueryScope:
    """Doctrine crosses projects; record exclusion is pinned elsewhere.

    Written when Rule was the only retrievable type, so Rule stood in for "some node" and
    these tests read as "one project's nodes do not reach another". The isolation cycle's
    doctrine-versus-records split makes that fixture choice self-contradictory on Rule:
    capability 25 requires a Rule tagged `writ` to reach EVERY project, because all 287
    shipped rules carry that tag and scoping them by tag would deliver zero rules
    everywhere else. The only predicate satisfying both readings would privilege the
    literal string `writ`, granting every project read access to everything so tagged.

    Two of the three assertions are therefore inverted to their doctrine form. The
    original intent, that one project's records stay private, is real and is pinned
    against a REAL graph in tests/test_cross_project_retrieval_isolation.py. It cannot be
    pinned here: this stub passes no node_routes, so the pipeline's legacy branch sets
    allowed_types={"Rule"} and a record-typed fixture is dropped before the visibility
    filter ever sees it. Confirmed by trying it.
    """

    def _meta_two_projects(self):
        return {
            "SEC-WRIT-001": _meta(project="writ"),
            "SEC-PROJ2-001": _meta(project="proj2"),
            "SEC-SHARED-001": _meta(project="_shared"),
        }

    def test_a_rule_reaches_a_caller_from_another_project(self) -> None:
        """Was test_query_proj2_excludes_writ, which asserted the opposite.

        Every one of the 287 shipped Rule nodes carries project "writ", so hiding a
        writ-tagged Rule from another caller delivers ZERO rules to every project except
        this one: correctly isolated and completely inert. Verified before inverting that
        this stub cannot express the original intent either way: it passes no node_routes,
        so _resolve_stage1_filter takes the legacy branch and sets allowed_types={"Rule"},
        making every node here doctrine by construction. Record exclusion is therefore
        untestable in this stub and is pinned against a real graph in
        tests/test_cross_project_retrieval_isolation.py instead.
        """
        p = _stub_pipeline(self._meta_two_projects())
        ids = _result_ids(p.query("secret", project="proj2"))
        assert "SEC-WRIT-001" in ids, (
            "doctrine tagged for one project did not reach a caller from another, which "
            "is how scoping by tag starves every other project of rules"
        )

    def test_a_rule_reaches_its_own_project_too(self) -> None:
        """The companion direction, so the test above cannot pass by admitting nothing."""
        p = _stub_pipeline(self._meta_two_projects())
        ids = _result_ids(p.query("secret", project="writ"))
        assert "SEC-WRIT-001" in ids
        assert "SEC-PROJ2-001" in ids, "doctrine is doctrine regardless of its tag"

    def test_shared_project_always_included(self) -> None:
        p = _stub_pipeline(self._meta_two_projects())
        ids = _result_ids(p.query("secret", project="proj2"))
        assert "SEC-SHARED-001" in ids, "_shared rules must reach every project"


class TestNoProjectIsDoctrineOnly:
    """Replaces test_no_project_is_search_all_backward_compatible.

    project=None ("no caller project resolved") now means: every Rule-typed
    candidate passes regardless of its own project tag (doctrine reaches an
    unscoped caller), and a non-doctrine, non-"_shared" candidate is excluded.
    A Rule fixture is used for both sides because pipeline.py's own default for
    a missing `project` key is "writ" (line 437: `m.get("project", "writ")`),
    which is exactly the fail-open this contract change closes for records while
    leaving the doctrine allowance visible above (TestProjectQueryScope).
    """

    def test_no_project_still_returns_writ_and_proj2_rule_candidates(self) -> None:
        """Rule is doctrine, so BOTH tagged rules still reach an unscoped caller --
        this is the completeness half of the contract, not a leak: `writ query`
        and authoring.py's dedup suggestions rely on getting the full rulebook
        with no project argument at all."""
        p = _stub_pipeline({
            "SEC-WRIT-001": _meta(project="writ"),
            "SEC-PROJ2-001": _meta(project="proj2"),
        })
        ids = _result_ids(p.query("secret"))  # no project
        assert {"SEC-WRIT-001", "SEC-PROJ2-001"} <= ids

    def test_no_project_excludes_a_record_typed_candidate(self) -> None:
        """The inverted half. A non-doctrine node_type must NOT reach an unscoped
        caller even when its project tag happens to be "writ" -- proving the
        exclusion is driven by node TYPE, not by which project string sits on
        the metadata dict. node_types=["Rule","Decision"] is required to get the
        record candidate past Stage 1 at all (see _resolve_stage1_filter)."""
        p = _stub_pipeline({
            "SEC-WRIT-001": _meta(project="writ"),
            "REC-WRIT-001": _meta(node_type="Decision", project="writ"),
        })
        ids = _result_ids(p.query("secret", node_types=["Rule", "Decision"]))
        assert "SEC-WRIT-001" in ids
        assert "REC-WRIT-001" not in ids, (
            "a record-typed candidate reached an unscoped caller; project=None "
            "must not degrade back into search-all for records"
        )


# --- :Project registry + cwd->project resolver -------------------------------

@pytest_asyncio.fixture()
async def db():
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    await conn.clear_all()
    yield conn
    await conn.clear_all()
    await conn.close()


class TestProjectRegistry:
    @pytest.mark.asyncio
    async def test_create_and_list_projects(self, db) -> None:
        await db.create_project("writ", repo_root="/home/u/.claude/skills/writ", bible_root="bible")
        await db.create_project("proj2", repo_root="/home/u/repos/proj2", bible_root="bible/proj2")
        projects = {p["name"]: p for p in await db.get_projects()}
        assert {"writ", "proj2"} <= set(projects)
        assert projects["proj2"]["repo_root"] == "/home/u/repos/proj2"

    @pytest.mark.asyncio
    async def test_resolve_project_for_cwd_longest_prefix(self, db) -> None:
        await db.create_project("writ", repo_root="/home/u/.claude/skills/writ", bible_root="bible")
        await db.create_project("proj2", repo_root="/home/u/repos/proj2", bible_root="bible/proj2")
        # A cwd under proj2's repo resolves to proj2.
        assert await db.resolve_project_for_cwd("/home/u/repos/proj2/app/api.py") == "proj2"
        # A cwd under writ's repo resolves to writ.
        assert await db.resolve_project_for_cwd("/home/u/.claude/skills/writ/writ/cli.py") == "writ"

    @pytest.mark.asyncio
    async def test_resolve_unknown_cwd_returns_no_project(self, db) -> None:
        """Replaces test_resolve_unknown_cwd_defaults_writ.

        The old default ("writ") MISLABELED another project's query as this
        one's -- the CLI, /recall and retrieval would all silently scope an
        unregistered cwd's request to Writ's own corpus instead of degrading
        safely. The new contract: an unregistered cwd resolves to NO project
        (empty string), and each of the three callers is responsible for
        degrading safely on that empty answer (see TestCallersDegradeOnUnresolvedProject).
        """
        await db.create_project("writ", repo_root="/home/u/.claude/skills/writ", bible_root="bible")
        resolved = await db.resolve_project_for_cwd("/tmp/some/other/place")
        assert resolved == "", (
            f"an unregistered cwd must resolve to no project (empty string), "
            f"not the default project; got {resolved!r}"
        )

    @pytest.mark.asyncio
    async def test_resolve_unknown_cwd_with_no_projects_registered_at_all(self, db) -> None:
        """Anti-vacuity for the test above: an empty registry must not be
        confused with "no match found in a populated registry"."""
        resolved = await db.resolve_project_for_cwd("/tmp/some/other/place")
        assert resolved == ""

    @pytest.mark.asyncio
    async def test_default_argument_no_longer_forces_writ(self, db) -> None:
        """The `default="writ"` keyword argument itself must be gone (or at
        least unreachable from the standard call), not merely overridden at
        one call site -- a lingering default is the next regression waiting to
        happen the moment a caller forgets to override it."""
        await db.create_project("writ", repo_root="/home/u/.claude/skills/writ", bible_root="bible")
        resolved = await db.resolve_project_for_cwd("/tmp/some/other/place")
        assert resolved != "writ"


class TestCallersDegradeSafelyOnUnresolvedProject:
    """The blast-radius correction: resolve_project_for_cwd's old default had
    THREE callers beyond retrieval (writ/cli.py:1392, decision_memory.py:141,
    plus retrieval itself), and each must degrade differently on an empty
    resolution rather than share one silent "writ" fallback.

    Retrieval's half (doctrine-only) is proven end to end in
    test_cross_project_retrieval_isolation.py against a real graph. The
    /recall half lives in tests/test_server_recall.py
    (TestRecallRouteUnresolvedProject) and the CLI half in
    tests/test_cli_recall.py (TestRecallCmdUnresolvedProject) -- both already
    exercise the daemon/CLI's fake-db seam and would duplicate that fixture
    machinery if repeated here. This class holds only the registry-level
    contract both of those tests depend on.
    """

    @pytest.mark.asyncio
    async def test_empty_resolution_is_falsy_so_callers_can_branch_on_it(self, db) -> None:
        resolved = await db.resolve_project_for_cwd("/tmp/nowhere-registered")
        assert not resolved, (
            "callers branch on `if not project:`; a resolution that is present "
            "but non-empty (e.g. a sentinel string) would silently skip every "
            "caller's degrade-safely path"
        )
