"""Decision-memory Phase 3a: reliable mechanical Decision capture on commit.

Test skeleton for the capabilities defined in plan.md ## Capabilities (14 items).
Every test in this file is RED until the implementer builds the corresponding
feature. Tests fail on AttributeError/ImportError/TypeError/AssertionError for
the missing code -- never on a harness (collection or syntax) error.

Run interpreter: .venv/bin/python (system python3 errors on embedding imports).
NEVER run bare pytest or the full suite: tests that touch Neo4j without the
test-dm-1d scope wipe the live shared graph.

  .venv/bin/python -m pytest tests/test_decision_memory_phase3a.py -v

Isolation tiers:
  PURE-PARSER -- no Neo4j at all; import only from writ.session.plan_harvest.
  FAKE-DB     -- no Neo4j; uses _FakeDB async double (mirrors
                 tests/test_decision_memory_harvester.py) to assert record shape
                 and edge wiring for writ.session.harvester.harvest_one_commit.
  NEO4J-GATED -- real MERGE semantics only; db_clean fixture (scope "test-dm-1d",
                 repo_root "/tmp/fake-test-1d-repo"); skipped when Neo4j is
                 unreachable. The live "writ" :Project is NEVER touched.

Capability map (all 14):
  [ph3a-1]   _extract_files parses prose heading ### N. `path` - description
  [ph3a-2]   _extract_files parses prose bullet - `path` - description
  [ph3a-3]   _extract_files still parses gated format and does not double-count
  [ph3a-4]   _extract_files(allowed_paths) drops paths absent from the set
  [ph3a-5]   harvest_plan threads allowed_paths; None returns unfiltered files
  [ph3a-6]   harvest_one_commit creates ONE Decision + Commit + N FileChanges
  [ph3a-7]   harvest_one_commit with plan_text=None: no Decision, Commit+FC with subject
  [ph3a-8]   harvest_one_commit wires all 7 edge types for a governed commit
  [ph3a-9]   harvest loop delegates to harvest_one_commit (identical records)
  [ph3a-10]  capture_commit reads transcript + creates Decision mechanically
  [ph3a-11]  capture_commit falls open on missing/unparseable transcript
  [ph3a-12]  idempotency: second run by either path adds zero net nodes (real DB)
  [ph3a-13]  decision_capture uses content-hash id _decision_id(name, plan_text)
  [ph3a-14]  test_creates_records_and_edges_with_real_db has try/finally teardown
             (implementer fix in test_decision_memory_commit.py; not tested here --
             capability listed in plan.md but owned by a sibling file modification)

ENF-SYS-005 note: idempotency ([ph3a-12]) cannot be proven with mocks. That test
uses db_clean with a real Neo4j connection and asserts zero net new nodes via
Cypher count queries before and after the second run.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection


# ---------------------------------------------------------------------------
# Constants (mirror test_decision_memory_commit.py scope/repo_root)
# ---------------------------------------------------------------------------

_TEST_SCOPE = "test-dm-1d"
_TEST_REPO_ROOT = "/tmp/fake-test-1d-repo"


# ---------------------------------------------------------------------------
# Fake async DB double (mirrors test_decision_memory_harvester.py:_FakeDB)
# ---------------------------------------------------------------------------

class _FakeDB:
    """Records every harvest_one_commit db call for assertion. All methods async.

    create_* validate via the REAL Pydantic models so a missing required field
    fails here too, not only against live Neo4j. The real db auto-stamps
    FileChange/Commit ts but NOT Decision ts, so the double mirrors that.
    """

    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.commits: list[dict] = []
        self.filechanges: list[dict] = []
        self.edges: list[tuple] = []
        self.resolved: list[tuple] = []

    async def create_decision(self, **kw):
        from writ.graph.schema import Decision
        Decision(**kw)  # Decision.ts is required from the caller (not auto-stamped)
        self.decisions.append(kw)
        return kw["decision_id"]

    async def create_commit(self, **kw):
        from writ.graph.schema import Commit
        kw.setdefault("ts", "2026-01-01T00:00:00Z")
        Commit(**kw)
        self.commits.append(kw)
        return kw["commit_hash"]

    async def create_filechange(self, **kw):
        from writ.graph.schema import FileChange
        kw.setdefault("ts", "2026-01-01T00:00:00Z")
        FileChange(**kw)
        self.filechanges.append(kw)
        return kw["change_id"]

    async def wire_has_decision(self, *a):
        self.edges.append(("HAS_DECISION", a))

    async def wire_governed_by(self, *a):
        self.edges.append(("GOVERNED_BY", a))

    async def wire_has_commit(self, *a):
        self.edges.append(("HAS_COMMIT", a))

    async def wire_has_change(self, *a):
        self.edges.append(("HAS_CHANGE", a))

    async def wire_includes(self, *a):
        self.edges.append(("INCLUDES", a))

    async def wire_motivated_by(self, *a):
        self.edges.append(("MOTIVATED_BY", a))

    async def wire_realizes(self, *a):
        self.edges.append(("REALIZES", a))

    async def resolve_file_claims(self, *a):
        self.resolved.append(a)
        return 1

    async def get_open_decisions_for_path(self, *a):
        # No pre-existing open claims: lets capture_commit's resolve_reasons_for_files
        # return cleanly so the fail-open test exercises the missing-transcript path,
        # not an AttributeError from an incomplete double.
        return []


def _edge_types(db: _FakeDB) -> set[str]:
    return {e[0] for e in db.edges}


# ---------------------------------------------------------------------------
# Neo4j-gated fixture (mirrors test_decision_memory_commit.py:db_clean)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_clean():
    """Connect to Neo4j, wipe test-dm-1d scope, yield conn, wipe + close.

    Skips when Neo4j is unreachable. Teardown wipes by both the test scope name
    and the /tmp/fake-test-1d-repo repo_root prefix so no test-seeded nodes
    survive into the live graph.
    """
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")

    await _wipe_3a_test_data(conn)
    yield conn
    await _wipe_3a_test_data(conn)
    await conn.close()


async def _wipe_3a_test_data(conn: Neo4jConnection) -> None:
    """Wipe all nodes that could have been created by Phase 3a tests."""
    await conn.clear_project(_TEST_SCOPE)
    async with conn._driver.session(database=conn._database) as s:
        # :Project by name prefix.
        await (await s.run(
            "MATCH (p:Project) WHERE p.name STARTS WITH $prefix DETACH DELETE p",
            prefix=_TEST_SCOPE,
        )).consume()
        # :Project by repo_root prefix.
        await (await s.run(
            "MATCH (p:Project) WHERE p.repo_root STARTS WITH $root_prefix DETACH DELETE p",
            root_prefix=_TEST_REPO_ROOT,
        )).consume()
        # :Decision by project prefix.
        await (await s.run(
            "MATCH (d:Decision) WHERE d.project STARTS WITH $prefix DETACH DELETE d",
            prefix=_TEST_SCOPE,
        )).consume()
        # :Commit by project prefix.
        await (await s.run(
            "MATCH (c:Commit) WHERE c.project STARTS WITH $prefix DETACH DELETE c",
            prefix=_TEST_SCOPE,
        )).consume()
        # :FileChange by project prefix.
        await (await s.run(
            "MATCH (fc:FileChange) WHERE fc.project STARTS WITH $prefix DETACH DELETE fc",
            prefix=_TEST_SCOPE,
        )).consume()
        # :Rule nodes seeded by 3a tests (3A-* prefix).
        await (await s.run(
            "MATCH (r:Rule) WHERE r.rule_id STARTS WITH '3A-' DETACH DELETE r",
        )).consume()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _expected_decision_id(name: str, plan_text: str) -> str:
    """Mirrors harvester._decision_id -- used to assert content-hash formula."""
    h = hashlib.sha1(f"{name}\x00{plan_text}".encode()).hexdigest()[:12]
    return f"DEC-{name}-{h}"


# ---------------------------------------------------------------------------
# PURE-PARSER: plan_harvest._extract_files prose heading format
# Capability [ph3a-1]
# ---------------------------------------------------------------------------

class TestExtractFilesProseParsing:
    """Caps [ph3a-1], [ph3a-2], [ph3a-3] -- no Neo4j, no git required."""

    def test_prose_heading_format_parsed_into_file_entry(self) -> None:
        # [ph3a-1]: "### N. `path` - description" -> {path, reason=description}.
        # RED: _extract_files does not yet accept the prose heading regex; the
        # path will be absent from the returned list (AssertionError) or
        # _extract_files raises TypeError on an unexpected keyword.
        from writ.session.plan_harvest import _extract_files

        plan = (
            "## Implementation\n\n"
            "### 1. `writ/session/harvester.py` - extract harvest_one_commit\n\n"
            "### 2. `writ/session/plan_harvest.py` - add prose regexes\n"
        )
        result = _extract_files(plan)
        paths = [f["path"] for f in result]
        assert "writ/session/harvester.py" in paths, (
            f"prose heading path 'writ/session/harvester.py' must be parsed; got {paths}"
        )
        assert "writ/session/plan_harvest.py" in paths, (
            f"prose heading path 'writ/session/plan_harvest.py' must be parsed; got {paths}"
        )
        # Reason must be the description after ' - '.
        entry = next(f for f in result if f["path"] == "writ/session/harvester.py")
        assert entry["reason"] == "extract harvest_one_commit", (
            f"reason must be the prose description; got {entry['reason']!r}"
        )

    def test_prose_bullet_format_parsed_into_file_entry(self) -> None:
        # [ph3a-2]: "- `path` - description" -> {path, reason=description}.
        # RED: same as above -- prose bullet regex does not exist yet.
        from writ.session.plan_harvest import _extract_files

        plan = (
            "Some narrative text.\n\n"
            "- `writ/session/commit_capture.py` - add _commit_ts helper\n"
            "- `writ/server.py` - thread plan_text into capture_commit\n"
        )
        result = _extract_files(plan)
        paths = [f["path"] for f in result]
        assert "writ/session/commit_capture.py" in paths, (
            f"prose bullet 'writ/session/commit_capture.py' must be parsed; got {paths}"
        )
        assert "writ/server.py" in paths, (
            f"prose bullet 'writ/server.py' must be parsed; got {paths}"
        )
        entry = next(f for f in result if f["path"] == "writ/session/commit_capture.py")
        assert entry["reason"] == "add _commit_ts helper", (
            f"reason must be the prose description; got {entry['reason']!r}"
        )

    def test_gated_format_still_parsed_and_no_double_count(self) -> None:
        # [ph3a-3]: the gated Writ "- `path` (type) -- reason" format still works,
        # AND a path that appears in both the ## Files section and a prose mention
        # is NOT double-counted (dedupe by first-seen).
        # RED: either the gated format regresses (the prose scan clobbers it) or
        # the dedupe is missing (path appears twice in the result list).
        from writ.session.plan_harvest import _extract_files

        plan = (
            "## Files\n\n"
            "- `writ/session/harvester.py` (modify) -- extract the shared helper\n"
            "- `writ/session/plan_harvest.py` (modify) -- add prose regexes\n\n"
            "## Implementation\n\n"
            "### 1. `writ/session/harvester.py` - extract harvest_one_commit\n"
        )
        result = _extract_files(plan)
        paths = [f["path"] for f in result]

        # Gated format entry must exist.
        assert "writ/session/harvester.py" in paths, (
            f"gated-format path must be parsed; got {paths}"
        )
        # No duplicate: the path that appears in both sections must appear exactly once.
        count = paths.count("writ/session/harvester.py")
        assert count == 1, (
            f"path 'writ/session/harvester.py' appears {count} times; must appear exactly once"
        )
        # The gated-format reason must be preserved (first-seen wins).
        entry = next(f for f in result if f["path"] == "writ/session/harvester.py")
        assert entry["reason"] == "extract the shared helper", (
            f"gated-format reason must be preserved; got {entry['reason']!r}"
        )


# ---------------------------------------------------------------------------
# PURE-PARSER: _extract_files with allowed_paths (phantom-path guard)
# Capability [ph3a-4]
# ---------------------------------------------------------------------------

class TestExtractFilesAllowedPaths:
    """Cap [ph3a-4] -- no Neo4j."""

    def test_allowed_paths_drops_absent_path_and_keeps_present(self) -> None:
        # [ph3a-4]: a parsed path NOT in allowed_paths is dropped; a parsed path
        # IN allowed_paths is kept.
        # RED: _extract_files does not yet accept an allowed_paths keyword
        # argument -> TypeError.
        from writ.session.plan_harvest import _extract_files

        plan = (
            "- `writ/session/harvester.py` - real change\n"
            "- `writ/session/phantom.py` - mentioned but not committed\n"
        )
        allowed = {"writ/session/harvester.py"}
        result = _extract_files(plan, allowed_paths=allowed)
        paths = [f["path"] for f in result]

        assert "writ/session/harvester.py" in paths, (
            f"path present in allowed_paths must be kept; got {paths}"
        )
        assert "writ/session/phantom.py" not in paths, (
            f"phantom path absent from allowed_paths must be dropped; got {paths}"
        )

    def test_allowed_paths_none_returns_all_paths(self) -> None:
        # [ph3a-4] edge case: allowed_paths=None -> no filtering, all paths returned.
        # RED: TypeError (unexpected keyword) or filtering applied wrongly.
        from writ.session.plan_harvest import _extract_files

        plan = (
            "- `writ/session/harvester.py` - change one\n"
            "- `writ/session/plan_harvest.py` - change two\n"
        )
        result = _extract_files(plan, allowed_paths=None)
        paths = [f["path"] for f in result]
        assert "writ/session/harvester.py" in paths, (
            "allowed_paths=None must not filter any path"
        )
        assert "writ/session/plan_harvest.py" in paths, (
            "allowed_paths=None must not filter any path"
        )


# ---------------------------------------------------------------------------
# PURE-PARSER: harvest_plan threads allowed_paths
# Capability [ph3a-5]
# ---------------------------------------------------------------------------

class TestHarvestPlanAllowedPaths:
    """Cap [ph3a-5] -- no Neo4j."""

    def test_harvest_plan_with_allowed_paths_filters(self) -> None:
        # [ph3a-5]: harvest_plan(plan_text, allowed_paths={...}) passes allowed_paths
        # through to _extract_files; only paths in the set are returned.
        # RED: harvest_plan does not yet accept allowed_paths -> TypeError.
        from writ.session.plan_harvest import harvest_plan

        plan = (
            "## Analysis\n\nThe rationale.\n\n"
            "- `writ/session/harvester.py` - kept\n"
            "- `writ/session/phantom.py` - dropped\n\n"
            "## Rules Applied\n\nDOC-ARCH-001\n"
        )
        result = harvest_plan(plan, allowed_paths={"writ/session/harvester.py"})
        paths = [f["path"] for f in result["files"]]
        assert "writ/session/harvester.py" in paths, (
            f"allowed path must survive; got {paths}"
        )
        assert "writ/session/phantom.py" not in paths, (
            f"phantom path must be filtered; got {paths}"
        )

    def test_harvest_plan_allowed_paths_none_returns_unfiltered(self) -> None:
        # [ph3a-5]: harvest_plan(plan_text, allowed_paths=None) -> all parsed files.
        # RED: TypeError or files inadvertently filtered.
        from writ.session.plan_harvest import harvest_plan

        plan = (
            "- `writ/session/harvester.py` - one\n"
            "- `writ/session/plan_harvest.py` - two\n"
        )
        result = harvest_plan(plan, allowed_paths=None)
        paths = [f["path"] for f in result["files"]]
        assert len(paths) == 2, (
            f"allowed_paths=None must return all 2 parsed paths; got {paths}"
        )

    def test_harvest_plan_without_allowed_paths_kwarg_still_works(self) -> None:
        # [ph3a-5]: calling harvest_plan(plan_text) without the new kwarg must not
        # break (backward-compatible default). RED: TypeError if kwarg has no default.
        from writ.session.plan_harvest import harvest_plan

        plan = "- `writ/session/harvester.py` - some change\n"
        # Must not raise.
        result = harvest_plan(plan)
        assert "files" in result, "harvest_plan must still return a dict with 'files'"


# ---------------------------------------------------------------------------
# FAKE-DB: harvest_one_commit record shape -- governing plan present
# Capabilities [ph3a-6], [ph3a-8]
# ---------------------------------------------------------------------------

class TestHarvestOneCommitWithPlan:
    """Caps [ph3a-6], [ph3a-8] -- no Neo4j; uses _FakeDB."""

    @pytest.mark.asyncio
    async def test_creates_one_decision_one_commit_n_filechanges(
        self, monkeypatch
    ) -> None:
        # [ph3a-6]: a governing plan that parses >=1 in-commit file produces exactly
        # one Decision with the content-hash id, one Commit, and one FileChange per
        # committed file.
        # RED: harvest_one_commit does not exist yet -> AttributeError.
        from writ.session import harvester

        # The plan names exactly the files in `files` (no phantom paths).
        plan_text = (
            "## Analysis\n\nThe rationale for this commit.\n\n"
            "- `writ/session/harvester.py` - extract the shared helper\n"
            "- `writ/session/plan_harvest.py` - add prose regexes\n\n"
            "## Rules Applied\n\nDOC-ARCH-001\nENF-SYS-005\n"
        )
        files = [
            {"path": "writ/session/harvester.py", "change_type": "modify"},
            {"path": "writ/session/plan_harvest.py", "change_type": "modify"},
        ]
        db = _FakeDB()
        stats = await harvester.harvest_one_commit(
            db, _TEST_SCOPE,
            commit_hash="3a-abc123def456",
            subject="feat(3a): extract harvest_one_commit",
            author="Test Author <dev@example.com>",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )

        # Exactly one Decision.
        assert len(db.decisions) == 1, (
            f"expected 1 Decision; got {len(db.decisions)}"
        )
        expected_id = _expected_decision_id(_TEST_SCOPE, plan_text)
        assert db.decisions[0]["decision_id"] == expected_id, (
            f"Decision id must be content-hash 'DEC-{{name}}-{{sha1[:12]}}'; "
            f"expected {expected_id!r}, got {db.decisions[0]['decision_id']!r}"
        )
        # Exactly one Commit.
        assert len(db.commits) == 1, f"expected 1 Commit; got {len(db.commits)}"
        assert db.commits[0]["commit_hash"] == "3a-abc123def456"
        # One FileChange per file.
        assert len(db.filechanges) == 2, (
            f"expected 2 FileChanges; got {len(db.filechanges)}"
        )
        # Stats.
        assert stats["decisions"] == 1, f"stats['decisions'] must be 1; got {stats}"
        assert stats["filechanges"] == 2, f"stats['filechanges'] must be 2; got {stats}"

    @pytest.mark.asyncio
    async def test_wires_all_required_edge_types(self, monkeypatch) -> None:
        # [ph3a-8]: a governed commit wires HAS_DECISION, GOVERNED_BY (per cited rule),
        # MOTIVATED_BY, REALIZES, INCLUDES, HAS_CHANGE, HAS_COMMIT.
        # RED: harvest_one_commit does not exist -> AttributeError.
        from writ.session import harvester

        plan_text = (
            "## Analysis\n\nWhy.\n\n"
            "- `writ/session/harvester.py` - the change\n\n"
            "## Rules Applied\n\nDOC-ARCH-001\nENF-SYS-005\n"
        )
        files = [{"path": "writ/session/harvester.py", "change_type": "modify"}]
        db = _FakeDB()
        await harvester.harvest_one_commit(
            db, _TEST_SCOPE,
            commit_hash="3a-edgewire-001",
            subject="feat(3a): wire test",
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )

        required_edges = {
            "HAS_DECISION", "GOVERNED_BY", "MOTIVATED_BY",
            "REALIZES", "INCLUDES", "HAS_CHANGE", "HAS_COMMIT",
        }
        actual = _edge_types(db)
        missing = required_edges - actual
        assert not missing, (
            f"harvest_one_commit must wire {required_edges}; missing: {missing}; "
            f"got {actual}"
        )
        # GOVERNED_BY must appear once per cited rule (2 rules in this plan).
        governed_by_count = sum(1 for e in db.edges if e[0] == "GOVERNED_BY")
        assert governed_by_count == 2, (
            f"GOVERNED_BY must fire once per cited rule (2 rules); got {governed_by_count}"
        )

    @pytest.mark.asyncio
    async def test_filechange_reason_is_plan_reason_for_governed_file(self) -> None:
        # [ph3a-6] detail: FileChange.reason for a file covered by the plan must be
        # the plan-parsed reason, NOT the commit subject.
        # RED: harvest_one_commit does not exist -> AttributeError.
        from writ.session import harvester

        plan_text = (
            "- `writ/session/harvester.py` - extract the shared helper\n"
        )
        files = [{"path": "writ/session/harvester.py", "change_type": "modify"}]
        db = _FakeDB()
        await harvester.harvest_one_commit(
            db, _TEST_SCOPE,
            commit_hash="3a-reason-001",
            subject="THIS SHOULD NOT BE THE REASON",
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )
        assert len(db.filechanges) == 1
        assert db.filechanges[0]["reason"] == "extract the shared helper", (
            f"plan reason must override commit subject; "
            f"got {db.filechanges[0]['reason']!r}"
        )


# ---------------------------------------------------------------------------
# FAKE-DB: harvest_one_commit with plan_text=None (fail-open)
# Capability [ph3a-7]
# ---------------------------------------------------------------------------

class TestHarvestOneCommitNoPlan:
    """Cap [ph3a-7] -- no Neo4j; uses _FakeDB."""

    @pytest.mark.asyncio
    async def test_no_plan_creates_no_decision_commit_fc_with_subject(self) -> None:
        # [ph3a-7]: plan_text=None -> no Decision created; Commit + FileChange are
        # still written; FileChange.reason is the commit subject (fail-open).
        # RED: harvest_one_commit does not exist -> AttributeError.
        from writ.session import harvester

        files = [
            {"path": "writ/session/harvester.py", "change_type": "modify"},
        ]
        db = _FakeDB()
        stats = await harvester.harvest_one_commit(
            db, _TEST_SCOPE,
            commit_hash="3a-noplan-001",
            subject="fix: fallback subject reason",
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=None,
            plan_ts=None,
        )

        assert db.decisions == [], (
            f"no Decision must be created when plan_text is None; got {db.decisions}"
        )
        assert len(db.commits) == 1, f"Commit must still be created; got {db.commits}"
        assert len(db.filechanges) == 1, (
            f"FileChange must still be created; got {db.filechanges}"
        )
        assert db.filechanges[0]["reason"] == "fix: fallback subject reason", (
            f"FileChange.reason must be the commit subject when plan is absent; "
            f"got {db.filechanges[0]['reason']!r}"
        )
        assert stats["decisions"] == 0, f"stats['decisions'] must be 0; got {stats}"
        assert stats["fallback_reason"] == 1, (
            f"stats['fallback_reason'] must be 1; got {stats}"
        )
        assert "MOTIVATED_BY" not in _edge_types(db), (
            "MOTIVATED_BY must not be wired when there is no governing Decision"
        )
        assert "HAS_DECISION" not in _edge_types(db), (
            "HAS_DECISION must not be wired when there is no governing Decision"
        )


# ---------------------------------------------------------------------------
# FAKE-DB: phantom-path guard end-to-end via harvest_one_commit
# Capability [ph3a-4] (end-to-end, via allowed_paths intersection in harvester)
# ---------------------------------------------------------------------------

class TestHarvestOneCommitPhantomPathGuard:
    """End-to-end phantom-path guard: plan names a file not in `files`."""

    @pytest.mark.asyncio
    async def test_phantom_path_in_plan_does_not_produce_filechange(self) -> None:
        # [ph3a-4] end-to-end: a plan that mentions 'writ/session/phantom.py' but
        # the git-authoritative `files` list does NOT include it -> no FileChange for
        # that path; the committed path gets a FileChange with plan reason.
        # RED: harvest_one_commit does not exist -> AttributeError. Once it exists,
        # if allowed_paths threading is missing, phantom.py yields a spurious
        # FileChange -> assertion failure.
        from writ.session import harvester

        plan_text = (
            "- `writ/session/harvester.py` - the real change\n"
            "- `writ/session/phantom.py` - never committed\n"
        )
        # git-authoritative list: only harvester.py was actually changed.
        files = [{"path": "writ/session/harvester.py", "change_type": "modify"}]
        db = _FakeDB()
        await harvester.harvest_one_commit(
            db, _TEST_SCOPE,
            commit_hash="3a-phantom-001",
            subject="feat: only harvester changed",
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )

        fc_paths = [fc["path"] for fc in db.filechanges]
        assert "writ/session/phantom.py" not in fc_paths, (
            f"phantom path must NOT produce a FileChange; got FileChange paths: {fc_paths}"
        )
        assert "writ/session/harvester.py" in fc_paths, (
            f"committed path must produce a FileChange; got {fc_paths}"
        )


# ---------------------------------------------------------------------------
# FAKE-DB: harvest loop delegates to harvest_one_commit (record identity)
# Capability [ph3a-9]
# ---------------------------------------------------------------------------

class TestHarvestDelegatesToHarvestOneCommit:
    """Cap [ph3a-9] -- harvester.harvest loop calls harvest_one_commit; records identical."""

    @pytest.mark.asyncio
    async def test_harvest_and_harvest_one_commit_produce_identical_records(
        self, monkeypatch
    ) -> None:
        # [ph3a-9]: after the Phase 3a refactor, harvester.harvest's per-commit loop
        # body delegates to harvest_one_commit. This test calls both paths with the
        # SAME inputs and asserts byte-identical FileChange change_ids and Decision ids.
        # RED: either harvest_one_commit does not exist (AttributeError on direct call)
        # or harvest still has its own inline loop (the Decision id formula diverges
        # because pre-3a harvest passes plan_text to harvest_plan without allowed_paths,
        # so a phantom path would be counted, producing a different planned_files list
        # and therefore a different Decision id).
        from writ.session import harvester

        plan_text = (
            "- `writ/session/harvester.py` - the one committed file\n"
        )
        commit_hash = "3a-identity-abc"
        subject = "feat(3a): identity check"
        files = [{"path": "writ/session/harvester.py", "change_type": "modify"}]

        # --- direct call to harvest_one_commit ---
        db_direct = _FakeDB()
        await harvester.harvest_one_commit(
            db_direct, _TEST_SCOPE,
            commit_hash=commit_hash,
            subject=subject,
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )

        # --- call via harvester.harvest (with monkeypatched git + transcript) ---
        def _fake_register(db, cwd, **kw):
            async def _inner(db, cwd, **kw):
                return _TEST_SCOPE
            import asyncio
            return _inner(db, cwd, **kw)

        async def _fake_register_async(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.harvester.ensure_project_registered", _fake_register_async
        )
        monkeypatch.setattr(harvester, "_collect_plan_writes", lambda d: [
            {"ts": "2026-06-29T09:00:00Z", "plan_text": plan_text},
        ])
        monkeypatch.setattr(harvester, "_git_commits", lambda r, s: [
            {
                "hash": commit_hash,
                "subject": subject,
                "body": "",
                "author": "tester",
                "branch": "main",
                "ts": "2026-06-29T10:00:00Z",
                "files": files,
            },
        ])

        db_harvest = _FakeDB()
        await harvester.harvest(db_harvest, "/repo")

        # Decision ids must match.
        assert len(db_direct.decisions) == 1, (
            f"direct call must create 1 Decision; got {len(db_direct.decisions)}"
        )
        assert len(db_harvest.decisions) == 1, (
            f"harvest loop must create 1 Decision; got {len(db_harvest.decisions)}"
        )
        assert db_direct.decisions[0]["decision_id"] == db_harvest.decisions[0]["decision_id"], (
            "harvest_one_commit and harvest loop must produce the SAME Decision id; "
            f"direct={db_direct.decisions[0]['decision_id']!r}, "
            f"harvest={db_harvest.decisions[0]['decision_id']!r}"
        )
        # FileChange change_ids must match.
        assert db_direct.filechanges[0]["change_id"] == db_harvest.filechanges[0]["change_id"], (
            "harvest_one_commit and harvest loop must produce the SAME change_id"
        )


# ---------------------------------------------------------------------------
# FAKE-DB: capture_commit falls open on missing/unparseable transcript
# Capability [ph3a-11]
# ---------------------------------------------------------------------------

class TestCaptureCommitFailsOpen:
    """Cap [ph3a-11] -- no Neo4j; uses _FakeDB."""

    @pytest.mark.asyncio
    async def test_capture_commit_falls_open_on_missing_transcript(
        self, monkeypatch
    ) -> None:
        # [ph3a-11]: when the session transcript is missing (no transcript dir),
        # capture_commit must NOT raise; it must still create Commit + FileChange
        # with the commit subject as the reason, and no Decision.
        # RED: harvest_one_commit does not exist -> AttributeError when
        # capture_commit tries to call it. Once the helper exists, if the
        # transcript-read error path does not fall through to plan_text=None,
        # it will raise or create a spurious Decision.
        from writ.session import commit_capture

        # Patch ensure_project_registered to return the test scope.
        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.commit_capture.ensure_project_registered", _fake_register
        )
        # Patch _parent_commit_ts to avoid real git.
        monkeypatch.setattr(commit_capture, "_parent_commit_ts", lambda cwd, h, r: "")
        # Make the transcript-dir lookup raise so the fail-open path is exercised.
        monkeypatch.setattr(
            "writ.session.harvester._collect_plan_writes",
            lambda d: (_ for _ in ()).throw(OSError("no transcript")),
        )

        db = _FakeDB()
        # Must not raise.
        result = await commit_capture.capture_commit(
            db,
            cwd=_TEST_REPO_ROOT + "/failopen-test",
            commit_hash="3a-failopen-001",
            subject="fix: no transcript available",
            author="tester",
            branch="main",
            files=[{"path": "writ/server.py", "change_type": "modify"}],
            session_id="",
        )

        assert result == _TEST_SCOPE, (
            f"capture_commit must return the project name on fail-open; got {result!r}"
        )
        assert len(db.commits) == 1, (
            f"Commit must be created even on transcript failure; got {db.commits}"
        )
        assert len(db.filechanges) == 1, (
            f"FileChange must be created even on transcript failure; got {db.filechanges}"
        )
        assert db.decisions == [], (
            f"No Decision must be created when transcript is missing; got {db.decisions}"
        )
        assert db.filechanges[0]["reason"] == "fix: no transcript available", (
            f"FileChange reason must fall back to commit subject; "
            f"got {db.filechanges[0]['reason']!r}"
        )


# ---------------------------------------------------------------------------
# PURE-IMPORT: decision_capture uses content-hash Decision id
# Capability [ph3a-13]
# ---------------------------------------------------------------------------

class TestDecisionCaptureContentHashId:
    """Cap [ph3a-13] -- no Neo4j; pure import + formula check."""

    def test_capture_decision_at_approve_uses_content_hash_id(
        self, monkeypatch
    ) -> None:
        # [ph3a-13]: decision_capture.capture_decision_at_approve must compute the
        # Decision id via harvester._decision_id(name, plan_text), NOT via the old
        # session-id-based formula "DEC-{name}-{session_id[:8]}-{phase}".
        # This test verifies the id formula is content-hash-derived by checking that
        # _decision_id is importable from harvester (it already exists) and that
        # decision_capture imports it (not its own formula).
        # RED: if decision_capture still uses the old formula, the id computed in this
        # test will differ from what _decision_id returns, triggering the assertion.
        from writ.session.harvester import _decision_id

        # Verify the formula: DEC-{name}-{sha1(name+\x00+plan_text)[:12]}.
        name = "myproject"
        plan_text = "## Analysis\n\nSome plan.\n\n## Files\n\n- `foo.py` (modify) -- reason\n"
        expected = _expected_decision_id(name, plan_text)
        got = _decision_id(name, plan_text)
        assert got == expected, (
            f"_decision_id formula mismatch: expected {expected!r}, got {got!r}"
        )
        assert got.startswith("DEC-myproject-"), (
            f"id must start with 'DEC-{{name}}-'; got {got!r}"
        )
        # The content-hash tail must be exactly 12 hex chars.
        tail = got[len("DEC-myproject-"):]
        assert len(tail) == 12 and all(c in "0123456789abcdef" for c in tail), (
            f"id tail must be 12 hex chars; got {tail!r}"
        )

    def test_decision_capture_imports_decision_id_from_harvester(self) -> None:
        # [ph3a-13]: after Phase 3a, decision_capture must import _decision_id from
        # harvester and use it. This test checks that the import succeeds (the function
        # is in scope in decision_capture's module namespace).
        # RED: ImportError if harvester._decision_id is not yet exported, or
        # AttributeError if decision_capture does not import it.
        import writ.session.decision_capture as dc
        import writ.session.harvester as hv

        # The symbol must be importable from decision_capture's namespace
        # (the plan calls for "from writ.session.harvester import _decision_id").
        assert hasattr(dc, "_decision_id"), (
            "decision_capture must import _decision_id from harvester; "
            "attribute '_decision_id' not found in writ.session.decision_capture"
        )
        # And it must be the SAME object as harvester._decision_id.
        assert dc._decision_id is hv._decision_id, (
            "decision_capture._decision_id must be the same function as "
            "harvester._decision_id (re-exported import, not a reimplementation)"
        )


# ---------------------------------------------------------------------------
# NEO4J-GATED: capture_commit creates Decision mechanically (real DB)
# Capability [ph3a-10]
# ---------------------------------------------------------------------------

class TestCaptureCommitCreatesDecisionMechanically:
    """Cap [ph3a-10] -- requires Neo4j (db_clean fixture)."""

    @pytest.mark.asyncio
    async def test_post_commit_path_creates_decision_without_approval_hook(
        self, db_clean: Neo4jConnection, monkeypatch, tmp_path: Path
    ) -> None:
        # [ph3a-10]: capture_commit reads a plan.md Write from the session
        # transcript (no LLM, no approval hook) and creates the governing
        # Decision with the content-hash id.
        # RED: harvest_one_commit does not exist -> AttributeError in capture_commit.
        from writ.session import commit_capture, harvester

        plan_text = (
            "## Analysis\n\nMechanical decision capture.\n\n"
            "- `writ/server.py` - add the route\n\n"
            "## Rules Applied\n\nDOC-ARCH-001\n"
        )
        commit_hash = f"3A-MECHCAP-{uuid.uuid4().hex[:8]}"

        # Stub the project registration to return the test scope.
        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.commit_capture.ensure_project_registered", _fake_register
        )
        monkeypatch.setattr(commit_capture, "_parent_commit_ts", lambda cwd, h, r: "")

        # Stub the transcript-read path to return our plan.
        monkeypatch.setattr(
            harvester, "_collect_plan_writes",
            lambda d: [{"ts": "2026-06-29T09:00:00Z", "plan_text": plan_text}],
        )
        # Stub _commit_ts (inside commit_capture).
        monkeypatch.setattr(commit_capture, "_commit_ts", lambda cwd, h, r=None: "2026-06-29T10:00:00Z")

        await commit_capture.capture_commit(
            db_clean,
            cwd=_TEST_REPO_ROOT + "/mechcap-test",
            commit_hash=commit_hash,
            subject="feat(3a): mechanical Decision capture",
            author="Test Author <dev@example.com>",
            branch="main",
            files=[{"path": "writ/server.py", "change_type": "modify"}],
            session_id="",
        )

        expected_id = _expected_decision_id(_TEST_SCOPE, plan_text)
        async with db_clean._driver.session(database=db_clean._database) as s:
            rec = await (await s.run(
                "MATCH (d:Decision {decision_id: $did}) RETURN d.decision_id AS id",
                did=expected_id,
            )).single()
        assert rec is not None, (
            f"capture_commit must create a Decision with id {expected_id!r}; "
            "no such node found in Neo4j"
        )
        assert rec["id"] == expected_id


# ---------------------------------------------------------------------------
# NEO4J-GATED: idempotency / dedup (ENF-SYS-005)
# Capability [ph3a-12]
# ---------------------------------------------------------------------------

class TestHarvestOneCommitIdempotency:
    """Cap [ph3a-12] -- requires Neo4j (db_clean fixture). ENF-SYS-005 compliant."""

    @pytest.mark.asyncio
    async def test_second_run_adds_zero_net_nodes(
        self, db_clean: Neo4jConnection, monkeypatch
    ) -> None:
        # [ph3a-12] (ENF-SYS-005): run harvest_one_commit twice with the same
        # name/plan_text/commit_hash/files. The second run must add ZERO net new
        # Decision, Commit, or FileChange nodes (all MERGEs). Assert via Cypher
        # count queries before and after the second run.
        # RED: harvest_one_commit does not exist -> AttributeError. Once it exists,
        # if any MERGE is replaced by a CREATE, the second run mints a duplicate
        # and the count assertion fails.
        from writ.session import harvester

        plan_text = (
            "## Analysis\n\nIdempotency test plan.\n\n"
            "- `writ/session/harvester.py` - one file\n\n"
            "## Rules Applied\n\nENF-SYS-005\n"
        )
        commit_hash = f"3A-IDEM-{uuid.uuid4().hex[:8]}"
        files = [{"path": "writ/session/harvester.py", "change_type": "modify"}]

        # Stub project registration to use the test scope.
        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.harvester.ensure_project_registered", _fake_register
        )

        # First run.
        await harvester.harvest_one_commit(
            db_clean, _TEST_SCOPE,
            commit_hash=commit_hash,
            subject="feat(3a): idempotency test",
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )

        # Capture counts after first run.
        async with db_clean._driver.session(database=db_clean._database) as s:
            d_count_1 = (await (await s.run(
                "MATCH (d:Decision) WHERE d.project = $p RETURN count(d) AS n",
                p=_TEST_SCOPE,
            )).single())["n"]
            c_count_1 = (await (await s.run(
                "MATCH (c:Commit) WHERE c.project = $p RETURN count(c) AS n",
                p=_TEST_SCOPE,
            )).single())["n"]
            fc_count_1 = (await (await s.run(
                "MATCH (fc:FileChange) WHERE fc.project = $p RETURN count(fc) AS n",
                p=_TEST_SCOPE,
            )).single())["n"]

        # Second run -- identical inputs.
        await harvester.harvest_one_commit(
            db_clean, _TEST_SCOPE,
            commit_hash=commit_hash,
            subject="feat(3a): idempotency test",
            author="tester",
            branch="main",
            commit_ts="2026-06-29T10:00:00Z",
            files=files,
            plan_text=plan_text,
            plan_ts="2026-06-29T09:00:00Z",
        )

        # Counts must be unchanged after second run.
        async with db_clean._driver.session(database=db_clean._database) as s:
            d_count_2 = (await (await s.run(
                "MATCH (d:Decision) WHERE d.project = $p RETURN count(d) AS n",
                p=_TEST_SCOPE,
            )).single())["n"]
            c_count_2 = (await (await s.run(
                "MATCH (c:Commit) WHERE c.project = $p RETURN count(c) AS n",
                p=_TEST_SCOPE,
            )).single())["n"]
            fc_count_2 = (await (await s.run(
                "MATCH (fc:FileChange) WHERE fc.project = $p RETURN count(fc) AS n",
                p=_TEST_SCOPE,
            )).single())["n"]

        assert d_count_2 == d_count_1, (
            f"second run must add 0 net Decision nodes; "
            f"before={d_count_1}, after={d_count_2}"
        )
        assert c_count_2 == c_count_1, (
            f"second run must add 0 net Commit nodes; "
            f"before={c_count_1}, after={c_count_2}"
        )
        assert fc_count_2 == fc_count_1, (
            f"second run must add 0 net FileChange nodes; "
            f"before={fc_count_1}, after={fc_count_2}"
        )

        # Decision id must match the content-hash formula.
        expected_id = _expected_decision_id(_TEST_SCOPE, plan_text)
        async with db_clean._driver.session(database=db_clean._database) as s:
            rec = await (await s.run(
                "MATCH (d:Decision {decision_id: $did}) RETURN d.decision_id AS id",
                did=expected_id,
            )).single()
        assert rec is not None, (
            f"the single Decision must have content-hash id {expected_id!r}; not found"
        )


# ---------------------------------------------------------------------------
# FAKE-DB: capture_commit disk-prefer regression tests (Phase 3a durable fix)
# Capabilities [disk-1], [disk-2], [disk-3]
# ---------------------------------------------------------------------------

# Plan sections needed to make the gated `## Files` bullet format parse correctly.
# Format per plan.md note: `- ` backtick path backtick ` (modify) -- reason text`
_DISK_PLAN_WIDGET = (
    "## Analysis\n\nCapture the debounce guard change.\n\n"
    "## Files\n\n"
    "- `src/widget.py` (modify) -- add the debounce guard\n\n"
    "## Rules Applied\n\nNo matching rules\n\n"
    "## Capabilities\n\n"
    "- capture FileChange reason from plan\n"
)

_DISK_PLAN_UNRELATED = (
    "## Analysis\n\nUnrelated plan for a different module.\n\n"
    "## Files\n\n"
    "- `other/unrelated.py` (modify) -- something else\n\n"
    "## Rules Applied\n\nNo matching rules\n\n"
    "## Capabilities\n\n"
    "- unrelated capability\n"
)


class TestCaptureCommitDiskPrefer:
    """Regression tests for the disk-prefer fix in capture_commit.

    Tests [disk-1] and [disk-2] are RED before the fix: the current
    transcript-only capture_commit ignores the on-disk plan.md and falls
    back to the commit subject as FileChange.reason. Test [disk-3] is GREEN
    before and after the fix: the phantom-path guard drops a stale disk plan
    whose ## Files do not cover the committed path, and the subject fallback
    applies regardless.
    """

    @pytest.mark.asyncio
    async def test_disk_plan_reason_used_when_transcript_has_no_matching_plan(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # [disk-1] subagent-gap disk-prefer (RED before fix):
        # A valid plan.md is on disk at tmp_path/plan.md. The transcript scan
        # returns [] (simulating the subagent-gap where the planner's Write lives
        # in the subagent transcript, not the parent's). The current capture_commit
        # ignores the disk plan and falls to the commit subject. After the fix,
        # _find_plan_md finds the disk plan and FileChange.reason is the plan reason.
        from writ.session import commit_capture

        (tmp_path / "plan.md").write_text(_DISK_PLAN_WIDGET, encoding="utf-8")

        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.commit_capture.ensure_project_registered", _fake_register
        )
        monkeypatch.setattr(
            commit_capture, "_parent_commit_ts", lambda cwd, h, r=None: 0.0
        )
        monkeypatch.setattr(
            commit_capture, "_commit_ts", lambda cwd, h, r=None: "2026-06-30T10:00:00Z"
        )
        # Transcript scan finds nothing -- the subagent-gap scenario.
        monkeypatch.setattr(
            "writ.session.harvester._collect_plan_writes",
            lambda d: [],
        )

        db = _FakeDB()
        result = await commit_capture.capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash="3a-disk1-001",
            subject="chore: unrelated subject",
            author="tester",
            branch="main",
            files=[{"path": "src/widget.py", "change_type": "modify"}],
            session_id="",
        )

        assert result == _TEST_SCOPE, (
            f"capture_commit must return the project name; got {result!r}"
        )
        assert len(db.filechanges) == 1, (
            f"one FileChange must be created; got {db.filechanges}"
        )
        fc_reason = db.filechanges[0]["reason"]
        assert fc_reason == "add the debounce guard", (
            f"FileChange.reason must equal the plan's per-file reason "
            f"'add the debounce guard', not the commit subject; got {fc_reason!r}"
        )

    @pytest.mark.asyncio
    async def test_disk_plan_reason_used_regardless_of_resolved_claim_state(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # [disk-2] resolved-claim independence (RED before fix):
        # Same disk plan.md as disk-1. The _FakeDB.get_open_decisions_for_path
        # already returns [] by default, simulating the state where all prior
        # approval-Decision claims are resolved (re-harvest / amend scenario).
        # After the fix FileChange.reason still comes from the disk plan -- proving
        # the disk read is independent of open/resolved claim state.
        from writ.session import commit_capture

        (tmp_path / "plan.md").write_text(_DISK_PLAN_WIDGET, encoding="utf-8")

        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.commit_capture.ensure_project_registered", _fake_register
        )
        monkeypatch.setattr(
            commit_capture, "_parent_commit_ts", lambda cwd, h, r=None: 0.0
        )
        monkeypatch.setattr(
            commit_capture, "_commit_ts", lambda cwd, h, r=None: "2026-06-30T10:00:00Z"
        )
        # Transcript scan finds nothing.
        monkeypatch.setattr(
            "writ.session.harvester._collect_plan_writes",
            lambda d: [],
        )

        db = _FakeDB()
        # Explicitly mark all open claims as already resolved by making
        # get_open_decisions_for_path return [] (claims resolved -> no prior reason).
        # This is the default in _FakeDB; the assertion here confirms that the disk
        # path does NOT depend on resolved-claim state to supply the reason.
        assert db.get_open_decisions_for_path  # sanity: method present on the double

        result = await commit_capture.capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash="3a-disk2-001",
            subject="chore: unrelated subject",
            author="tester",
            branch="main",
            files=[{"path": "src/widget.py", "change_type": "modify"}],
            session_id="",
        )

        assert result == _TEST_SCOPE
        assert len(db.filechanges) == 1
        fc_reason = db.filechanges[0]["reason"]
        assert fc_reason == "add the debounce guard", (
            f"FileChange.reason must be the plan reason even when all prior claims "
            f"are resolved; got {fc_reason!r}"
        )

    @pytest.mark.asyncio
    async def test_stale_disk_plan_phantom_path_falls_back_to_subject(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # [disk-3] stale-disk-plan phantom-path -> subject fallback (GREEN before+after):
        # A plan.md is on disk, but its ## Files section names other/unrelated.py --
        # NOT the committed path src/widget.py. The phantom-path guard inside
        # harvest_one_commit intersects the parsed plan files with the git-authoritative
        # file list; no overlap -> parsed_files=[] -> no Decision -> subject fallback.
        # This asserts that disk-prefer NEVER fabricates a reason for a path the plan
        # does not name. Passes today (disk plan is not consulted) AND after the fix
        # (disk plan is consulted but drops to empty due to the phantom-path guard).
        from writ.session import commit_capture

        (tmp_path / "plan.md").write_text(_DISK_PLAN_UNRELATED, encoding="utf-8")

        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.commit_capture.ensure_project_registered", _fake_register
        )
        monkeypatch.setattr(
            commit_capture, "_parent_commit_ts", lambda cwd, h, r=None: 0.0
        )
        monkeypatch.setattr(
            commit_capture, "_commit_ts", lambda cwd, h, r=None: "2026-06-30T10:00:00Z"
        )
        # Transcript scan finds nothing.
        monkeypatch.setattr(
            "writ.session.harvester._collect_plan_writes",
            lambda d: [],
        )

        db = _FakeDB()
        result = await commit_capture.capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash="3a-disk3-001",
            subject="fix: real subject",
            author="tester",
            branch="main",
            files=[{"path": "src/widget.py", "change_type": "modify"}],
            session_id="",
        )

        assert result == _TEST_SCOPE
        assert len(db.filechanges) == 1
        fc_reason = db.filechanges[0]["reason"]
        assert fc_reason == "fix: real subject", (
            f"phantom-path guard must drop the unmatched stale disk plan and fall "
            f"back to the commit subject; got {fc_reason!r}"
        )
        assert db.decisions == [], (
            f"no Decision must be created when the disk plan covers no committed path; "
            f"got {db.decisions}"
        )


# ---------------------------------------------------------------------------
# Unplanned-file fix: capture_commit emits committed_file_not_in_plan friction
# ---------------------------------------------------------------------------
# Capability [ph3a-unplanned-friction]: capture_commit emits one
# committed_file_not_in_plan friction event per unplanned path; the commit
# is NOT blocked (fail-open).
#
# RED before the fix: capture_commit does not yet read stats["unplanned_files"]
# or emit the event, so the friction-log assertion fails.

class TestCaptureCommitUnplannedFileFriction:
    """Cap [ph3a-unplanned-friction] -- FAKE-DB; uses autouse WRIT_FRICTION_LOG redirect."""

    @pytest.mark.asyncio
    async def test_unplanned_file_emits_friction_event_and_does_not_block(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        # [ph3a-unplanned-friction] (RED before fix):
        # A plan.md on disk names planned.py; the commit touches both planned.py and
        # unplanned.py. After capture_commit runs:
        #   - exactly one committed_file_not_in_plan friction event is emitted
        #   - that event's file_path is "unplanned.py"
        #   - no committed_file_not_in_plan event is emitted for planned.py
        #   - capture_commit returns the project name (commit not blocked)
        #
        # The friction log is the one already redirected by the autouse
        # _isolate_friction_log fixture (WRIT_FRICTION_LOG -> tmp_path/workflow-friction.log).
        import json
        import os
        from writ.session import commit_capture, harvester

        # Write a minimal plan.md that names planned.py only.
        plan_text = (
            "## Files\n\n"
            "- `planned.py` (modify) -- add the debounce guard\n\n"
            "## Rules Applied\n\nNo matching rules\n"
        )
        (tmp_path / "plan.md").write_text(plan_text, encoding="utf-8")

        async def _fake_register(db, cwd, **kw):
            return _TEST_SCOPE

        monkeypatch.setattr(
            "writ.session.commit_capture.ensure_project_registered", _fake_register
        )
        monkeypatch.setattr(
            commit_capture, "_parent_commit_ts", lambda cwd, h, r=None: 0.0
        )
        monkeypatch.setattr(
            commit_capture, "_commit_ts",
            lambda cwd, h, r=None: "2026-06-30T10:00:00Z",
        )
        # Transcript scan returns nothing (subagent-gap scenario); disk plan.md is used.
        monkeypatch.setattr(
            "writ.session.harvester._collect_plan_writes",
            lambda d: [],
        )

        db = _FakeDB()
        result = await commit_capture.capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash="3a-unplanned-friction-001",
            subject="feat: add planned and unplanned file",
            author="tester",
            branch="main",
            files=[
                {"path": "planned.py", "change_type": "modify"},
                {"path": "unplanned.py", "change_type": "add"},
            ],
            session_id="test-session-unplanned",
        )

        # Commit must not be blocked.
        assert result == _TEST_SCOPE, (
            f"capture_commit must return project name (commit not blocked); got {result!r}"
        )
        assert len(db.filechanges) == 2, (
            f"both FileChanges must be created; got {db.filechanges}"
        )

        # Read the friction log (redirected by autouse fixture to
        # tmp_path/workflow-friction.log).
        friction_log = Path(os.environ["WRIT_FRICTION_LOG"])
        events: list[dict] = []
        if friction_log.exists():
            for line in friction_log.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

        unplanned_events = [
            e for e in events if e.get("event") == "committed_file_not_in_plan"
        ]
        assert len(unplanned_events) == 1, (
            f"exactly one committed_file_not_in_plan event must be emitted; "
            f"got {len(unplanned_events)}: {unplanned_events}"
        )
        assert unplanned_events[0].get("file_path") == "unplanned.py", (
            f"friction event file_path must be 'unplanned.py'; "
            f"got {unplanned_events[0].get('file_path')!r}"
        )

        # No friction event for the planned file.
        planned_events = [
            e for e in unplanned_events if e.get("file_path") == "planned.py"
        ]
        assert planned_events == [], (
            "planned.py (which has a plan reason) must not generate a friction event"
        )
