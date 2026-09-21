"""Decision Memory Phase 1d: git hooks, commit-body, capture at commit, installer,
auto-install.

Test skeleton for the capability gate defined in capabilities.md and plan.md.
Every test in this file is RED until the implementer builds the corresponding
feature. Tests fail on ImportError/AttributeError/AssertionError for the missing
modules/methods/routes -- never on a harness error.

Run interpreter: .venv/bin/python -m pytest (has onnxruntime; system python3
errors on embedding imports).

Neo4j-gated tests use the db_clean fixture (scope "test-dm-1d", repo_root prefix
"/tmp/fake-test-1d-repo") and skip when Neo4j is unreachable.
Pure-Python tests (parse_name_status) run without Neo4j.
Server-route tests use FastAPI TestClient + monkeypatch writ.server._db.
Installer tests use tmp_path git repos; NEVER the real .git/hooks.

Capability map (39 items from capabilities.md):
  [parse-1]       parse_name_status parses A/M/D/T single-path lines
  [parse-2]       parse_name_status parses R/C rename lines
  [parse-3]       parse_name_status returns [] for empty input
  [db-1]          get_open_decisions_for_path returns open Decisions sorted ts-desc
  [db-2]          resolve_file_claims flips resolved False->True on every Decision
  [edge-inc]      wire_includes wires Commit-INCLUDES->FileChange
  [edge-mot]      wire_motivated_by wires FileChange-MOTIVATED_BY->Decision
  [edge-rea]      wire_realizes wires Commit-REALIZES->Decision
  [edge-hc]       wire_has_change and wire_has_commit wire Project record edges
  [resolve-1]     resolve_reasons_for_files attaches reason/decision_id/rules per file
  [cap-1]         capture_commit registers Project before any record write
  [cap-2]         capture_commit creates Commit + one FileChange per file
  [cap-3]         capture_commit derives deterministic change_id (re-MERGE idempotency)
  [cap-4]         capture_commit wires INCLUDES, MOTIVATED_BY, REALIZES (Cypher-verified)
  [cap-5]         capture_commit resolves every planning Decision's claim per committed file
  [cap-6]         capture_commit is idempotent (re-run re-MERGEs, claim resolve is no-op)
  [hook-pc-1]     post-commit fails open (skip graph write, exits 0) when daemon down
  [route-cap-1]   POST /commit/capture creates records + edges
  [route-cap-2]   POST /commit/capture guards _db is None
  [route-cap-3]   POST /commit/capture logs + returns on exception (fail-open)
  [inst-1]        install_git_hooks writes the post-commit hook with marker, chmod 0o755; no prepare-commit-msg
  [inst-2]        install_git_hooks is idempotent (marker appears exactly once)
  [inst-3]        install_git_hooks coexists with pre-existing hook content
  [inst-4]        git_hooks_installed returns True/False correctly
  [inst-5]        uninstall_git_hooks strips only the marked block
  [auto-1]        auto-install route: installs when marker absent
  [auto-2]        auto-install route: no-op when marker present (already=true)
  [auto-3]        auto-install route: guards _db is None
  [auto-4]        auto-install fail-open guards (work-mode-only, not-in-repo) at route level
  [cli-1]         writ git-hooks install / uninstall CLI commands
  [cli-2]         writ git-hooks bootstrap registers the writ project with remote_url

ENF-SYS-005 note: db-1, db-2, edge-*, cap-1 through cap-6, route-cap-1, and cli-2
require a real Neo4j connection to validate MERGE semantics and graph state. Mock-only
tests of those behaviors would prove nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

# ruff: noqa: F811 -- the shared client/isolated_cache fixtures below are consumed
# as test-method parameters, which ruff misreads as redefinitions of this import.
from tests.fixtures.server_routes import client, isolated_cache  # noqa: F401
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_SCOPE = "test-dm-1d"
_TEST_REPO_ROOT = "/tmp/fake-test-1d-repo"
_TEST_BIBLE_ROOT = "bible"

# Writ marker prefix used by install_git_hooks (plan.md installer section).
_WRIT_MARKER = "# >>> Writ"


# ---------------------------------------------------------------------------
# Runner stub helpers (same pattern as test_decision_memory_capture.py:218-249)
# ---------------------------------------------------------------------------

def _make_runner(responses: dict[str, subprocess.CompletedProcess]) -> Callable:
    """Build a subprocess.run-shaped callable from a token->result mapping."""
    def _runner(args, *, cwd=None, capture_output=False, text=False, timeout=None, **_):
        for token, result in responses.items():
            if token in args:
                return result
        tokens_seen = [a for a in args if isinstance(a, str)]
        raise KeyError(
            f"runner stub has no mapping for args tokens {tokens_seen!r}; "
            f"registered tokens: {list(responses)!r}"
        )
    return _runner


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _runner_no_repo() -> Callable:
    """Runner stub for a cwd that is NOT inside any git work tree."""
    return _make_runner({
        "rev-parse": _completed(128, stderr="fatal: not a git repository\n"),
    })


def _runner_with_remote(repo_root: str, remote_url: str) -> Callable:
    """Runner stub for a cwd inside a git repo with a remote."""
    return _make_runner({
        "rev-parse": _completed(0, stdout=repo_root + "\n"),
        "get-url": _completed(0, stdout=remote_url + "\n"),
    })


# ---------------------------------------------------------------------------
# Git tmp-repo helper (for installer + hook tests)
# ---------------------------------------------------------------------------

def _init_tmp_repo(base: Path) -> Path:
    """Create a minimal git repo in base; return its root Path."""
    base.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(base)], check=True, capture_output=True)
    return base


def _hooks_dir(repo: Path) -> Path:
    """Resolve the hooks directory for a (possibly worktree) repo."""
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    )
    git_common = result.stdout.strip()
    if not os.path.isabs(git_common):
        git_common = str(repo / git_common)
    return Path(git_common) / "hooks"


# ---------------------------------------------------------------------------
# Neo4j-gated fixture -- scope "test-dm-1d", two-pass :Project teardown
# (mirrors test_decision_memory_capture.py:258-319, adjusted for 1d prefix)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_clean():
    """Connect to Neo4j, wipe test-dm-1d project scope, yield, clean up.

    Skips when Neo4j is unreachable. Two-pass :Project teardown:
    1. By name prefix "test-dm-1d" -- catches nodes with explicit test names.
    2. By repo_root prefix "/tmp/fake-test-1d-repo" -- catches derived-name nodes.
    No production node uses /tmp/fake-test-1d-repo, so no leakage to live data.
    The live "writ" :Project is never touched.
    """
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")

    await _wipe_1d_test_data(conn)
    yield conn
    await _wipe_1d_test_data(conn)
    await conn.close()


async def _wipe_1d_test_data(conn: Neo4jConnection) -> None:
    """Wipe test-dm-1d project scope and all related test-seeded nodes."""
    await conn.clear_project(_TEST_SCOPE)
    async with conn._driver.session(database=conn._database) as s:
        # Pass 1: :Project by name prefix.
        await (await s.run(
            "MATCH (p:Project) WHERE p.name STARTS WITH $prefix DETACH DELETE p",
            prefix=_TEST_SCOPE,
        )).consume()
        # Pass 2: :Project by repo_root prefix (catches derived-name registrations).
        await (await s.run(
            "MATCH (p:Project) WHERE p.repo_root STARTS WITH $root_prefix DETACH DELETE p",
            root_prefix=_TEST_REPO_ROOT,
        )).consume()
        # :Decision nodes by project prefix.
        await (await s.run(
            "MATCH (d:Decision) WHERE d.project STARTS WITH $prefix DETACH DELETE d",
            prefix=_TEST_SCOPE,
        )).consume()
        # :Commit nodes by project prefix.
        await (await s.run(
            "MATCH (c:Commit) WHERE c.project STARTS WITH $prefix DETACH DELETE c",
            prefix=_TEST_SCOPE,
        )).consume()
        # :FileChange nodes by project prefix.
        await (await s.run(
            "MATCH (fc:FileChange) WHERE fc.project STARTS WITH $prefix DETACH DELETE fc",
            prefix=_TEST_SCOPE,
        )).consume()
        # Records written under derived project names (capture_commit derives the
        # project name from the runner remote_url, e.g. github.com/org1d/cap2).
        await (await s.run(
            "MATCH (n) WHERE n.project STARTS WITH 'github.com/org1d/' DETACH DELETE n",
        )).consume()
        # Test-seeded fake Rule nodes (TEST1D-* prefix).
        await (await s.run(
            "MATCH (r:Rule) WHERE r.rule_id STARTS WITH 'TEST1D' DETACH DELETE r",
        )).consume()
        # :Project nodes registered by the test remote-url org.
        await (await s.run(
            "MATCH (p:Project) WHERE p.remote_url STARTS WITH 'git@github.com:org1d/' "
            "DETACH DELETE p",
        )).consume()


# ---------------------------------------------------------------------------
# Server-route fixtures (client, isolated_cache) are shared -- imported at the
# top of this file from tests/fixtures/server_routes.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pure-Python: parse_name_status
# (capability: [parse-1], [parse-2], [parse-3])
# ---------------------------------------------------------------------------

class TestParseNameStatus:
    """Caps [parse-1], [parse-2], [parse-3] -- no Neo4j, no git required."""

    def test_parses_add_modify_delete_type_lines(self) -> None:
        # [parse-1]: A/M/D/T single-path TAB-split lines -> {path, change_type}.
        # RED: writ/session/name_status.py does not exist yet (ImportError).
        from writ.session.name_status import parse_name_status

        text = (
            "A\twrit/session/name_status.py\n"
            "M\twrit/graph/db.py\n"
            "D\told_module.py\n"
            "T\tsome_file.py\n"
        )
        result = parse_name_status(text)
        assert len(result) == 4, f"expected 4 entries, got {len(result)}: {result}"

        entry_map = {e["path"]: e for e in result}
        assert entry_map["writ/session/name_status.py"]["change_type"] == "add", (
            "A must map to 'add'"
        )
        assert entry_map["writ/graph/db.py"]["change_type"] == "modify", (
            "M must map to 'modify'"
        )
        assert entry_map["old_module.py"]["change_type"] == "delete", (
            "D must map to 'delete'"
        )
        # T (type-change) is a distinct git status; plan maps it to some defined change_type.
        assert "change_type" in entry_map["some_file.py"], (
            "T must produce an entry with a change_type field"
        )

    def test_parses_rename_lines_with_old_path(self) -> None:
        # [parse-2]: R/C rename lines (status carries similarity score + two paths)
        # -> {path, change_type, old_path}. The status field is "R100" or "C75" etc.
        # RED: ImportError.
        from writ.session.name_status import parse_name_status

        text = (
            "R100\told/path/foo.py\tnew/path/foo.py\n"
            "C75\tsrc/base.py\tsrc/derived.py\n"
        )
        result = parse_name_status(text)
        assert len(result) == 2, f"expected 2 rename entries, got {len(result)}: {result}"

        rename = result[0]
        assert rename["path"] == "new/path/foo.py", (
            "path must be the NEW path for a rename"
        )
        assert rename["old_path"] == "old/path/foo.py", (
            "old_path must be the OLD path for a rename"
        )
        assert rename["change_type"] == "rename", (
            "R status must map to change_type='rename'"
        )

        copy_entry = result[1]
        assert "old_path" in copy_entry, "C (copy) entry must have old_path"
        assert copy_entry["path"] == "src/derived.py"

    def test_returns_empty_list_for_empty_input(self) -> None:
        # [parse-3]: empty input (empty commit or clean merge) -> [].
        # RED: ImportError.
        from writ.session.name_status import parse_name_status

        assert parse_name_status("") == [], "empty string must return []"
        assert parse_name_status("\n\n") == [], "whitespace-only must return []"


# ---------------------------------------------------------------------------
# Neo4j-gated: get_open_decisions_for_path + resolve_file_claims
# (capability: [db-1], [db-2])
# ---------------------------------------------------------------------------

class TestGetOpenDecisionsForPath:
    """Caps [db-1] -- requires Neo4j."""

    @pytest.mark.asyncio
    async def test_returns_open_decisions_sorted_most_recent_first(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [db-1]: get_open_decisions_for_path returns only OPEN claims for the path,
        # sorted by ts descending, correctly parsing the JSON blob.
        # RED: method does not exist yet (AttributeError).
        path = "writ/graph/db.py"

        # Seed two Decisions with open claims on `path`, one already resolved.
        older_ts = "2026-06-25T10:00:00+00:00"
        newer_ts = "2026-06-26T10:00:00+00:00"
        did_older = f"TEST1D-OPEN-OLD-{uuid.uuid4().hex[:6]}"
        did_newer = f"TEST1D-OPEN-NEW-{uuid.uuid4().hex[:6]}"
        did_resolved = f"TEST1D-RESOLVED-{uuid.uuid4().hex[:6]}"

        for did, ts, resolved in [
            (did_older, older_ts, False),
            (did_newer, newer_ts, False),
            (did_resolved, newer_ts, True),
        ]:
            await db_clean.create_decision(
                decision_id=did,
                project=_TEST_SCOPE,
                title="Test decision",
                rationale="test",
                planned_files=[{"path": path, "reason": f"reason for {did}", "resolved": resolved}],
                governing_rule_ids=[],
                phase="planning",
                session_id=f"{_TEST_SCOPE}-{did[:8]}",
                ts=ts,
            )

        results = await db_clean.get_open_decisions_for_path(_TEST_SCOPE, path)

        assert len(results) == 2, (
            f"expected 2 open decisions, got {len(results)}: {[r.get('decision_id') for r in results]}"
        )
        # Most-recent first (ts-desc).
        assert results[0]["decision_id"] == did_newer, (
            f"first result must be the newer decision; got {results[0].get('decision_id')}"
        )
        assert results[1]["decision_id"] == did_older, (
            f"second result must be the older decision; got {results[1].get('decision_id')}"
        )
        # Resolved decision must be absent.
        ids = {r["decision_id"] for r in results}
        assert did_resolved not in ids, "resolved decision must NOT appear in open results"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_open_claims(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [db-1] edge case: empty-list edge case; no open claims -> [].
        # RED: AttributeError.
        results = await db_clean.get_open_decisions_for_path(_TEST_SCOPE, "nonexistent/path.py")
        assert results == [], f"expected [] for path with no claims, got {results}"

    @pytest.mark.asyncio
    async def test_ignores_decisions_with_empty_planned_files(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [db-1] edge case: a Decision with planned_files=[] (empty list) must not
        # error and must not appear in results (the _coerce_neo4j_value empty-list
        # gotcha: empty list passes through as native, not as '[]', plan.md:120-126).
        # RED: AttributeError.
        did_empty = f"TEST1D-EMPTY-{uuid.uuid4().hex[:6]}"
        await db_clean.create_decision(
            decision_id=did_empty,
            project=_TEST_SCOPE,
            title="Empty files decision",
            rationale="test",
            planned_files=[],
            governing_rule_ids=[],
            phase="planning",
            session_id=f"{_TEST_SCOPE}-empty",
            ts="2026-06-26T09:00:00+00:00",
        )
        results = await db_clean.get_open_decisions_for_path(_TEST_SCOPE, "any/path.py")
        ids = {r.get("decision_id") for r in results}
        assert did_empty not in ids, (
            "Decision with empty planned_files must not appear in results"
        )


class TestResolveFileClaims:
    """Caps [db-2] -- requires Neo4j."""

    @pytest.mark.asyncio
    async def test_flips_resolved_false_to_true_on_every_decision(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [db-2]: resolve_file_claims flips resolved False->True on EVERY Decision
        # that planned the path (RESOLVE-ON-EVERY-DECISION, plan.md:130-136).
        # RED: method does not exist yet (AttributeError).
        path = "writ/session/commit_capture.py"
        did_a = f"TEST1D-CLAIM-A-{uuid.uuid4().hex[:6]}"
        did_b = f"TEST1D-CLAIM-B-{uuid.uuid4().hex[:6]}"

        for did in (did_a, did_b):
            await db_clean.create_decision(
                decision_id=did,
                project=_TEST_SCOPE,
                title="Claim test",
                rationale="test",
                planned_files=[{"path": path, "reason": "some reason", "resolved": False}],
                governing_rule_ids=[],
                phase="planning",
                session_id=f"{_TEST_SCOPE}-{did[:8]}",
                ts="2026-06-26T11:00:00+00:00",
            )

        count = await db_clean.resolve_file_claims(_TEST_SCOPE, path)
        assert count == 2, f"resolve_file_claims must update 2 decisions; got {count}"

        # Verify both are now resolved via get_open_decisions_for_path (should be empty).
        open_after = await db_clean.get_open_decisions_for_path(_TEST_SCOPE, path)
        assert open_after == [], (
            "after resolve_file_claims, get_open_decisions_for_path must return [] "
            f"for the path; got {[r.get('decision_id') for r in open_after]}"
        )

    @pytest.mark.asyncio
    async def test_idempotent_never_flips_true_to_false(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [db-2]: a second call to resolve_file_claims on already-resolved decisions
        # is a no-op -- it must NEVER flip True->False (idempotent re-run).
        # RED: AttributeError.
        path = "writ/session/commit_capture_idempotent.py"
        did = f"TEST1D-IDEM-{uuid.uuid4().hex[:6]}"

        await db_clean.create_decision(
            decision_id=did,
            project=_TEST_SCOPE,
            title="Idempotent claim test",
            rationale="test",
            planned_files=[{"path": path, "reason": "reason", "resolved": False}],
            governing_rule_ids=[],
            phase="planning",
            session_id=f"{_TEST_SCOPE}-idem",
            ts="2026-06-26T11:30:00+00:00",
        )

        # First resolve.
        await db_clean.resolve_file_claims(_TEST_SCOPE, path)
        # Second resolve (re-run, must be no-op).
        await db_clean.resolve_file_claims(_TEST_SCOPE, path)

        # Still resolved -- not flipped back.
        open_after = await db_clean.get_open_decisions_for_path(_TEST_SCOPE, path)
        assert open_after == [], (
            "after idempotent re-run of resolve_file_claims, "
            "decisions must remain resolved (not flipped back to open)"
        )


# ---------------------------------------------------------------------------
# Neo4j-gated: wire_includes, wire_motivated_by, wire_realizes,
#              wire_has_change, wire_has_commit
# (capability: [edge-inc], [edge-mot], [edge-rea], [edge-hc])
# ---------------------------------------------------------------------------

class TestWireRecordEdges:
    """Caps [edge-inc], [edge-mot], [edge-rea], [edge-hc] -- requires Neo4j."""

    @pytest.mark.asyncio
    async def test_wire_includes_commit_to_filechange(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [edge-inc]: wire_includes creates exactly one INCLUDES edge
        # from Commit to FileChange, idempotent via MERGE.
        # RED: method does not exist yet (AttributeError).
        commit_hash = f"TEST1D-COMM-INC-{uuid.uuid4().hex[:8]}"
        change_id = f"TEST1D-FC-INC-{uuid.uuid4().hex[:8]}"

        await db_clean.create_commit(
            commit_hash=commit_hash, project=_TEST_SCOPE,
            subject="test includes", author="tester",
            branch="main", ts="2026-06-26T12:00:00+00:00",
        )
        await db_clean.create_filechange(
            change_id=change_id, project=_TEST_SCOPE,
            path="some/file.py", change_type="add",
            commit_hash=commit_hash, reason="", decision_id=None,
        )

        await db_clean.wire_includes(commit_hash, change_id, _TEST_SCOPE)

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (c:Commit {commit_hash: $ch})-[e:INCLUDES]->(fc:FileChange {change_id: $cid}) "
                "RETURN count(e) AS cnt",
                ch=commit_hash, cid=change_id,
            )
            record = await result.single()
        assert record["cnt"] == 1, (
            f"wire_includes must create exactly one INCLUDES edge; got cnt={record['cnt']}"
        )

    @pytest.mark.asyncio
    async def test_wire_motivated_by_filechange_to_decision(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [edge-mot]: wire_motivated_by creates exactly one MOTIVATED_BY edge
        # from FileChange to Decision.
        # RED: AttributeError.
        change_id = f"TEST1D-FC-MOT-{uuid.uuid4().hex[:8]}"
        decision_id = f"TEST1D-DEC-MOT-{uuid.uuid4().hex[:8]}"
        commit_hash = f"TEST1D-COMM-MOT-{uuid.uuid4().hex[:8]}"

        await db_clean.create_commit(
            commit_hash=commit_hash, project=_TEST_SCOPE,
            subject="test mot", author="tester",
            branch="main", ts="2026-06-26T12:00:00+00:00",
        )
        await db_clean.create_filechange(
            change_id=change_id, project=_TEST_SCOPE,
            path="some/file.py", change_type="modify",
            commit_hash=commit_hash, reason="test reason", decision_id=decision_id,
        )
        await db_clean.create_decision(
            decision_id=decision_id, project=_TEST_SCOPE,
            title="Test dec", rationale="r",
            planned_files=[{"path": "some/file.py", "reason": "r", "resolved": False}],
            governing_rule_ids=[], phase="planning",
            session_id=f"{_TEST_SCOPE}-mot", ts="2026-06-26T12:00:00+00:00",
        )

        await db_clean.wire_motivated_by(change_id, decision_id, _TEST_SCOPE)

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (fc:FileChange {change_id: $cid})-[e:MOTIVATED_BY]->(d:Decision {decision_id: $did}) "
                "RETURN count(e) AS cnt",
                cid=change_id, did=decision_id,
            )
            record = await result.single()
        assert record["cnt"] == 1, (
            f"wire_motivated_by must create exactly one MOTIVATED_BY edge; got cnt={record['cnt']}"
        )

    @pytest.mark.asyncio
    async def test_wire_realizes_commit_to_decision(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [edge-rea]: wire_realizes creates exactly one REALIZES edge
        # from Commit to Decision.
        # RED: AttributeError.
        commit_hash = f"TEST1D-COMM-REA-{uuid.uuid4().hex[:8]}"
        decision_id = f"TEST1D-DEC-REA-{uuid.uuid4().hex[:8]}"

        await db_clean.create_commit(
            commit_hash=commit_hash, project=_TEST_SCOPE,
            subject="test realizes", author="tester",
            branch="main", ts="2026-06-26T12:00:00+00:00",
        )
        await db_clean.create_decision(
            decision_id=decision_id, project=_TEST_SCOPE,
            title="Test dec", rationale="r",
            planned_files=[], governing_rule_ids=[], phase="planning",
            session_id=f"{_TEST_SCOPE}-rea", ts="2026-06-26T12:00:00+00:00",
        )

        await db_clean.wire_realizes(commit_hash, decision_id, _TEST_SCOPE)

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (c:Commit {commit_hash: $ch})-[e:REALIZES]->(d:Decision {decision_id: $did}) "
                "RETURN count(e) AS cnt",
                ch=commit_hash, did=decision_id,
            )
            record = await result.single()
        assert record["cnt"] == 1, (
            f"wire_realizes must create exactly one REALIZES edge; got cnt={record['cnt']}"
        )

    @pytest.mark.asyncio
    async def test_wire_has_change_and_wire_has_commit(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [edge-hc]: wire_has_change and wire_has_commit wire Project->FileChange
        # (HAS_CHANGE) and Project->Commit (HAS_COMMIT) via create_record_edge.
        # RED: AttributeError on both methods.
        project_name = f"{_TEST_SCOPE}-hc"
        commit_hash = f"TEST1D-COMM-HC-{uuid.uuid4().hex[:8]}"
        change_id = f"TEST1D-FC-HC-{uuid.uuid4().hex[:8]}"

        await db_clean.create_project(
            project_name, _TEST_REPO_ROOT + "/hc", _TEST_BIBLE_ROOT, None
        )
        await db_clean.create_commit(
            commit_hash=commit_hash, project=_TEST_SCOPE,
            subject="test hc", author="tester",
            branch="main", ts="2026-06-26T12:00:00+00:00",
        )
        await db_clean.create_filechange(
            change_id=change_id, project=_TEST_SCOPE,
            path="some/file.py", change_type="add",
            commit_hash=commit_hash, reason="", decision_id=None,
        )

        await db_clean.wire_has_change(project_name, change_id, _TEST_SCOPE)
        await db_clean.wire_has_commit(project_name, commit_hash, _TEST_SCOPE)

        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (p:Project {name: $name})-[:HAS_CHANGE]->(fc:FileChange {change_id: $cid}) "
                "RETURN count(fc) AS cnt_fc",
                name=project_name, cid=change_id,
            )
            fc_rec = await res.single()

            res2 = await s.run(
                "MATCH (p:Project {name: $name})-[:HAS_COMMIT]->(c:Commit {commit_hash: $ch}) "
                "RETURN count(c) AS cnt_c",
                name=project_name, ch=commit_hash,
            )
            c_rec = await res2.single()

        assert fc_rec["cnt_fc"] == 1, (
            f"wire_has_change must create one HAS_CHANGE edge; got {fc_rec['cnt_fc']}"
        )
        assert c_rec["cnt_c"] == 1, (
            f"wire_has_commit must create one HAS_COMMIT edge; got {c_rec['cnt_c']}"
        )


# ---------------------------------------------------------------------------
# Neo4j-gated: resolve_reasons_for_files
# (capability: [resolve-1])
# ---------------------------------------------------------------------------

class TestResolveReasonsForFiles:
    """Cap [resolve-1] -- requires Neo4j."""

    @pytest.mark.asyncio
    async def test_attaches_most_recent_open_decision_per_file(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [resolve-1]: resolve_reasons_for_files returns per-file dicts with reason,
        # decision_id, and governing_rule_ids from the most-recent open Decision.
        # RED: writ/session/commit_capture.py does not exist yet (ImportError).
        from writ.session.commit_capture import resolve_reasons_for_files

        path = "writ/session/commit_body.py"
        decision_id = f"TEST1D-RSN-{uuid.uuid4().hex[:6]}"
        rule_id = f"TEST1D-RSN-RULE-{uuid.uuid4().hex[:6]}"

        await db_clean.create_decision(
            decision_id=decision_id,
            project=_TEST_SCOPE,
            title="Reason test",
            rationale="rationale",
            planned_files=[{"path": path, "reason": "add commit body module", "resolved": False}],
            governing_rule_ids=[rule_id],
            phase="planning",
            session_id=f"{_TEST_SCOPE}-rsn",
            ts="2026-06-26T13:00:00+00:00",
        )

        files = [{"path": path, "change_type": "add"}]
        result = await resolve_reasons_for_files(db_clean, _TEST_SCOPE, files)

        assert len(result) == 1
        entry = result[0]
        assert entry["path"] == path
        assert entry["reason"] == "add commit body module", (
            f"reason must come from the most-recent open Decision; got {entry.get('reason')!r}"
        )
        assert entry["decision_id"] == decision_id, (
            f"decision_id must be attached; got {entry.get('decision_id')!r}"
        )

    @pytest.mark.asyncio
    async def test_blank_reason_when_no_decision_matches(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [resolve-1]: a file with no matching open Decision gets blank reason and
        # no decision_id (never invented).
        # RED: ImportError.
        from writ.session.commit_capture import resolve_reasons_for_files

        files = [{"path": "totally/unplanned/hotfix.py", "change_type": "modify"}]
        result = await resolve_reasons_for_files(db_clean, _TEST_SCOPE, files)

        assert len(result) == 1
        entry = result[0]
        assert entry.get("reason", "") == "" or entry.get("reason") is None, (
            "unmatched file must have blank/None reason, never invented; "
            f"got {entry.get('reason')!r}"
        )
        assert entry.get("decision_id") is None, (
            f"unmatched file must have no decision_id; got {entry.get('decision_id')!r}"
        )


# ---------------------------------------------------------------------------
# Neo4j-gated: capture_commit
# (capability: [cap-1], [cap-2], [cap-3], [cap-4], [cap-5], [cap-6])
# ---------------------------------------------------------------------------

class TestCaptureCommit:
    """Caps [cap-1] through [cap-6] -- requires Neo4j."""

    @pytest.mark.asyncio
    async def test_registers_project_before_write(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [cap-1]: capture_commit calls ensure_project_registered before any record write.
        # A cwd in no git repo returns None, capturing nothing (no "writ" fallback).
        # RED: ImportError.
        from writ.session.commit_capture import capture_commit

        # inject a no-repo runner so ensure_project_registered returns None.
        result = await capture_commit(
            db_clean,
            cwd=_TEST_REPO_ROOT + "/noreg",
            commit_hash="TEST1D-NOREPO-ABC",
            subject="no-repo commit",
            author="tester",
            branch="main",
            files=[{"path": "some/file.py", "change_type": "add"}],
            runner=_runner_no_repo(),
        )
        # capture_commit must return None (nothing to capture) when no git repo.
        assert result is None, (
            "capture_commit must return None when cwd is in no git repo"
        )

        # No :Commit node with this hash must exist.
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (c:Commit {commit_hash: $ch}) RETURN count(c) AS cnt",
                ch="TEST1D-NOREPO-ABC",
            )
            record = await res.single()
        assert record["cnt"] == 0, (
            "capture_commit must not write any Commit node when cwd is in no git repo"
        )

    @pytest.mark.asyncio
    async def test_creates_commit_and_filechange_nodes(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [cap-2]: capture_commit creates a :Commit node and one :FileChange per file.
        # RED: ImportError.
        from writ.session.commit_capture import capture_commit

        repo_root = _TEST_REPO_ROOT + "/cap2"
        remote_url = "git@github.com:org1d/cap2.git"
        runner = _runner_with_remote(repo_root, remote_url)
        commit_hash = f"TEST1D-CAP2-{uuid.uuid4().hex[:10]}"

        await capture_commit(
            db_clean,
            cwd=repo_root,
            commit_hash=commit_hash,
            subject="test cap2",
            author="tester <t@t.com>",
            branch="main",
            files=[
                {"path": "writ/session/name_status.py", "change_type": "add"},
                {"path": "writ/session/commit_body.py", "change_type": "add"},
            ],
            runner=runner,
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            res_c = await s.run(
                "MATCH (c:Commit {commit_hash: $ch}) RETURN count(c) AS cnt",
                ch=commit_hash,
            )
            c_rec = await res_c.single()
            res_fc = await s.run(
                "MATCH (fc:FileChange {commit_hash: $ch}) RETURN count(fc) AS cnt",
                ch=commit_hash,
            )
            fc_rec = await res_fc.single()

        assert c_rec["cnt"] == 1, (
            f"capture_commit must create exactly one :Commit node; got {c_rec['cnt']}"
        )
        assert fc_rec["cnt"] == 2, (
            f"capture_commit must create one :FileChange per file (2 expected); got {fc_rec['cnt']}"
        )

    @pytest.mark.asyncio
    async def test_deterministic_change_id_remerges_same_filechange(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [cap-3]: change_id = sha1(project\x00commit_hash\x00path)[:16].
        # A re-run on the SAME commit_hash re-MERGEs the SAME :FileChange, adding ZERO nodes.
        # RED: ImportError.
        from writ.session.commit_capture import capture_commit

        repo_root = _TEST_REPO_ROOT + "/cap3"
        remote_url = "git@github.com:org1d/cap3.git"
        runner = _runner_with_remote(repo_root, remote_url)
        commit_hash = f"TEST1D-CAP3-{uuid.uuid4().hex[:10]}"
        files = [{"path": "writ/graph/db.py", "change_type": "modify"}]

        await capture_commit(
            db_clean, cwd=repo_root, commit_hash=commit_hash,
            subject="first run", author="tester", branch="main",
            files=files, runner=runner,
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (fc:FileChange {commit_hash: $ch}) RETURN count(fc) AS cnt",
                ch=commit_hash,
            )
            count_after_first = (await res.single())["cnt"]

        # Second run -- same commit_hash, must re-MERGE (not duplicate).
        await capture_commit(
            db_clean, cwd=repo_root, commit_hash=commit_hash,
            subject="re-run", author="tester", branch="main",
            files=files, runner=runner,
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            res2 = await s.run(
                "MATCH (fc:FileChange {commit_hash: $ch}) RETURN count(fc) AS cnt",
                ch=commit_hash,
            )
            count_after_second = (await res2.single())["cnt"]

        assert count_after_first == 1, (
            f"first capture must create 1 FileChange; got {count_after_first}"
        )
        assert count_after_second == 1, (
            f"re-run must not create a duplicate FileChange; got {count_after_second} "
            "(deterministic change_id must re-MERGE)"
        )

    @pytest.mark.asyncio
    async def test_wires_includes_motivated_by_realizes_via_cypher(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [cap-4]: capture_commit wires INCLUDES, MOTIVATED_BY, and REALIZES.
        # Verified via direct Cypher assertions.
        # RED: ImportError.
        from writ.session.commit_capture import capture_commit

        repo_root = _TEST_REPO_ROOT + "/cap4"
        remote_url = "git@github.com:org1d/cap4.git"
        runner = _runner_with_remote(repo_root, remote_url)
        commit_hash = f"TEST1D-CAP4-{uuid.uuid4().hex[:10]}"
        path = "writ/session/commit_capture.py"

        # Pre-seed a Decision with an OPEN claim on the path.
        # The project name that capture_commit derives via runner is the REUSE name.
        # We need to register first so the Decision is under the same name.
        from writ.session.registration import ensure_project_registered
        project_name = await ensure_project_registered(
            db_clean, repo_root, runner=runner
        )
        assert project_name is not None

        decision_id = f"TEST1D-CAP4-DEC-{uuid.uuid4().hex[:6]}"
        await db_clean.create_decision(
            decision_id=decision_id, project=project_name,
            title="cap4 dec", rationale="r",
            planned_files=[{"path": path, "reason": "add module", "resolved": False}],
            governing_rule_ids=[], phase="planning",
            session_id=f"{_TEST_SCOPE}-cap4", ts="2026-06-26T14:00:00+00:00",
        )

        await capture_commit(
            db_clean, cwd=repo_root, commit_hash=commit_hash,
            subject="cap4 commit", author="tester", branch="main",
            files=[{"path": path, "change_type": "add"}],
            runner=runner,
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            # INCLUDES: Commit -> FileChange.
            res_inc = await s.run(
                "MATCH (c:Commit {commit_hash: $ch})-[:INCLUDES]->(fc:FileChange) "
                "RETURN count(fc) AS cnt",
                ch=commit_hash,
            )
            inc_cnt = (await res_inc.single())["cnt"]

            # MOTIVATED_BY: FileChange -> Decision.
            res_mot = await s.run(
                "MATCH (fc:FileChange {commit_hash: $ch})-[:MOTIVATED_BY]->(d:Decision) "
                "RETURN count(d) AS cnt",
                ch=commit_hash,
            )
            mot_cnt = (await res_mot.single())["cnt"]

            # REALIZES: Commit -> Decision.
            res_rea = await s.run(
                "MATCH (c:Commit {commit_hash: $ch})-[:REALIZES]->(d:Decision) "
                "RETURN count(d) AS cnt",
                ch=commit_hash,
            )
            rea_cnt = (await res_rea.single())["cnt"]

        assert inc_cnt >= 1, f"capture_commit must wire INCLUDES; got {inc_cnt}"
        assert mot_cnt >= 1, (
            f"capture_commit must wire MOTIVATED_BY when a Decision matched; got {mot_cnt}"
        )
        assert rea_cnt >= 1, (
            f"capture_commit must wire REALIZES when a Decision matched; got {rea_cnt}"
        )

    @pytest.mark.asyncio
    async def test_resolves_every_planning_decision_claim(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [cap-5]: capture_commit calls resolve_file_claims for each committed path,
        # resolving claims on EVERY planning Decision for that path.
        # RED: ImportError.
        from writ.session.commit_capture import capture_commit

        repo_root = _TEST_REPO_ROOT + "/cap5"
        remote_url = "git@github.com:org1d/cap5.git"
        runner = _runner_with_remote(repo_root, remote_url)
        commit_hash = f"TEST1D-CAP5-{uuid.uuid4().hex[:10]}"
        path = "writ/session/git_hooks.py"

        from writ.session.registration import ensure_project_registered
        project_name = await ensure_project_registered(
            db_clean, repo_root, runner=runner
        )
        assert project_name is not None

        # Two open Decisions for the same path (both must be resolved).
        did_a = f"TEST1D-CAP5-A-{uuid.uuid4().hex[:6]}"
        did_b = f"TEST1D-CAP5-B-{uuid.uuid4().hex[:6]}"
        for did in (did_a, did_b):
            await db_clean.create_decision(
                decision_id=did, project=project_name,
                title="cap5 dec", rationale="r",
                planned_files=[{"path": path, "reason": "add installer", "resolved": False}],
                governing_rule_ids=[], phase="planning",
                session_id=f"{_TEST_SCOPE}-cap5-{did[:8]}", ts="2026-06-26T15:00:00+00:00",
            )

        await capture_commit(
            db_clean, cwd=repo_root, commit_hash=commit_hash,
            subject="cap5 commit", author="tester", branch="main",
            files=[{"path": path, "change_type": "add"}],
            runner=runner,
        )

        open_after = await db_clean.get_open_decisions_for_path(project_name, path)
        assert open_after == [], (
            "capture_commit must resolve ALL open Decision claims for the committed path; "
            f"still open: {[r.get('decision_id') for r in open_after]}"
        )

    @pytest.mark.asyncio
    async def test_idempotent_rerun_adds_zero_nodes_and_is_noop_on_claims(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [cap-6]: a re-run of capture_commit (amend/post-rewrite replay) re-MERGEs
        # nodes/edges and a second resolve_file_claims call is a no-op (already resolved).
        # RED: ImportError.
        from writ.session.commit_capture import capture_commit

        repo_root = _TEST_REPO_ROOT + "/cap6"
        remote_url = "git@github.com:org1d/cap6.git"
        runner = _runner_with_remote(repo_root, remote_url)
        commit_hash = f"TEST1D-CAP6-{uuid.uuid4().hex[:10]}"
        path = "writ/cli.py"

        from writ.session.registration import ensure_project_registered
        project_name = await ensure_project_registered(
            db_clean, repo_root, runner=runner
        )
        assert project_name is not None

        did = f"TEST1D-CAP6-DEC-{uuid.uuid4().hex[:6]}"
        await db_clean.create_decision(
            decision_id=did, project=project_name,
            title="cap6 dec", rationale="r",
            planned_files=[{"path": path, "reason": "add git-hooks cmd", "resolved": False}],
            governing_rule_ids=[], phase="planning",
            session_id=f"{_TEST_SCOPE}-cap6", ts="2026-06-26T16:00:00+00:00",
        )

        files = [{"path": path, "change_type": "modify"}]

        # First run.
        await capture_commit(
            db_clean, cwd=repo_root, commit_hash=commit_hash,
            subject="cap6 run1", author="tester", branch="main",
            files=files, runner=runner,
        )
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (fc:FileChange {commit_hash: $ch}) RETURN count(fc) AS cnt",
                ch=commit_hash,
            )
            count_after_first = (await res.single())["cnt"]

        # Second run (idempotent re-run).
        await capture_commit(
            db_clean, cwd=repo_root, commit_hash=commit_hash,
            subject="cap6 run2 (amend replay)", author="tester", branch="main",
            files=files, runner=runner,
        )
        async with db_clean._driver.session(database=db_clean._database) as s:
            res2 = await s.run(
                "MATCH (fc:FileChange {commit_hash: $ch}) RETURN count(fc) AS cnt",
                ch=commit_hash,
            )
            count_after_second = (await res2.single())["cnt"]

        assert count_after_first == count_after_second, (
            f"idempotent re-run must not add new FileChange nodes; "
            f"first={count_after_first}, second={count_after_second}"
        )

        # Claims must still be resolved (not un-resolved by second run).
        open_after = await db_clean.get_open_decisions_for_path(project_name, path)
        assert open_after == [], (
            "idempotent re-run must not re-open already-resolved claims; "
            f"still open: {[r.get('decision_id') for r in open_after]}"
        )


# ---------------------------------------------------------------------------
# Hook shell scripts: prepare-commit-msg and post-commit
# (capability: [hook-pcm-1], [hook-pcm-2], [hook-pcm-3], [hook-pc-1])
#
# Auto-install guard note: the auto-install guards (work-mode-only, not-in-repo,
# fail-open) are tested at the ROUTE level below ([auto-3], [auto-4]) because no
# existing subprocess-hook test pattern for writ-cwd-changed's auto-install seam
# exists in the suite (grep tests/ found no writ-cwd-changed subprocess test for
# the auto-install curl). The bash seam is thin and curls the route; the behaviours
# are asserted at the route boundary as documented in plan.md.
# ---------------------------------------------------------------------------

class TestGitHookScripts:
    """Caps [hook-pcm-1], [hook-pcm-2], [hook-pcm-3], [hook-pc-1].

    Tests run prepare-commit-msg and post-commit via subprocess against a tmp git
    repo with an unreachable daemon port (19999) to trigger fail-open paths.
    RED until the hook files are written to hooks/git/.
    """

    _SKILL_DIR = str(Path(__file__).resolve().parent.parent)
    _POST_HOOK = str(Path(__file__).resolve().parent.parent / "hooks/git/post-commit")

    def _hook_env(self, repo: Path) -> dict:
        """Env for hook subprocess: BOTH transports dead, real git repo.

        THE PORT ALONE IS NOT ISOLATION, and for a long time this helper set only
        the port. The hook resolves its transport at hooks/git/post-commit:104-106:
        if `$WRIT_SOCKET` (default `$HOME/.cache/writ/run/writ.sock`) exists it
        passes `--unix-socket`, and the hook's own comment at :100 records that curl
        then IGNORES the url. So "unreachable daemon" reached the real daemon over
        the socket and wrote to the PRODUCTION graph: 182 of 275 Commit records
        (66%) carried a `/tmp/pytest-.../repo-postcmt` project when measured on
        2026-09-21. WRIT_SOCKET now points at a path that is not a socket, so the
        hook's `[ -S ]` test fails and curl has nothing to prefer.

        Pinned by tests/test_post_commit_isolation.py, which counts records at the
        DESTINATION rather than asserting on this dict: asserting the dict would
        pass while the hook resolved a third way, which is how this survived.
        """
        env = os.environ.copy()
        env["WRIT_PORT"] = "19999"  # unreachable
        env["WRIT_SOCKET"] = str(repo / "definitely-not-a-socket")
        env["GIT_DIR"] = str(repo / ".git")
        env["WRIT_DIR"] = self._SKILL_DIR
        return env

    def test_post_commit_hook_file_exists(self) -> None:
        # Sentinel: the post-commit hook file must exist on disk.
        # RED until hooks/git/post-commit is created.
        assert os.path.exists(self._POST_HOOK), (
            f"hooks/git/post-commit not found at {self._POST_HOOK}"
        )

    def test_post_commit_fails_open_when_daemon_down(self, tmp_path: Path) -> None:
        # [hook-pc-1]: with daemon unreachable, post-commit must exit 0 and create
        # zero Commit/FileChange nodes (no graph write on daemon-down).
        # This test verifies the exit-0 contract; the "zero nodes" claim is separately
        # verified by the route-level tests (route-cap-2 with _db=None).
        # RED: hook file does not exist yet.
        repo = _init_tmp_repo(tmp_path / "repo-postcmt")

        # Create a real commit so HEAD exists.
        (repo / "file.txt").write_text("hello\n")
        subprocess.run(
            ["git", "-C", str(repo), "add", "file.txt"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init",
             "--author", "tester <t@test.com>"],
            check=True, capture_output=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "tester",
                 "GIT_AUTHOR_EMAIL": "t@test.com",
                 "GIT_COMMITTER_NAME": "tester",
                 "GIT_COMMITTER_EMAIL": "t@test.com"},
        )

        result = subprocess.run(
            ["bash", self._POST_HOOK],
            cwd=str(repo),
            capture_output=True, text=True,
            env=self._hook_env(repo),
            timeout=10,
        )
        assert result.returncode == 0, (
            f"post-commit must exit 0 when daemon is unreachable; "
            f"returncode={result.returncode}, stderr={result.stderr}"
        )


# ---------------------------------------------------------------------------
# Route-level: POST /commit/capture
# (capability: [route-cap-1], [route-cap-2], [route-cap-3])
# ---------------------------------------------------------------------------

class TestCommitCaptureRoute:
    """Caps [route-cap-1], [route-cap-2], [route-cap-3]."""

    def test_guards_db_is_none(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [route-cap-2]: POST /commit/capture with _db=None returns error shape.
        # RED: route does not exist yet (404).
        import writ.server as _srv
        monkeypatch.setattr(_srv, "_db", None)

        resp = client.post("/commit/capture", json={
            "project_root": _TEST_REPO_ROOT + "/cap-null",
            "commit_hash": "TEST1D-ROUTE-NULL",
            "subject": "test",
            "author": "tester",
            "branch": "main",
            "files": [],
        })
        assert resp.status_code == 200, (
            f"_db is None guard must return 200; got {resp.status_code}"
        )
        data = resp.json()
        assert "error" in data, (
            f"_db is None must return error key; got {list(data.keys())}"
        )

    def test_logs_and_returns_on_capture_exception(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [route-cap-3]: when capture_commit raises, the route logs
        # 'commit_capture_failed' and returns (does not propagate the exception,
        # never blocking a commit). Mirrors server.py:950-962 decision-capture pattern.
        # RED: route does not exist yet (404).
        import writ.server as _srv

        class _ErrorDB:
            async def get_open_decisions_for_path(self, project, path):
                raise RuntimeError("simulated DB failure")
            async def get_projects(self):
                return []
            async def create_project(self, *args, **kwargs):
                return "err-project"

        monkeypatch.setattr(_srv, "_db", _ErrorDB())

        # The route must still return 200 (fail-open).
        resp = client.post("/commit/capture", json={
            "project_root": _TEST_REPO_ROOT + "/cap-err",
            "commit_hash": "TEST1D-ROUTE-ERR",
            "subject": "test",
            "author": "tester",
            "branch": "main",
            "files": [{"path": "file.py", "change_type": "add"}],
        })
        assert resp.status_code == 200, (
            f"POST /commit/capture must return 200 even on internal exception (fail-open); "
            f"got {resp.status_code}: {resp.text}"
        )

    def test_creates_records_and_edges_with_real_db(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [route-cap-1]: POST /commit/capture creates :Commit + :FileChange nodes and
        # INCLUDES edge when _db is a real Neo4j connection (skipped if unreachable).
        # RED: route does not exist yet (404).
        #
        # This test is Neo4j-gated via a synchronous skip pattern (no async fixture).
        # It uses a real connection; the finally block below wipes the test-dm-1d
        # scope (and the /tmp/fake-test-1d-repo repo_root prefix) and closes the
        # connection, so no test-seeded nodes leak into the live graph.
        import asyncio
        conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
        # An explicit current loop -- a preceding async test can leave the thread's
        # event loop closed/unset, so a bare get_event_loop() raises on 3.12. The
        # test body below reuses this same loop via get_event_loop().
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _probe() -> None:
            async with conn._driver.session(database=conn._database) as s:
                await (await s.run("RETURN 1 AS ok")).consume()

        try:
            loop.run_until_complete(_probe())
        except Exception:
            loop.run_until_complete(conn.close())
            pytest.skip("Neo4j unreachable")

        import writ.server as _srv
        monkeypatch.setattr(_srv, "_db", conn)

        try:
            commit_hash = f"TEST1D-RROUTE-{uuid.uuid4().hex[:10]}"
            resp = client.post("/commit/capture", json={
                "project_root": _TEST_REPO_ROOT + "/cap-route",
                "commit_hash": commit_hash,
                "subject": "route test",
                "author": "tester <t@t.com>",
                "branch": "main",
                "files": [{"path": "writ/server.py", "change_type": "modify"}],
            })
            assert resp.status_code == 200, (
                f"POST /commit/capture must return 200; got {resp.status_code}: {resp.text}"
            )
            data = resp.json()
            # Route must return {ok: true} or at least not an error key for a non-failure path.
            # (Exact shape depends on whether the project auto-registered; fail-open means
            # the route may return ok=true even if the DB write degraded gracefully.)
            assert "error" not in data or data.get("ok") is True, (
                f"route must return ok=true or no error for a valid capture request; got {data}"
            )
        finally:
            # Reuse the explicit loop created above rather than a bare
            # get_event_loop() (which can raise on 3.12 after a prior test).
            loop.run_until_complete(_wipe_1d_test_data(conn))
            loop.run_until_complete(conn.close())


# ---------------------------------------------------------------------------
# Installer: install_git_hooks, uninstall_git_hooks, git_hooks_installed
# (capability: [inst-1], [inst-2], [inst-3], [inst-4], [inst-5])
# ---------------------------------------------------------------------------

class TestGitHooksInstaller:
    """Caps [inst-1], [inst-2], [inst-3], [inst-4], [inst-5].

    All filesystem work uses tmp_path git repos; NEVER the real .git/hooks.
    RED until writ/session/git_hooks.py is created.
    """

    def test_writes_post_commit_only_with_marker_and_executable(self, tmp_path: Path) -> None:
        # [inst-1]: install_git_hooks writes post-commit (only) with the "# >>> Writ"
        # marker block, chmod 0o755, and does NOT write prepare-commit-msg (retired:
        # commit messages stay normal).
        from writ.session.git_hooks import install_git_hooks

        repo = _init_tmp_repo(tmp_path / "repo-inst1")
        install_git_hooks(str(repo))

        hooks_d = _hooks_dir(repo)
        post = hooks_d / "post-commit"
        assert post.exists(), "install_git_hooks must write post-commit"
        content = post.read_text()
        assert _WRIT_MARKER in content, (
            f"post-commit must contain the '# >>> Writ' marker; content:\n{content[:200]}"
        )
        assert post.stat().st_mode & stat.S_IXUSR, "post-commit must be executable (chmod 0o755)"
        assert not (hooks_d / "prepare-commit-msg").exists(), (
            "prepare-commit-msg is retired; install must NOT write it"
        )

    def test_install_strips_legacy_prepare_commit_msg_block(self, tmp_path: Path) -> None:
        # [inst-1b]: a repo with an already-installed prepare-commit-msg Writ block has
        # that block stripped on the next install (migration); a file left bare is deleted.
        from writ.session.git_hooks import install_git_hooks

        repo = _init_tmp_repo(tmp_path / "repo-inst1b")
        hooks_d = _hooks_dir(repo)
        hooks_d.mkdir(parents=True, exist_ok=True)
        (hooks_d / "prepare-commit-msg").write_text(
            "#!/bin/sh\n# >>> Writ prepare-commit-msg hook >>>\necho legacy\n"
            "# <<< Writ prepare-commit-msg hook <<<\n"
        )

        install_git_hooks(str(repo))

        pcm = hooks_d / "prepare-commit-msg"
        if pcm.exists():
            assert _WRIT_MARKER not in pcm.read_text(), (
                "install must strip the retired prepare-commit-msg Writ block"
            )

    def test_idempotent_second_install_does_not_grow_file(self, tmp_path: Path) -> None:
        # [inst-2]: a second install_git_hooks call must not append a second block.
        # The marker must appear exactly once per hook file.
        # RED: ImportError.
        from writ.session.git_hooks import install_git_hooks

        repo = _init_tmp_repo(tmp_path / "repo-inst2")
        install_git_hooks(str(repo))
        install_git_hooks(str(repo))  # second call

        hooks_d = _hooks_dir(repo)
        content = (hooks_d / "post-commit").read_text()
        marker_count = content.count(_WRIT_MARKER)
        assert marker_count == 1, (
            f"idempotent install: marker must appear exactly once in post-commit; "
            f"found {marker_count} occurrences"
        )
        assert not (hooks_d / "prepare-commit-msg").exists(), (
            "prepare-commit-msg is retired; install must not create it"
        )

    def test_coexists_with_preexisting_hook_content(self, tmp_path: Path) -> None:
        # [inst-3]: install_git_hooks appends the Writ block without clobbering
        # pre-existing hook content.
        # RED: ImportError.
        from writ.session.git_hooks import install_git_hooks

        repo = _init_tmp_repo(tmp_path / "repo-inst3")
        hooks_d = _hooks_dir(repo)
        hooks_d.mkdir(parents=True, exist_ok=True)

        pre_content = "#!/bin/sh\n# My custom hook\nexit 0\n"
        for name in ("prepare-commit-msg", "post-commit"):
            (hooks_d / name).write_text(pre_content)

        install_git_hooks(str(repo))

        # post-commit: custom content preserved + Writ marker appended.
        post_content = (hooks_d / "post-commit").read_text()
        assert "My custom hook" in post_content, (
            f"install must preserve pre-existing post-commit content; got:\n{post_content[:300]}"
        )
        assert _WRIT_MARKER in post_content, "install must append the Writ marker to post-commit"
        # prepare-commit-msg: retired -- custom content preserved, NO Writ marker added.
        pcm_content = (hooks_d / "prepare-commit-msg").read_text()
        assert "My custom hook" in pcm_content, (
            "install must preserve pre-existing prepare-commit-msg content"
        )
        assert _WRIT_MARKER not in pcm_content, (
            "install must NOT add the retired prepare-commit-msg marker"
        )

    def test_git_hooks_installed_true_after_install_false_before(self, tmp_path: Path) -> None:
        # [inst-4]: git_hooks_installed returns False before install and True after.
        # RED: ImportError.
        from writ.session.git_hooks import install_git_hooks, git_hooks_installed

        repo = _init_tmp_repo(tmp_path / "repo-inst4")
        assert not git_hooks_installed(str(repo)), (
            "git_hooks_installed must return False before install"
        )
        install_git_hooks(str(repo))
        assert git_hooks_installed(str(repo)), (
            "git_hooks_installed must return True after install"
        )

    def test_uninstall_strips_only_writ_block_preserves_custom_content(
        self, tmp_path: Path
    ) -> None:
        # [inst-5]: uninstall_git_hooks strips only the Writ marker block.
        # Pre-existing custom content is intact after uninstall.
        # RED: ImportError.
        from writ.session.git_hooks import (
            install_git_hooks, uninstall_git_hooks, git_hooks_installed
        )

        repo = _init_tmp_repo(tmp_path / "repo-inst5")
        hooks_d = _hooks_dir(repo)
        hooks_d.mkdir(parents=True, exist_ok=True)

        pre_content = "#!/bin/sh\n# Custom business logic\ndo_something\n"
        for name in ("prepare-commit-msg", "post-commit"):
            (hooks_d / name).write_text(pre_content)

        install_git_hooks(str(repo))
        assert git_hooks_installed(str(repo)), "hook must be installed before uninstall test"

        uninstall_git_hooks(str(repo))

        assert not git_hooks_installed(str(repo)), (
            "git_hooks_installed must return False after uninstall"
        )
        for name in ("prepare-commit-msg", "post-commit"):
            hook_path = hooks_d / name
            if hook_path.exists():
                content = hook_path.read_text()
                assert "Custom business logic" in content, (
                    f"uninstall must preserve custom content in {name}; content:\n{content}"
                )
                assert _WRIT_MARKER not in content, (
                    f"uninstall must remove the Writ marker from {name}"
                )

    def test_uninstall_deletes_file_when_only_shebang_remains(self, tmp_path: Path) -> None:
        # [inst-5]: when the hook file contains ONLY the shebang (created by Writ),
        # uninstall_git_hooks deletes the file rather than leaving an empty stub.
        # RED: ImportError.
        from writ.session.git_hooks import install_git_hooks, uninstall_git_hooks

        repo = _init_tmp_repo(tmp_path / "repo-inst5b")
        # No pre-existing hooks; Writ creates them from scratch.
        install_git_hooks(str(repo))

        hooks_d = _hooks_dir(repo)
        uninstall_git_hooks(str(repo))

        for name in ("prepare-commit-msg", "post-commit"):
            hook_path = hooks_d / name
            if hook_path.exists():
                content = hook_path.read_text().strip()
                # Either deleted entirely OR contains non-shebang content we preserved.
                assert content in ("", "#!/bin/sh") or "Custom" in content, (
                    f"uninstall must delete {name} when only shebang remains; "
                    f"got content: {content!r}"
                )


# ---------------------------------------------------------------------------
# Route-level: POST /git-hooks/auto-install
# (capability: [auto-1], [auto-2], [auto-3], [auto-4])
#
# Auto-install guard note: fail-open / work-mode-only / not-in-repo guards are
# tested at the route level, NOT via writ-cwd-changed.sh subprocess, because no
# existing subprocess-hook test pattern for the auto-install curl seam exists in
# the suite (plan.md: "if NO such subprocess-hook test pattern exists, assert
# these guards at the route + installer level instead").
# ---------------------------------------------------------------------------

class TestAutoInstallRoute:
    """Caps [auto-1], [auto-2], [auto-3], [auto-4]."""

    def test_installs_when_marker_absent(
        self, client: TestClient, isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # [auto-1]: POST /git-hooks/auto-install installs when the marker is absent,
        # returns {installed: true, already: false}.
        # RED: route does not exist yet (404).
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _make_fake_route_db())

        # Monkeypatch git_hooks functions to avoid real filesystem mutation.
        import writ.session.git_hooks as _ghmod
        monkeypatch.setattr(_ghmod, "git_hooks_installed", lambda cwd: False)
        install_calls = []
        monkeypatch.setattr(_ghmod, "install_git_hooks", lambda cwd: install_calls.append(cwd))

        repo = _init_tmp_repo(tmp_path / "repo-auto1")
        resp = client.post("/git-hooks/auto-install", json={"project_root": str(repo)})
        assert resp.status_code == 200, (
            f"POST /git-hooks/auto-install must return 200; got {resp.status_code}: {resp.text}"
        )
        data = resp.json()
        assert data.get("installed") is True, (
            f"must return installed=true when marker was absent; got {data}"
        )
        assert data.get("already") is False, (
            f"must return already=false when marker was absent; got {data}"
        )
        assert len(install_calls) == 1, (
            f"install_git_hooks must be called once; called {len(install_calls)} times"
        )

    def test_noop_when_marker_already_present(
        self, client: TestClient, isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # [auto-2]: POST /git-hooks/auto-install returns {installed: false, already: true}
        # when the marker is already present (idempotent -- no reinstall).
        # RED: route does not exist yet (404).
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _make_fake_route_db())

        import writ.session.git_hooks as _ghmod
        monkeypatch.setattr(_ghmod, "git_hooks_installed", lambda cwd: True)
        install_calls = []
        monkeypatch.setattr(_ghmod, "install_git_hooks", lambda cwd: install_calls.append(cwd))

        repo = _init_tmp_repo(tmp_path / "repo-auto2")
        resp = client.post("/git-hooks/auto-install", json={"project_root": str(repo)})
        assert resp.status_code == 200, (
            f"must return 200; got {resp.status_code}"
        )
        data = resp.json()
        assert data.get("already") is True, (
            f"must return already=true when marker was present; got {data}"
        )
        assert data.get("installed") is False, (
            f"must return installed=false (no re-install); got {data}"
        )
        assert len(install_calls) == 0, (
            "install_git_hooks must NOT be called when marker is already present"
        )

    def test_guards_db_is_none(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # [auto-3]: POST /git-hooks/auto-install with _db=None returns gracefully
        # (error shape, no raise). Daemon-down is fail-open.
        # RED: route does not exist yet (404).
        import writ.server as _srv
        monkeypatch.setattr(_srv, "_db", None)

        repo = _init_tmp_repo(tmp_path / "repo-auto3")
        resp = client.post("/git-hooks/auto-install", json={"project_root": str(repo)})
        assert resp.status_code == 200, (
            f"_db is None guard must return 200; got {resp.status_code}"
        )
        data = resp.json()
        assert "error" in data, (
            f"_db is None must return error key; got {list(data.keys())}"
        )

    def test_no_install_outside_git_repo(
        self, client: TestClient, isolated_cache: Path,
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        # [auto-4] not-in-repo guard: when project_root is not inside a git repo,
        # the route must be a no-op (no install). This tests the guard at route level.
        # RED: route does not exist yet (404).
        #
        # NOTE: work-mode-only guard is in the bash seam (writ-cwd-changed.sh checks
        # mode before curling the route); the route itself is mode-agnostic. The not-in-repo
        # guard is the route-level check (git_hooks_installed will fail or return False
        # gracefully for a non-repo dir).
        import writ.server as _srv

        monkeypatch.setattr(_srv, "_db", _make_fake_route_db())

        import writ.session.git_hooks as _ghmod
        install_calls = []
        original_installed = _ghmod.git_hooks_installed

        def _installed_for_non_repo(cwd):
            # For a non-repo dir, git rev-parse will fail; simulate that gracefully.
            try:
                return original_installed(cwd)
            except Exception:
                return False

        monkeypatch.setattr(_ghmod, "git_hooks_installed", _installed_for_non_repo)
        monkeypatch.setattr(_ghmod, "install_git_hooks", lambda cwd: install_calls.append(cwd))

        non_repo_dir = str(tmp_path / "not-a-repo")
        os.makedirs(non_repo_dir, exist_ok=True)

        resp = client.post("/git-hooks/auto-install", json={"project_root": non_repo_dir})
        # Route must return 200 (fail-open) and must NOT call install_git_hooks
        # for a non-repo dir (the marker check itself will return False / error).
        assert resp.status_code == 200, (
            f"auto-install route must return 200 even for a non-repo dir; "
            f"got {resp.status_code}: {resp.text}"
        )
        assert len(install_calls) == 0, (
            "auto-install must NOT call install_git_hooks for a non-repo directory"
        )


# ---------------------------------------------------------------------------
# CLI: writ git-hooks install / uninstall / bootstrap
# (capability: [cli-1], [cli-2])
# ---------------------------------------------------------------------------

class TestGitHooksCLI:
    """Caps [cli-1], [cli-2]."""

    def test_git_hooks_install_and_uninstall_cli_commands_are_registered(self) -> None:
        # [cli-1]: the 'git-hooks' sub-app with install/uninstall is registered in
        # the Typer app (or as a registered group). RED: cli.py not modified yet.
        from typer.testing import CliRunner as _CliRunner
        from writ.cli import app as _app

        runner = _CliRunner()
        result = runner.invoke(_app, ["git-hooks", "--help"])
        # Must not exit with "No such command" (exit_code=2).
        assert result.exit_code != 2 or "git-hooks" in result.output.lower(), (
            f"'writ git-hooks' must be a registered command; "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        # 'install' and 'uninstall' sub-commands must appear in the help.
        assert "install" in result.output or result.exit_code == 0, (
            f"'writ git-hooks --help' must list 'install'; output={result.output!r}"
        )

    def test_git_hooks_install_cli_runs_installer(self, tmp_path: Path) -> None:
        # [cli-1]: `writ git-hooks install` drives the installer against the given repo.
        # RED: cli.py not modified yet (no git-hooks command).
        from typer.testing import CliRunner as _CliRunner
        from writ.cli import app as _app

        repo = _init_tmp_repo(tmp_path / "repo-cli-inst")
        runner = _CliRunner()
        result = runner.invoke(_app, ["git-hooks", "install", "--repo", str(repo)])
        # exit_code must be 0 (command runs) -- content validated by installer tests.
        assert result.exit_code == 0, (
            f"'writ git-hooks install' must exit 0; "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        # The post-commit hook must exist after the CLI command; prepare-commit-msg
        # is retired (commit messages stay normal) and must NOT be created.
        hooks_d = _hooks_dir(repo)
        assert (hooks_d / "post-commit").exists(), (
            "'writ git-hooks install' must create post-commit in .git/hooks"
        )
        assert not (hooks_d / "prepare-commit-msg").exists(), (
            "'writ git-hooks install' must NOT create the retired prepare-commit-msg"
        )

    def test_git_hooks_uninstall_cli_removes_hooks(self, tmp_path: Path) -> None:
        # [cli-1]: `writ git-hooks uninstall` removes the Writ marker block.
        # RED: cli.py not modified yet.
        from writ.session.git_hooks import install_git_hooks, git_hooks_installed
        from typer.testing import CliRunner as _CliRunner
        from writ.cli import app as _app

        repo = _init_tmp_repo(tmp_path / "repo-cli-uninst")
        install_git_hooks(str(repo))
        assert git_hooks_installed(str(repo)), "must be installed before CLI uninstall"

        runner = _CliRunner()
        result = runner.invoke(_app, ["git-hooks", "uninstall", "--repo", str(repo)])
        assert result.exit_code == 0, (
            f"'writ git-hooks uninstall' must exit 0; "
            f"exit_code={result.exit_code}, output={result.output!r}"
        )
        assert not git_hooks_installed(str(repo)), (
            "after 'writ git-hooks uninstall', git_hooks_installed must return False"
        )

    @pytest.mark.asyncio
    async def test_git_hooks_bootstrap_registers_writ_project(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [cli-2]: `writ git-hooks bootstrap` registers a :Project named after the
        # writ repo, bound to its remote_url. The test uses the fake test repo_root
        # so it NEVER touches the live writ :Project registry.
        #
        # Approach: call the function that bootstrap invokes (create_project with
        # a known name + remote_url under the test scope), then verify the :Project
        # was registered with that remote_url. Teardown deletes exactly this node.
        #
        # RED: bootstrap command does not exist in cli.py yet; but also we assert the
        # underlying function behavior (create_project with remote_url stores it).
        from writ.session.registration import ensure_project_registered

        fake_repo = tmp_path / "fake-writ-repo"
        fake_remote = "git@github.com:org1d/writ-bootstrap-test.git"
        runner = _runner_with_remote(str(fake_repo), fake_remote)

        name = await ensure_project_registered(
            db_clean, str(fake_repo), runner=runner
        )
        assert name is not None, "bootstrap seam must register the project and return a name"
        assert name != "writ", (
            "bootstrap must not scope under the bare 'writ' fallback for a real repo "
            f"(got name={name!r}); see SCOPE-KEY COLLISION GUARD"
        )

        # Verify the :Project carries the remote_url.
        projects = await db_clean.get_projects()
        by_name = {p["name"]: p for p in projects}
        assert name in by_name, (
            f"registered :Project '{name}' must appear in get_projects(); "
            f"got {list(by_name.keys())}"
        )
        assert by_name[name]["remote_url"] == fake_remote, (
            f":Project '{name}' must carry remote_url={fake_remote!r}; "
            f"got {by_name[name].get('remote_url')!r}"
        )


# ===========================================================================
# NEW TESTS: per-file queried rules captured + shown beside cited rules
# Capabilities from plan.md / capabilities.md
# ===========================================================================

# ---------------------------------------------------------------------------
# Capability 1: --add-queried-rules-for-file handler in _UPDATE_HANDLERS
# Tag: [queried-handler-1]
# ---------------------------------------------------------------------------

class TestAddQueriedRulesForFileHandler:
    """Caps [queried-handler-1] -- pure Python, no Neo4j required.

    Tests the --add-queried-rules-for-file flag in budget_tracking._UPDATE_HANDLERS:
    arity is 2, handler unions rule_ids into cache['queried_rules_by_file'][normalized_path].
    Driven through cmd_update (the same entry point used by sibling tests in
    test_pol6g1_citations_budget_extraction.py:TestCmdUpdate).
    """

    def _load_facade(self):
        import importlib.util
        import importlib
        facade_path = str(Path(__file__).resolve().parent.parent / "bin/lib/writ-session.py")
        spec = importlib.util.spec_from_file_location("writ_session_qrf", facade_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _seed(self, tmp_path, sid, **fields):
        import sys
        skill_root = str(Path(__file__).resolve().parent.parent)
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
        import importlib
        cache = importlib.import_module("writ.session.cache")
        data = cache._read_cache(sid)
        data.update(fields)
        cache._write_cache(sid, data)

    def _read(self, tmp_path, sid):
        import sys
        skill_root = str(Path(__file__).resolve().parent.parent)
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
        import importlib
        cache = importlib.import_module("writ.session.cache")
        return cache._read_cache(sid)

    def test_handler_registered_with_arity_2(self) -> None:
        # [queried-handler-1]: --add-queried-rules-for-file must be registered in
        # _UPDATE_HANDLERS with arity 2 (consumes args[i+1]=path, args[i+2]=json_ids).
        # RED: key absent from _UPDATE_HANDLERS (KeyError / assertion failure).
        from writ.session.budget_tracking import _UPDATE_HANDLERS

        assert "--add-queried-rules-for-file" in _UPDATE_HANDLERS, (
            "--add-queried-rules-for-file must be registered in _UPDATE_HANDLERS"
        )
        handler_fn, arity = _UPDATE_HANDLERS["--add-queried-rules-for-file"]
        assert arity == 2, (
            f"--add-queried-rules-for-file must have arity 2 (path + json_ids); got {arity}"
        )

    def test_handler_empty_start_produces_sorted_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [queried-handler-1]: starting from an empty cache, one call produces
        # {normalized_path: sorted(ids)}.
        # RED: --add-queried-rules-for-file not yet in _UPDATE_HANDLERS (KeyError on lookup).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        f = self._load_facade()
        sid = f"qrf-start-{uuid.uuid4().hex[:8]}"
        self._seed(tmp_path, sid)
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "writ/session/cache.py",
            json.dumps(["ENF-B", "ENF-A"]),
        ])
        data = self._read(tmp_path, sid)

        assert "queried_rules_by_file" in data, (
            "cache must contain queried_rules_by_file after --add-queried-rules-for-file"
        )
        by_file = data["queried_rules_by_file"]
        assert "writ/session/cache.py" in by_file, (
            f"normalized path must be a key; keys={list(by_file.keys())}"
        )
        assert by_file["writ/session/cache.py"] == ["ENF-A", "ENF-B"], (
            f"ids must be sorted; got {by_file['writ/session/cache.py']}"
        )

    def test_handler_second_call_unions_deduped_and_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [queried-handler-1]: a second call with overlapping+new ids produces the
        # union, sorted, deduped (no duplicates).
        # RED: handler not implemented.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        f = self._load_facade()
        sid = f"qrf-union-{uuid.uuid4().hex[:8]}"
        self._seed(tmp_path, sid)
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "writ/session/cache.py",
            json.dumps(["ENF-A", "ENF-B"]),
        ])
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "writ/session/cache.py",
            json.dumps(["ENF-B", "PERF-001"]),
        ])
        data = self._read(tmp_path, sid)
        ids = data["queried_rules_by_file"]["writ/session/cache.py"]
        assert ids == sorted(set(ids)), "ids must be sorted and deduped"
        assert set(ids) == {"ENF-A", "ENF-B", "PERF-001"}, (
            f"union must contain all unique ids; got {ids}"
        )

    def test_handler_normalizes_dot_slash_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [queried-handler-1]: "./x" and "x" collapse to the same key after
        # normalize_path (via the handler's normalize_path call).
        # RED: handler not implemented.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        f = self._load_facade()
        sid = f"qrf-norm-{uuid.uuid4().hex[:8]}"
        self._seed(tmp_path, sid)
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "./writ/session/cache.py",
            json.dumps(["ENF-A"]),
        ])
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "writ/session/cache.py",
            json.dumps(["ENF-B"]),
        ])
        data = self._read(tmp_path, sid)
        by_file = data["queried_rules_by_file"]
        assert len(by_file) == 1, (
            f"'./x' and 'x' must normalize to the same key; got keys={list(by_file.keys())}"
        )
        ids = list(by_file.values())[0]
        assert set(ids) == {"ENF-A", "ENF-B"}, (
            f"both writes must land under the same key; got {ids}"
        )


# ---------------------------------------------------------------------------
# Capability 2: cache.py default + setdefault for queried_rules_by_file
# Tag: [cache-default-1]
# ---------------------------------------------------------------------------

class TestCacheQueriedRulesByFileDefault:
    """Cap [cache-default-1] -- pure Python, no Neo4j required.

    _read_cache must include queried_rules_by_file: {} in the default dict
    (new session) AND backfill it via setdefault on an existing cache that
    lacks the key (migration path).
    """

    def test_fresh_session_cache_has_queried_rules_by_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cache-default-1]: a freshly created session has queried_rules_by_file = {}.
        # RED: key absent from the default dict in _read_cache.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        from writ.session.cache import _read_cache

        sid = f"cache-fresh-{uuid.uuid4().hex[:8]}"
        data = _read_cache(sid)
        assert "queried_rules_by_file" in data, (
            "fresh session cache must contain 'queried_rules_by_file'"
        )
        assert data["queried_rules_by_file"] == {}, (
            f"fresh session 'queried_rules_by_file' must default to {{}}; got {data['queried_rules_by_file']!r}"
        )

    def test_legacy_cache_gets_queried_rules_by_file_via_setdefault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cache-default-1]: an existing cache file that lacks queried_rules_by_file
        # gets it populated as {} on _read_cache (setdefault migration path).
        # RED: setdefault call not yet added to _read_cache.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        import json as _json
        sid = f"cache-legacy-{uuid.uuid4().hex[:8]}"
        legacy = {"loaded_rule_ids": [], "loaded_rules": [], "mode": None}
        cache_path = str(tmp_path / f"writ-session-{sid}.json")
        with open(cache_path, "w") as fh:
            _json.dump(legacy, fh)

        from writ.session.cache import _read_cache
        data = _read_cache(sid)
        assert "queried_rules_by_file" in data, (
            "legacy cache without queried_rules_by_file must get it added via setdefault"
        )
        assert data["queried_rules_by_file"] == {}, (
            f"migrated queried_rules_by_file must be {{}}; got {data['queried_rules_by_file']!r}"
        )


# ---------------------------------------------------------------------------
# Capability 3: FileChange has queried_rule_ids defaulting to []
# Tag: [schema-queried-1]
# ---------------------------------------------------------------------------

class TestFileChangeQueriedRuleIds:
    """Cap [schema-queried-1] -- pure Python, no Neo4j required.

    writ.graph.schema.FileChange must have queried_rule_ids: list[str]
    defaulting to [].
    """

    def test_filechange_defaults_queried_rule_ids_to_empty_list(self) -> None:
        # [schema-queried-1]: constructing a FileChange without queried_rule_ids
        # produces queried_rule_ids == [].
        # RED: attribute not yet defined on FileChange (AttributeError).
        from writ.graph.schema import FileChange

        fc = FileChange(
            change_id="FC-TEST-001",
            project="test-proj",
            path="writ/session/cache.py",
            change_type="modify",
            reason="test reason",
            commit_hash="abc123",
            ts="2026-06-26T10:00:00Z",
        )
        assert hasattr(fc, "queried_rule_ids"), (
            "FileChange must have a queried_rule_ids attribute"
        )
        assert fc.queried_rule_ids == [], (
            f"FileChange.queried_rule_ids must default to []; got {fc.queried_rule_ids!r}"
        )

    def test_filechange_preserves_queried_rule_ids_when_provided(self) -> None:
        # [schema-queried-1]: when queried_rule_ids is supplied, it is preserved.
        # RED: attribute not yet defined (AttributeError / field rejected).
        from writ.graph.schema import FileChange

        ids = ["ENF-A", "PERF-001"]
        fc = FileChange(
            change_id="FC-TEST-002",
            project="test-proj",
            path="writ/session/cache.py",
            change_type="add",
            reason="add the module",
            commit_hash="def456",
            ts="2026-06-26T10:00:00Z",
            queried_rule_ids=ids,
        )
        assert fc.queried_rule_ids == ids, (
            f"FileChange.queried_rule_ids must preserve the supplied list; got {fc.queried_rule_ids!r}"
        )


# ---------------------------------------------------------------------------
# Capability 4: capture_commit attaches queried_rule_ids from the session cache
# Tag: [cap-queried-1]
# ---------------------------------------------------------------------------

class TestCaptureCommitQueriedRuleIds:
    """Cap [cap-queried-1] -- uses the _FakeDB double (same pattern as harvester tests),
    no live Neo4j required for the queried_rule_ids attachment contract.

    Two cases:
    (a) session_id is set and the cache has queried_rules_by_file -> FileChange carries ids.
    (b) session_id is "" -> FileChange gets queried_rule_ids = [].
    """

    @pytest.mark.asyncio
    async def test_attaches_queried_rule_ids_from_session_cache(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cap-queried-1]: when session_id is set and the cache contains queried_rules_by_file
        # with a matching path, the FileChange created for that path carries the ids.
        # RED: capture_commit does not yet accept session_id (TypeError) or does not read
        # the cache for per-file queried ids (assertion failure).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache

        sid = f"cap-qrf-{uuid.uuid4().hex[:8]}"
        path = "writ/session/cache.py"
        rule_ids = ["ENF-A", "PERF-001"]
        _write_cache(sid, {"queried_rules_by_file": {path: rule_ids}})

        db = _make_fake_capture_db()

        import subprocess as _sp

        def _fake_runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse" and "--show-toplevel" in args:
                    return _sp.CompletedProcess(args=[], returncode=0, stdout=str(tmp_path) + "\n")
                if tok == "rev-parse":
                    return _sp.CompletedProcess(args=[], returncode=0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess(args=[], returncode=0, stdout="git@github.com:org/test.git\n")
            return _sp.CompletedProcess(args=[], returncode=0, stdout="\n")

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"TEST-CAPQRF-{uuid.uuid4().hex[:8]}",
            subject="test",
            author="tester",
            branch="main",
            files=[{"path": path, "change_type": "modify"}],
            runner=_fake_runner,
            session_id=sid,
        )

        assert len(db.filechanges) == 1, (
            f"Expected 1 FileChange created; got {len(db.filechanges)}"
        )
        fc = db.filechanges[0]
        assert fc.get("queried_rule_ids") == rule_ids, (
            f"FileChange.queried_rule_ids must be the ids from the session cache; "
            f"got {fc.get('queried_rule_ids')!r}, expected {rule_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_queried_rule_ids_empty_when_session_id_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cap-queried-1]: when session_id is "" (default), queried_rule_ids = [].
        # RED: capture_commit does not yet accept session_id (TypeError).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit

        db = _make_fake_capture_db()

        import subprocess as _sp

        def _fake_runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse":
                    return _sp.CompletedProcess(args=[], returncode=0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess(args=[], returncode=0, stdout="git@github.com:org/test2.git\n")
            return _sp.CompletedProcess(args=[], returncode=0, stdout="\n")

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"TEST-CAPQRF2-{uuid.uuid4().hex[:8]}",
            subject="test no session",
            author="tester",
            branch="main",
            files=[{"path": "writ/session/budget_tracking.py", "change_type": "modify"}],
            runner=_fake_runner,
            session_id="",
        )

        assert len(db.filechanges) == 1
        fc = db.filechanges[0]
        assert fc.get("queried_rule_ids", []) == [], (
            f"FileChange.queried_rule_ids must be [] when session_id is ''; "
            f"got {fc.get('queried_rule_ids')!r}"
        )


# ===========================================================================
# HARDENING PASS: adversarial-review additions
# ===========================================================================

# ---------------------------------------------------------------------------
# C1 hardening: normalize-test sorted assertion + trailing-flag continuation
# Item 8 -- added to existing handler concerns; new test methods
# ---------------------------------------------------------------------------

class TestAddQueriedRulesForFileHandlerHardening:
    """C1 hardening (item 8): two gap-closing tests for the handler."""

    def _load_facade(self):
        import importlib.util, importlib
        facade_path = str(Path(__file__).resolve().parent.parent / "bin/lib/writ-session.py")
        spec = importlib.util.spec_from_file_location("writ_session_c1h", facade_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _seed(self, tmp_path, sid, **fields):
        import sys, importlib
        skill_root = str(Path(__file__).resolve().parent.parent)
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
        cache = importlib.import_module("writ.session.cache")
        data = cache._read_cache(sid)
        data.update(fields)
        cache._write_cache(sid, data)

    def _read(self, tmp_path, sid):
        import sys, importlib
        skill_root = str(Path(__file__).resolve().parent.parent)
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
        cache = importlib.import_module("writ.session.cache")
        return cache._read_cache(sid)

    def test_normalize_result_is_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [queried-handler-1] item 8a: after normalizing ./x and x to the same key,
        # the merged list must be SORTED (not insertion-ordered).
        # RED: handler not registered.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        f = self._load_facade()
        sid = f"c1h-sorted-{uuid.uuid4().hex[:8]}"
        self._seed(tmp_path, sid)
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "./writ/session/cache.py",
            json.dumps(["ZZZ-001"]),
        ])
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "writ/session/cache.py",
            json.dumps(["AAA-001"]),
        ])
        data = self._read(tmp_path, sid)
        by_file = data["queried_rules_by_file"]
        assert len(by_file) == 1, f"must normalize to one key; got {list(by_file.keys())}"
        ids = list(by_file.values())[0]
        assert ids == sorted(ids), (
            f"merged list after path-collision must be sorted; got {ids}"
        )

    def test_trailing_flag_dispatcher_keeps_parsing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [queried-handler-1] item 8b: --add-queried-rules-for-file returns i+3 so
        # the dispatcher keeps parsing subsequent flags. A combined call with
        # --add-queried-rules-for-file and --add-rules must land both effects.
        # RED: handler not registered (KeyError skip) or returns wrong i.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        f = self._load_facade()
        sid = f"c1h-trail-{uuid.uuid4().hex[:8]}"
        self._seed(tmp_path, sid)
        f.cmd_update(sid, [
            "--add-queried-rules-for-file", "writ/session/cache.py",
            json.dumps(["ENF-Q-001"]),
            "--add-rules", json.dumps(["ENF-R-001"]),
        ])
        data = self._read(tmp_path, sid)
        # Effect 1: queried_rules_by_file updated.
        assert "queried_rules_by_file" in data, (
            "queried_rules_by_file must be in cache after combined update"
        )
        assert "ENF-Q-001" in data["queried_rules_by_file"].get("writ/session/cache.py", []), (
            "ENF-Q-001 must land in queried_rules_by_file"
        )
        # Effect 2: --add-rules also ran (handler returned i+3, not i+2).
        assert "ENF-R-001" in data.get("loaded_rule_ids", []), (
            "ENF-R-001 must land in loaded_rule_ids (--add-rules ran after the handler)"
        )


# ---------------------------------------------------------------------------
# C2 setdefault-no-overwrite
# Item 9 -- existing cache must not be clobbered
# ---------------------------------------------------------------------------

class TestCacheQueriedRulesByFileNoOverwrite:
    """C2 item 9: setdefault must not clobber a non-empty existing value."""

    def test_existing_non_empty_value_not_clobbered_on_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cache-default-1] item 9: a legacy on-disk cache that already has a
        # NON-EMPTY queried_rules_by_file must NOT be reset to {} on _read_cache.
        # setdefault only fills the key when absent; overwrite would be wrong.
        # RED: key absent from setdefault list (KeyError on subsequent access).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        import json as _json
        sid = f"cache-nooverwrite-{uuid.uuid4().hex[:8]}"
        pre_existing = {"writ/session/cache.py": ["ENF-EXISTING-001"]}
        legacy = {
            "loaded_rule_ids": [],
            "loaded_rules": [],
            "mode": None,
            "queried_rules_by_file": pre_existing,
        }
        cache_path = str(tmp_path / f"writ-session-{sid}.json")
        with open(cache_path, "w") as fh:
            _json.dump(legacy, fh)

        from writ.session.cache import _read_cache
        data = _read_cache(sid)
        assert data["queried_rules_by_file"] == pre_existing, (
            f"_read_cache must NOT overwrite an existing non-empty queried_rules_by_file; "
            f"expected {pre_existing!r}, got {data['queried_rules_by_file']!r}"
        )


# ---------------------------------------------------------------------------
# Item 1: capture_commit multi-file per-path isolation
# ---------------------------------------------------------------------------

class TestCaptureCommitPerPathQueriedRuleIds:
    """Item 1: per-path lookup -- a.py gets its ids, b.py gets []."""

    @pytest.mark.asyncio
    async def test_multi_file_per_path_isolation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cap-queried-1] item 1: session cache has queried_rules_by_file = {"a.py": ["R1"]}.
        # commit touches a.py AND b.py. FileChange for a.py must carry ["R1"]; FileChange
        # for b.py must carry [].
        # RED: capture_commit does not yet accept session_id, or uses a flat list instead
        # of per-path lookup.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache

        sid = f"cap-multi-{uuid.uuid4().hex[:8]}"
        _write_cache(sid, {"queried_rules_by_file": {"a.py": ["R1"]}})

        db = _make_fake_capture_db()

        import subprocess as _sp

        def _runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse":
                    return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess([], 0, stdout="git@github.com:org/multi.git\n")
            return _sp.CompletedProcess([], 0, stdout="\n")

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"MULTI-{uuid.uuid4().hex[:8]}",
            subject="multi-file",
            author="tester",
            branch="main",
            files=[
                {"path": "a.py", "change_type": "modify"},
                {"path": "b.py", "change_type": "add"},
            ],
            runner=_runner,
            session_id=sid,
        )

        assert len(db.filechanges) == 2, (
            f"Expected 2 FileChanges; got {len(db.filechanges)}"
        )
        by_path = {fc["path"]: fc for fc in db.filechanges}
        assert by_path["a.py"].get("queried_rule_ids") == ["R1"], (
            f"a.py must carry [\"R1\"]; got {by_path['a.py'].get('queried_rule_ids')!r}"
        )
        assert by_path["b.py"].get("queried_rule_ids") == [], (
            f"b.py must carry [] (absent from cache); got {by_path['b.py'].get('queried_rule_ids')!r}"
        )


# ---------------------------------------------------------------------------
# Regression (review finding): the write hook keys queried_rules_by_file by the
# ABSOLUTE write path, the commit hook looks up by the git repo-relative path.
# capture_commit must reconcile them or queried rules are silently always [].
# ---------------------------------------------------------------------------

class TestCaptureCommitAbsoluteKeyReconciliation:
    """The write hook stores normalize_path(ABSOLUTE file path) as the key; the
    commit passes the git repo-relative path. The FileChange must still carry the
    ids, proving capture_commit reconstructs the absolute key from cwd + path.
    """

    @pytest.mark.asyncio
    async def test_relative_commit_path_matches_absolute_cache_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cap-queried-abskey]: cache keyed exactly as writ-pre-write-dispatch.sh ->
        # _upd_add_queried_rules_for_file keys it (normalize_path of the absolute
        # path), commit passes the repo-relative path. RED before the fix: the
        # commit-side lookup uses only the relative key and misses the absolute one.
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache
        from writ.session.remote_parse import normalize_path

        sid = f"cap-abskey-{uuid.uuid4().hex[:8]}"
        abs_key = normalize_path(os.path.join(str(tmp_path), "writ/session/cache.py"))
        _write_cache(sid, {"queried_rules_by_file": {abs_key: ["R1", "R2"]}})

        db = _make_fake_capture_db()

        import subprocess as _sp

        def _runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse":
                    return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess([], 0, stdout="git@github.com:org/abskey.git\n")
            return _sp.CompletedProcess([], 0, stdout="\n")

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"ABSKEY-{uuid.uuid4().hex[:8]}",
            subject="abs key reconcile",
            author="tester",
            branch="main",
            files=[{"path": "writ/session/cache.py", "change_type": "modify"}],
            runner=_runner,
            session_id=sid,
        )

        assert len(db.filechanges) == 1, f"expected 1 FileChange; got {len(db.filechanges)}"
        fc = db.filechanges[0]
        assert fc.get("queried_rule_ids") == ["R1", "R2"], (
            "commit-side lookup must reconcile the git repo-relative path with the "
            f"absolute cache key the write hook stores; got {fc.get('queried_rule_ids')!r}"
        )


# ---------------------------------------------------------------------------
# Item 2: capture_commit best-effort: _read_cache raises -> [] not exception
# ---------------------------------------------------------------------------

class TestCaptureCommitQueriedRuleIdsBestEffort:
    """Item 2: when _read_cache raises, capture_commit succeeds with queried_rule_ids=[]."""

    @pytest.mark.asyncio
    async def test_best_effort_empty_on_cache_read_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [cap-queried-1] item 2: monkeypatch writ.session.cache._read_cache to raise;
        # capture_commit must still succeed (no propagated exception) and the FileChange
        # must carry queried_rule_ids = [] (best-effort per plan).
        # RED: capture_commit does not yet accept session_id (TypeError).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        import writ.session.cache as _cache_mod
        monkeypatch.setattr(_cache_mod, "_read_cache", lambda sid: (_ for _ in ()).throw(OSError("simulated read failure")))

        from writ.session.commit_capture import capture_commit

        db = _make_fake_capture_db()

        import subprocess as _sp

        def _runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse":
                    return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess([], 0, stdout="git@github.com:org/be.git\n")
            return _sp.CompletedProcess([], 0, stdout="\n")

        # Must not raise despite _read_cache failing.
        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"BE-{uuid.uuid4().hex[:8]}",
            subject="best-effort test",
            author="tester",
            branch="main",
            files=[{"path": "writ/session/cache.py", "change_type": "modify"}],
            runner=_runner,
            session_id="some-session-id",
        )

        assert len(db.filechanges) == 1, (
            f"Expected 1 FileChange even after cache-read failure; got {len(db.filechanges)}"
        )
        fc = db.filechanges[0]
        assert fc.get("queried_rule_ids", []) == [], (
            f"queried_rule_ids must be [] when _read_cache raises; got {fc.get('queried_rule_ids')!r}"
        )


# ---------------------------------------------------------------------------
# Item 4: write-hook wiring -- static assertion on writ-pre-write-dispatch.sh
# ---------------------------------------------------------------------------

class TestPreWriteDispatchQueriedRulesWiring:
    """Item 4: writ-pre-write-dispatch.sh must call --add-queried-rules-for-file.

    Static source assertion (same pattern as test_pre_write_dispatch.py).
    The hook script must invoke _writ_session update ... --add-queried-rules-for-file
    "$DECISION_FILE" "$NEW_RULE_IDS" guarded by [ -n "$DECISION_FILE" ] inside the
    existing NEW_RULE_IDS guard.
    """

    DISPATCH = str(Path(__file__).resolve().parent.parent / "hooks/scripts/writ-pre-write-dispatch.sh")

    def test_dispatch_calls_add_queried_rules_for_file(self) -> None:
        # Item 4: the --add-queried-rules-for-file flag must appear in the hook so
        # the handler is actually called at write time, not just registered.
        # RED: flag absent from script source.
        with open(self.DISPATCH) as fh:
            src = fh.read()
        assert "--add-queried-rules-for-file" in src, (
            "writ-pre-write-dispatch.sh must call --add-queried-rules-for-file "
            "to record per-file queried rule ids into the session cache at write time"
        )

    def test_dispatch_guards_decision_file_not_empty(self) -> None:
        # Item 4: the call must be guarded by [ -n "$DECISION_FILE" ] so it only
        # fires when a file path is known (matches the plan's guard condition).
        # RED: guard absent (unconditional call on an empty DECISION_FILE).
        with open(self.DISPATCH) as fh:
            src = fh.read()
        assert '[ -n "$DECISION_FILE" ]' in src, (
            "writ-pre-write-dispatch.sh must guard --add-queried-rules-for-file "
            "with [ -n \"$DECISION_FILE\" ] so it only fires when a path is known"
        )

    def test_dispatch_passes_decision_file_and_new_rule_ids(self) -> None:
        # Item 4: the flag invocation must pass both $DECISION_FILE and $NEW_RULE_IDS
        # as the two required arguments (arity=2 per the handler spec).
        # RED: arguments absent from script source.
        with open(self.DISPATCH) as fh:
            src = fh.read()
        assert '"$DECISION_FILE"' in src, (
            "dispatch must pass \"$DECISION_FILE\" as the path arg to --add-queried-rules-for-file"
        )
        assert '"$NEW_RULE_IDS"' in src, (
            "dispatch must pass \"$NEW_RULE_IDS\" as the ids arg to --add-queried-rules-for-file"
        )


class TestSubagentStartStampWiring:
    """Step 1a: writ-subagent-start.sh must stamp parent_session_id + agent_type onto
    the child cache (static source assertion, same pattern as the dispatch-wiring test).
    Without this wiring the handlers exist but are never called, so the commit-time
    merge silently captures no sub-agent queried rules in production.
    """

    START_HOOK = str(Path(__file__).resolve().parent.parent / "hooks/scripts/writ-subagent-start.sh")

    def test_start_hook_stamps_parent_session_id(self) -> None:
        # RED: the --parent-session-id stamp is absent from the hook.
        with open(self.START_HOOK) as fh:
            src = fh.read()
        assert "--parent-session-id" in src, (
            "writ-subagent-start.sh must call _writ_session update ... --parent-session-id "
            "so the child cache links to the committing session for the commit-time merge"
        )

    def test_start_hook_stamps_agent_type(self) -> None:
        # RED: the --agent-type stamp is absent from the hook.
        with open(self.START_HOOK) as fh:
            src = fh.read()
        assert "--agent-type" in src, (
            "writ-subagent-start.sh must stamp --agent-type onto the child cache"
        )


# ---------------------------------------------------------------------------
# Item 5: server route plumbing -- session_id reaches capture_commit
# ---------------------------------------------------------------------------

class TestCommitCaptureRouteSessionId:
    """Item 5: /commit/capture passes session_id from the request body to capture_commit."""

    def test_route_passes_session_id_to_capture_commit(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Item 5: sending "session_id" in the JSON body must cause the route to
        # forward it to capture_commit. Verified by spying on capture_commit via
        # monkeypatch and recording the keyword arg it received.
        # RED: CommitCaptureRequest does not yet have a session_id field, OR the
        # route does not forward it (KeyError / wrong value in spy capture).
        import writ.server as _srv
        import writ.session.commit_capture as _cc

        captured_kwargs: list[dict] = []

        async def _spy_capture_commit(db, **kwargs):
            captured_kwargs.append(kwargs)

        # Patch the name as bound in writ.server (from-import holds a direct ref).
        monkeypatch.setattr(_srv, "capture_commit", _spy_capture_commit)

        monkeypatch.setattr(_srv, "_db", _make_fake_route_db())

        resp = client.post("/commit/capture", json={
            "project_root": _TEST_REPO_ROOT + "/cap-sid",
            "commit_hash": "TEST1D-SID-001",
            "subject": "test session_id routing",
            "author": "tester",
            "branch": "main",
            "files": [],
            "session_id": "sess-route-test-001",
        })
        assert resp.status_code == 200, (
            f"POST /commit/capture must return 200; got {resp.status_code}: {resp.text}"
        )
        assert len(captured_kwargs) == 1, (
            f"capture_commit spy must be called exactly once; got {len(captured_kwargs)} calls"
        )
        actual_sid = captured_kwargs[0].get("session_id")
        assert actual_sid == "sess-route-test-001", (
            f"capture_commit must be called with session_id='sess-route-test-001'; "
            f"got session_id={actual_sid!r}. The route must forward request.session_id."
        )


# ---------------------------------------------------------------------------
# Item 6: post-commit hook reads /tmp/writ-current-session and includes session_id
# ---------------------------------------------------------------------------

class TestPostCommitHookSessionIdBridge:
    """Item 6: static source assertions on hooks/git/post-commit for session_id bridge."""

    _POST_HOOK = str(Path(__file__).resolve().parent.parent / "hooks/git/post-commit")

    def test_post_commit_reads_writ_current_session(self) -> None:
        # Item 6: the hook must read /tmp/writ-current-session to obtain the
        # session_id (the plan's session-id bridge for git hooks).
        # RED: file does not yet read /tmp/writ-current-session.
        with open(self._POST_HOOK) as fh:
            src = fh.read()
        assert "/tmp/writ-current-session" in src, (
            "hooks/git/post-commit must read /tmp/writ-current-session "
            "to bridge the session_id into the /commit/capture payload"
        )

    def test_post_commit_includes_session_id_in_json_payload(self) -> None:
        # Item 6: the JSON body POSTed to /commit/capture must include "session_id".
        # RED: post-commit does not yet emit "session_id" in the request body.
        with open(self._POST_HOOK) as fh:
            src = fh.read()
        assert '"session_id"' in src, (
            "hooks/git/post-commit must include \"session_id\" in the JSON payload "
            "sent to /commit/capture so the server can forward it to capture_commit"
        )


# ===========================================================================
# Phase 1f: sub-agent queried rules merged onto FileChange + cited_rule_ids
# Capabilities from plan.md Phase 1f / capabilities.md
# ===========================================================================

# ---------------------------------------------------------------------------
# Capability: capture_commit merges a linked sub-agent cache's queried rules
#   onto the correct FileChange, merged with the main session cache,
#   with per-path isolation preserved.
# Tag: [1f-cap-subagent-merge]
# ---------------------------------------------------------------------------

def _make_fake_route_db():
    """Minimal DB double for git-hooks route / capture-commit spy tests.

    Implements ONLY get_projects + create_project. It deliberately omits the
    capture methods (create_commit, create_filechange, wire_*, ...) so a route
    that wrongly invokes the capture path raises AttributeError -- the signal
    that the auto-install route must not call create_commit.
    """

    class _RouteDB:
        async def get_projects(self):
            return []

        async def create_project(self, *a, **kw):
            return "test-project"

    return _RouteDB()


def _make_fake_capture_db(*, open_decisions=None, resolve_count=0):
    """Full capture-contract DB double for capture_commit tests.

    Generalizes the former _make_fake_db_for_merge: tracks created commits and
    filechanges, and parametrizes the open-decision snapshot (open_decisions)
    and the resolve_file_claims return (resolve_count). wire_* edges are no-ops;
    no capture test asserts on recorded edges.
    """

    class _CaptureDB:
        def __init__(self):
            self.filechanges: list[dict] = []
            self.commits: list[dict] = []

        async def get_projects(self):
            return []

        async def create_project(self, *a, **kw):
            return "test-proj"

        async def create_commit(self, **kw):
            self.commits.append(kw)
            return kw.get("commit_hash")

        async def create_filechange(self, **kw):
            self.filechanges.append(kw)
            return kw.get("change_id")

        async def get_open_decisions_for_path(self, project, path):
            # Echo the queried path into each decision's planned_files (matching
            # the original _FakeDBWithCited behavior) so the fake discriminates
            # by the queried path rather than a construction-time literal.
            result = []
            for decision in open_decisions or []:
                entry = dict(decision)
                planned = entry.get("planned_files")
                if planned:
                    entry["planned_files"] = [{**pf, "path": path} for pf in planned]
                result.append(entry)
            return result

        async def resolve_file_claims(self, project, path):
            return resolve_count

        async def wire_includes(self, *a):
            pass

        async def wire_motivated_by(self, *a):
            pass

        async def wire_realizes(self, *a):
            pass

        async def wire_has_change(self, *a):
            pass

        async def wire_has_commit(self, *a):
            pass

    return _CaptureDB()


def _make_fake_runner_for_merge(tmp_path):
    """subprocess.run stub that satisfies rev-parse and get-url in merge tests."""
    import subprocess as _sp

    def _runner(args, *, cwd=None, **_kw):
        for tok in args:
            if tok == "rev-parse":
                return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
            if tok == "get-url":
                return _sp.CompletedProcess([], 0, stdout="git@github.com:org/merge-test.git\n")
        return _sp.CompletedProcess([], 0, stdout="\n")

    return _runner


class TestCaptureCommitSubagentMerge:
    """Cap [1f-cap-subagent-merge]: capture_commit merges queried rules from a
    linked sub-agent cache into the correct FileChange, per-path isolated.

    Setup: write both a main session cache and a child agent cache under the
    same tmp WRIT_CACHE_DIR. The child has parent_session_id == the committing
    session_id and is_subagent=True. After capture_commit runs, the FileChange
    for the child's path must carry the merged union from both caches; other
    paths must NOT see the child's rules (per-path isolation).

    RED: capture_commit does not yet call _collect_subagent_queried_rules
    (function does not exist) or does not merge its result into the FileChange
    (queried_rule_ids on the relevant FileChange is missing the child's ids).
    """

    @pytest.mark.asyncio
    async def test_subagent_queried_rules_merged_onto_filechange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [1f-cap-subagent-merge]: a sub-agent cache with parent_session_id == the
        # committing session_id and is_subagent=True contributes its
        # queried_rules_by_file to the FileChange for the relevant path, MERGED
        # with the main session cache's queried ids for that path.
        #
        # Arrangement:
        #   main cache: {"queried_rules_by_file": {"a.py": ["MAIN-R1"]}}
        #   child cache: {"parent_session_id": sid, "is_subagent": True,
        #                  "queried_rules_by_file": {"a.py": ["CHILD-R2"]}}
        # Expected FileChange for a.py: queried_rule_ids == ["CHILD-R2", "MAIN-R1"]
        # (sorted union).
        #
        # RED: _collect_subagent_queried_rules does not exist yet (ImportError
        # inside capture_commit) or the merged result is not passed to
        # create_filechange (queried_rule_ids == ["MAIN-R1"] not the full union).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache

        sid = f"1f-merge-main-{uuid.uuid4().hex[:8]}"
        child_agent_id = f"1f-merge-child-{uuid.uuid4().hex[:8]}"

        # Write the main session cache.
        _write_cache(sid, {
            "queried_rules_by_file": {"a.py": ["MAIN-R1"]},
        })
        # Write the child agent cache.
        child_cache_path = tmp_path / f"writ-session-{child_agent_id}.json"
        child_cache_path.write_text(json.dumps({
            "parent_session_id": sid,
            "is_subagent": True,
            "queried_rules_by_file": {"a.py": ["CHILD-R2"]},
        }))

        db = _make_fake_capture_db()

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"MERGE-{uuid.uuid4().hex[:8]}",
            subject="test merge",
            author="tester",
            branch="main",
            files=[{"path": "a.py", "change_type": "modify"}],
            runner=_make_fake_runner_for_merge(tmp_path),
            session_id=sid,
        )

        assert len(db.filechanges) == 1, (
            f"Expected 1 FileChange; got {len(db.filechanges)}"
        )
        fc = db.filechanges[0]
        actual_ids = set(fc.get("queried_rule_ids") or [])
        assert actual_ids == {"MAIN-R1", "CHILD-R2"}, (
            f"FileChange.queried_rule_ids must be the merged union of main + child; "
            f"expected {{'MAIN-R1', 'CHILD-R2'}}, got {actual_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_per_path_isolation_child_rules_dont_leak_to_other_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [1f-cap-subagent-merge]: per-path isolation. The child cache has rules
        # only for "a.py". The commit touches both "a.py" and "b.py". The
        # FileChange for "b.py" must carry queried_rule_ids == [] (the child's
        # "a.py" rules must NOT leak onto "b.py").
        #
        # RED: same as above (function absent or merge uses a flat list).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache

        sid = f"1f-isolate-{uuid.uuid4().hex[:8]}"
        child_agent_id = f"1f-isolate-child-{uuid.uuid4().hex[:8]}"

        _write_cache(sid, {"queried_rules_by_file": {}})
        child_cache_path = tmp_path / f"writ-session-{child_agent_id}.json"
        child_cache_path.write_text(json.dumps({
            "parent_session_id": sid,
            "is_subagent": True,
            "queried_rules_by_file": {"a.py": ["CHILD-ONLY"]},
        }))

        db = _make_fake_capture_db()

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"ISOLATE-{uuid.uuid4().hex[:8]}",
            subject="test isolation",
            author="tester",
            branch="main",
            files=[
                {"path": "a.py", "change_type": "modify"},
                {"path": "b.py", "change_type": "add"},
            ],
            runner=_make_fake_runner_for_merge(tmp_path),
            session_id=sid,
        )

        assert len(db.filechanges) == 2, (
            f"Expected 2 FileChanges; got {len(db.filechanges)}"
        )
        by_path = {fc["path"]: fc for fc in db.filechanges}
        assert "CHILD-ONLY" in set(by_path["a.py"].get("queried_rule_ids") or []), (
            "a.py must carry the child's rule id"
        )
        assert by_path["b.py"].get("queried_rule_ids", []) == [], (
            f"b.py must carry [] (child's a.py rules must not leak to b.py); "
            f"got {by_path['b.py'].get('queried_rule_ids')!r}"
        )


# ---------------------------------------------------------------------------
# Capability: capture_commit snapshots cited_rule_ids onto the FileChange
#   from entry["governing_rule_ids"] at commit time.
# Tag: [1f-cap-cited-snapshot]
# ---------------------------------------------------------------------------

class TestCaptureCommitCitedRuleIds:
    """Cap [1f-cap-cited-snapshot]: capture_commit attaches cited_rule_ids to
    each FileChange from the governing_rule_ids of the entry returned by
    resolve_reasons_for_files.

    The _FakeDB here overrides get_open_decisions_for_path to return a
    Decision whose governing_rule_ids is ["CITED-R1"]. After capture_commit,
    the FileChange must carry cited_rule_ids == ["CITED-R1"].

    RED: FileChange schema does not yet have cited_rule_ids (AttributeError on
    the schema model) or create_filechange is not called with cited_rule_ids
    (key absent / [] on the created FileChange).
    """

    @pytest.mark.asyncio
    async def test_cited_rule_ids_attached_from_governing_rule_ids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [1f-cap-cited-snapshot]: the entry produced by resolve_reasons_for_files
        # carries governing_rule_ids from the open Decision. capture_commit must
        # pass this as cited_rule_ids to create_filechange.
        #
        # RED: schema.FileChange lacks cited_rule_ids field (AttributeError in
        # create_filechange validation) OR capture_commit does not read
        # entry.get("governing_rule_ids") and pass it to create_filechange
        # (cited_rule_ids absent or [] on the FileChange record).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache
        import subprocess as _sp

        sid = f"1f-cited-{uuid.uuid4().hex[:8]}"
        _write_cache(sid, {"queried_rules_by_file": {}})

        # Decision with a governing rule so resolve_reasons_for_files populates
        # governing_rule_ids = ["CITED-R1"] on the entry for the committed path.
        db = _make_fake_capture_db(
            open_decisions=[{
                "decision_id": "DEC-1f-cited-001",
                "governing_rule_ids": ["CITED-R1"],
                "reason_for_path": "add the module",
                "planned_files": [
                    {"path": "writ/session/cache.py", "reason": "add the module", "resolved": False}
                ],
                "ts": "2026-06-27T10:00:00+00:00",
            }],
            resolve_count=1,
        )

        def _runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse":
                    return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess([], 0, stdout="git@github.com:org/cited.git\n")
            return _sp.CompletedProcess([], 0, stdout="\n")

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"CITED-{uuid.uuid4().hex[:8]}",
            subject="test cited snapshot",
            author="tester",
            branch="main",
            files=[{"path": "writ/session/cache.py", "change_type": "modify"}],
            runner=_runner,
            session_id=sid,
        )

        assert len(db.filechanges) == 1, (
            f"Expected 1 FileChange; got {len(db.filechanges)}"
        )
        fc = db.filechanges[0]
        cited_ids = fc.get("cited_rule_ids")
        assert cited_ids is not None, (
            "create_filechange must be called with a 'cited_rule_ids' keyword arg; "
            "key absent from recorded FileChange kwargs"
        )
        assert "CITED-R1" in cited_ids, (
            f"cited_rule_ids must contain 'CITED-R1' from the Decision's "
            f"governing_rule_ids; got {cited_ids!r}"
        )

    @pytest.mark.asyncio
    async def test_cited_rule_ids_empty_when_no_decision_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [1f-cap-cited-snapshot]: when no open Decision matches the path,
        # cited_rule_ids must be [] (not absent, not None).
        #
        # RED: cited_rule_ids key absent from FileChange (the field hasn't been
        # added to create_filechange call yet).
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache
        import subprocess as _sp

        sid = f"1f-cited-empty-{uuid.uuid4().hex[:8]}"
        _write_cache(sid, {"queried_rules_by_file": {}})

        db = _make_fake_capture_db()

        def _runner(args, *, cwd=None, **_kw):
            for tok in args:
                if tok == "rev-parse":
                    return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
                if tok == "get-url":
                    return _sp.CompletedProcess([], 0, stdout="git@github.com:org/cited2.git\n")
            return _sp.CompletedProcess([], 0, stdout="\n")

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"CITED2-{uuid.uuid4().hex[:8]}",
            subject="test cited empty",
            author="tester",
            branch="main",
            files=[{"path": "writ/unplanned.py", "change_type": "add"}],
            runner=_runner,
            session_id=sid,
        )

        assert len(db.filechanges) == 1, (
            f"Expected 1 FileChange; got {len(db.filechanges)}"
        )
        fc = db.filechanges[0]
        cited_ids = fc.get("cited_rule_ids", "ABSENT")
        assert cited_ids != "ABSENT", (
            "create_filechange must be called with 'cited_rule_ids' keyword arg even "
            "when no Decision matches; key was absent from the FileChange record"
        )
        assert cited_ids == [], (
            f"cited_rule_ids must be [] when no Decision matches; got {cited_ids!r}"
        )


# ===========================================================================
# Churn fix: capture_commit uses path + recency, not just session_id match
# Capabilities from plan.md "robust sub-agent queried-rule merge"
#
#   [cap-churn-1] a sub-agent whose parent_session_id does NOT match the
#                 committing session_id contributes its queried rules to the
#                 correct FileChange when the cache is recent enough and holds
#                 the committed path's key (the churn regression test)
# ===========================================================================


def _make_fake_runner_for_churn(tmp_path, parent_commit_ts: str):
    """subprocess.run stub that handles rev-parse, get-url, AND git-show %ct.

    The git-show -s --format=%ct <hash>^ branch is needed by the new
    _parent_commit_ts helper in commit_capture.py.

    parent_commit_ts: a unix timestamp string (e.g. "1750000000") to return
    for the git-show call so since_ts is set BELOW the child cache mtime
    and the cache is admitted by the recency filter.
    """
    import subprocess as _sp

    def _runner(args, *, cwd=None, **_kw):
        str_args = [a for a in args if isinstance(a, str)]
        # git show -s --format=%ct <hash>^ -> return parent_commit_ts
        if "show" in str_args and "%ct" in " ".join(str_args):
            return _sp.CompletedProcess([], 0, stdout=parent_commit_ts + "\n")
        for tok in str_args:
            if tok == "rev-parse":
                return _sp.CompletedProcess([], 0, stdout=str(tmp_path) + "\n")
            if tok == "get-url":
                return _sp.CompletedProcess(
                    [], 0, stdout="git@github.com:org1d/churn-fix.git\n"
                )
        return _sp.CompletedProcess([], 0, stdout="\n")

    return _runner


class TestCaptureCommitChurnFix:
    """[cap-churn-1]: regression test for the session-id churn bug.

    Scenario: a sub-agent did real work and keyed its queried rules by the
    committed file path, but its parent_session_id was stamped from
    /tmp/writ-current-session at SubagentStart, which then churned to a
    different value before the commit ran. The old code called
    _collect_subagent_queried_rules(session_id) and required parent_session_id
    to match exactly, so the sub-agent was silently dropped and the FileChange
    got queried_rule_ids=[].

    After the fix, capture_commit builds committed_keys and passes since_ts
    from the parent commit timestamp, so the sub-agent is found by PATH even
    when parent_session_id does not match.

    Right-reason RED (before implementation):
      capture_commit calls _collect_subagent_queried_rules(session_id) with
      only the session_id argument. The new call shape requires
      _collect_subagent_queried_rules(session_id, committed_keys, since_ts).
      Before the change, _collect is called without committed_keys/since_ts,
      so the non-matching-parent sub-agent is skipped by the parent_match
      check. The FileChange queried_rule_ids is [] -> assertion fails.
    """

    @pytest.mark.asyncio
    async def test_cap_churn_1_non_matching_parent_sub_agent_lands_on_filechange(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """[cap-churn-1]: non-matching-parent sub-agent with recent mtime and
        committed path contributes its queried rule ids to the FileChange.

        Setup:
          - committing session_id = "churn-main-session"
          - sub-agent parent_session_id = "churn-stale-session" (churned, does
            not match the committing session)
          - sub-agent is_subagent = True
          - sub-agent queried_rules_by_file = {<abs_key_of_committed_path>: ["CHURN-R1"]}
          - sub-agent cache mtime is AFTER the parent-commit timestamp
            (runner returns a ts well in the past so since_ts admits the cache)
          - committed file = "writ/session/cache.py"

        After capture_commit, the FileChange for "writ/session/cache.py" must
        carry queried_rule_ids containing "CHURN-R1".

        A second committed file ("writ/session/budget_tracking.py") with no
        sub-agent entry must carry queried_rule_ids == [] (per-path isolation).

        RED: the current capture_commit calls _collect_subagent_queried_rules
        with only session_id. The sub-agent parent_session_id does not match,
        so it is excluded. FileChange for cache.py gets [] -> assertion fails.
        """
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        from writ.session.commit_capture import capture_commit
        from writ.session.cache import _write_cache
        from writ.session.remote_parse import normalize_path

        committing_sid = f"churn-main-{uuid.uuid4().hex[:8]}"
        child_agent_id = f"churn-child-{uuid.uuid4().hex[:8]}"
        committed_rel_path = "writ/session/cache.py"
        other_rel_path = "writ/session/budget_tracking.py"

        # Build the absolute key the write hook would have stamped.
        abs_key = normalize_path(os.path.join(str(tmp_path), committed_rel_path))

        # Write the main session cache (no queried rules for the committed path).
        _write_cache(committing_sid, {
            "queried_rules_by_file": {},
        })

        # Write the child cache with a STALE / mismatched parent_session_id.
        child_cache_path = tmp_path / f"writ-session-{child_agent_id}.json"
        import json as _json
        child_cache_path.write_text(_json.dumps({
            "parent_session_id": "churn-stale-session-that-no-longer-matches",
            "is_subagent": True,
            "queried_rules_by_file": {abs_key: ["CHURN-R1"]},
        }))

        # The child cache mtime is effectively "now" (just written).
        # The runner returns a parent-commit ts well in the past (epoch + 1000)
        # so since_ts = 1000.0, which is far below the file's mtime -> admitted.
        runner = _make_fake_runner_for_churn(tmp_path, parent_commit_ts="1000")

        db = _make_fake_capture_db()

        await capture_commit(
            db,
            cwd=str(tmp_path),
            commit_hash=f"CHURN-{uuid.uuid4().hex[:8]}",
            subject="churn regression test",
            author="tester",
            branch="main",
            files=[
                {"path": committed_rel_path, "change_type": "modify"},
                {"path": other_rel_path, "change_type": "add"},
            ],
            runner=runner,
            session_id=committing_sid,
        )

        assert len(db.filechanges) == 2, (
            f"[cap-churn-1] Expected 2 FileChanges; got {len(db.filechanges)}"
        )
        by_path = {fc["path"]: fc for fc in db.filechanges}

        # The sub-agent's queried rule must land on the committed path's FileChange.
        queried_ids = set(by_path[committed_rel_path].get("queried_rule_ids") or [])
        assert "CHURN-R1" in queried_ids, (
            f"[cap-churn-1] CHURN-R1 must appear on {committed_rel_path!r}'s "
            f"FileChange.queried_rule_ids even though the sub-agent parent_session_id "
            f"did not match the committing session_id; "
            f"got queried_rule_ids={queried_ids!r}. "
            f"This fails before the churn fix because capture_commit calls "
            f"_collect_subagent_queried_rules(session_id) without committed_keys "
            f"or since_ts, so the non-matching-parent sub-agent is dropped."
        )

        # The second committed file with no sub-agent entry must get [].
        other_ids = by_path[other_rel_path].get("queried_rule_ids") or []
        assert other_ids == [], (
            f"[cap-churn-1] {other_rel_path!r} must carry [] (no sub-agent rules "
            f"for that path); got {other_ids!r}"
        )


class TestParentCommitTsFallback:
    """[cap-churn-2]: when the parent-commit time is unavailable (git fails, or a
    first commit), _parent_commit_ts falls back to a tight recent window, NOT 0.0.
    A 0.0 bound disables the recency fence (mtime >= 0.0 is always true) and would
    let a stale cross-conversation cache leak into the merge.
    """

    def test_git_failure_falls_back_to_recent_window_not_zero(self) -> None:
        import subprocess as _sp
        from writ.session.commit_capture import (
            _parent_commit_ts,
            _RECENCY_FALLBACK_WINDOW_S,
        )

        def _failing_runner(args, **_kw):
            return _sp.CompletedProcess(args, 1, stdout="", stderr="fatal: bad revision")

        ts = _parent_commit_ts("/repo", "deadbeef", runner=_failing_runner)
        assert ts != 0.0, (
            "git-failure fallback must not be 0.0; a 0.0 bound disables the recency "
            "fence and lets stale cross-conversation caches leak"
        )
        expected = time.time() - _RECENCY_FALLBACK_WINDOW_S
        assert abs(ts - expected) < 60, (
            f"git-failure fallback must be about now minus the window ({expected}); "
            f"got {ts}"
        )

    def test_valid_parent_ct_is_returned(self) -> None:
        import subprocess as _sp
        from writ.session.commit_capture import _parent_commit_ts

        def _ok_runner(args, **_kw):
            return _sp.CompletedProcess(args, 0, stdout="1700000000\n")

        assert _parent_commit_ts("/repo", "deadbeef", runner=_ok_runner) == 1700000000.0
