"""Decision Memory Phase 1c: Decision capture at approve.

Test skeleton for the capability gate defined in capabilities.md and plan.md.
Every test in this file is RED until the implementer builds the substrate.

Run interpreter: .venv/bin/python -m pytest (has onnxruntime; system python3
errors on embedding imports).

Neo4j-gated tests use the same db_clean fixture/skip pattern as
test_decision_memory_records.py:97-115 and test_decision_memory_identity.py:116-161,
with scope "test-dm-1c" and the two-pass :Project teardown.
Pure-Python tests (harvest, validate) run without Neo4j.
Server-route tests use FastAPI TestClient with monkeypatched session cache +
gate token, mirroring test_phase6hi_methodology_retrieval_and_playbook_wiring.py.

Capability map (25 items from capabilities.md):
  [validate-1]  _validate_phase_a rejects a plan whose ## Files line has no reason
  [validate-2]  _validate_phase_a passes a plan where every ## Files line is annotated
  [validate-3]  _validate_phase_a accepts legacy markdown-table Files rows
  [harvest-1]   harvest_plan extracts ## Analysis body as rationale
  [harvest-2]   harvest_plan extracts each annotated ## Files entry as {path,change_type,reason}
  [harvest-3]   harvest_plan captures a reasonless ## Files line as blank-reason claim
  [harvest-4]   harvest_plan extracts ONLY rule IDs cited in ## Rules Applied
  [harvest-5]   harvest_plan is robust against a ## Files literal inside a code fence
  [edge-1]      create_record_edge wires Project-[HAS_DECISION]->Decision
  [edge-2]      create_record_edge wires Decision-[GOVERNED_BY]->Rule
  [edge-3]      create_record_edge raises ValueError for unknown edge types
  [edge-4]      create_record_edge is idempotent (same edge twice -> one relationship)
  [capture-1]   capture_decision_at_approve creates Decision with OPEN planned_files claims
  [capture-2]   capture_decision_at_approve wires GOVERNED_BY only for cited+existing rules
  [capture-3]   capture_decision_at_approve wires HAS_DECISION from registered Project
  [capture-4]   capture_decision_at_approve calls ensure_project_registered before create_decision
  [capture-5]   capture_decision_at_approve returns None when cwd is in no git repo
  [e2e-1]       End-to-end: Cypher confirms Decision, open claims, HAS_DECISION, GOVERNED_BY
  [server-1]    Server REJECTS planning advance with reasonless plan; phase unchanged; token consumed
  [server-2]    Token is CONSUMED on rejection; same token re-post is refused; fresh token advances
  [server-3]    Empty project_root is HARD-REJECTED loudly; advance blocked; token consumed
  [server-4]    Valid plan advances AND captures a Decision (Neo4j-gated)
  [server-5]    Non-phase-a advance (testing->implementation) is unaffected by new validation
  [server-6]    FAIL-OPEN: capture raise -> advance still succeeds + decision_capture_failed logged
  [server-7]    FAIL-OPEN: _db is None -> advance succeeds; capture skipped without error

ENF-SYS-005 note: edge-1, edge-2, edge-4, capture-1 through capture-5, e2e-1, and server-4
require a real Neo4j connection to validate MERGE semantics and graph state.
Mock-only tests of those behaviors would prove nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable

import pytest
import pytest_asyncio
import httpx
from fastapi.testclient import TestClient

# ruff: noqa: F811 -- the shared client/isolated_cache fixtures below are consumed
# as test-method parameters, which ruff misreads as redefinitions of this import.
from tests.fixtures.server_routes import client, isolated_cache  # noqa: F401
from tests.fixtures.session_state import write_bound_gate_token
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection
from writ.server import app


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_SCOPE = "test-dm-1c"
_TEST_REPO_ROOT = "/tmp/fake-test-1c-repo"
_TEST_BIBLE_ROOT = "bible"

# A minimal valid plan.md text: every ## Files line has a reason.
_VALID_PLAN = """\
# Plan: test plan

## Files

- `writ/session/plan_harvest.py` (create) -- new harvest module

## Analysis

This is the rationale for the plan. It explains the why.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open capture

## Capabilities

- [ ] harvest_plan extracts rationale
"""

# A plan with a reasonless ## Files line.
_REASONLESS_PLAN = """\
# Plan: broken plan

## Files

- `writ/session/plan_harvest.py` (create)

## Analysis

Some rationale.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open capture

## Capabilities

- [ ] some capability
"""

# A plan whose ## Files uses the bold change-type shape:
# "- **change_type** `path` -- reason". Mirrors _VALID_PLAN's other sections
# (a real rule-id-shaped token in ## Rules Applied) so only the ## Files shape
# under test differs.
_BOLD_PLAN = """\
# Plan: bold change-type plan

## Files

- **create** `writ/session/plan_harvest.py` -- new harvest module

## Analysis

This is the rationale for the plan. It explains the why.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open capture

## Capabilities

- [ ] harvest_plan extracts rationale
"""

# Same as _BOLD_PLAN but the bold ## Files line has NO reason.
_BOLD_REASONLESS_PLAN = """\
# Plan: bold change-type plan, missing reason

## Files

- **create** `writ/session/plan_harvest.py`

## Analysis

This is the rationale for the plan. It explains the why.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open capture

## Capabilities

- [ ] harvest_plan extracts rationale
"""

# A plan whose ## Files uses legacy markdown-table row format.
_LEGACY_TABLE_PLAN = """\
# Plan: legacy plan

## Files

| `writ/session/plan_harvest.py` | create -- new harvest module |

## Analysis

Legacy rationale.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open

## Capabilities

- [ ] some capability
"""

# A plan with a ## Files literal inside a code fence (robustness test).
_CODE_FENCE_PLAN = """\
# Plan: fence plan

## Files

- `writ/session/plan_harvest.py` (create) -- new harvest module

## Analysis

Example of bad template:

```markdown
## Files

- `should-not-be-parsed` (modify) -- this is inside a fence
```

Real analysis text.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open
- **DOC-ARCH-001** -- adr recorded

## Capabilities

- [ ] fence robustness
"""

# A plan with rule IDs in Analysis and Files reasons but not in Rules Applied
# (harvest should NOT extract those).
_RULE_ID_NOISE_PLAN = """\
# Plan: noise plan

## Files

- `writ/session/plan_harvest.py` (create) -- satisfies ENF-POST-004 invariant

## Analysis

We apply ENF-POST-004 here and also TEST-COVERAGE-001 for coverage reasons.

## Rules Applied

- **ERR-HANDLE-001** -- fail-open capture

## Capabilities

- [ ] rule extraction only from Rules Applied
"""


def _plan_citing_rules(rules: list[tuple[str, str]]) -> str:
    """Build a minimal valid plan.md whose ## Rules Applied cites the given rules.

    Each (rule_id, note) becomes a `- **<rule_id>** -- <note>` line. Used so the
    capture tests cite UNIQUE fake rule ids (e.g. TEST1C-GOV-001) instead of the
    live writ ERR-HANDLE-001, whose duplication under the test scope would make
    get_rule.single() error.
    """
    applied = "\n".join(f"- **{rid}** -- {note}" for rid, note in rules)
    return (
        "# Plan: test plan\n\n"
        "## Files\n\n"
        "- `writ/session/plan_harvest.py` (create) -- new harvest module\n\n"
        "## Analysis\n\n"
        "This is the rationale for the plan. It explains the why.\n\n"
        "## Rules Applied\n\n"
        f"{applied}\n\n"
        "## Capabilities\n\n"
        "- [ ] harvest_plan extracts rationale\n"
    )


# ---------------------------------------------------------------------------
# Runner stub helpers (mirrored from test_decision_memory_identity.py:46-95)
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
    return _make_runner({
        "rev-parse": _completed(0, stdout=repo_root + "\n"),
        "get-url": _completed(0, stdout=remote_url + "\n"),
    })


# ---------------------------------------------------------------------------
# Neo4j-gated fixture -- scope "test-dm-1c", two-pass :Project teardown
# (mirrors test_decision_memory_identity.py:116-161; adjusted for 1c prefix
# and repo_root prefix /tmp/fake-test-1c-repo per spec).
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_clean():
    """Connect to Neo4j, wipe test-dm-1c project scope, yield, clean up.

    Skips when Neo4j is unreachable. Two-pass :Project teardown:
    1. By name prefix "test-dm-1c" -- catches nodes with explicit test names.
    2. By repo_root prefix "/tmp/fake-test-1c-repo" -- catches derived-name nodes.
    No production node uses /tmp/fake-test-1c-repo, so no leakage to live data.
    The live "writ" :Project is never touched.
    """
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")

    await _wipe_1c_test_data(conn)
    yield conn
    await _wipe_1c_test_data(conn)
    await conn.close()


async def _wipe_1c_test_data(conn: Neo4jConnection) -> None:
    """Wipe test-dm-1c project scope and all related :Project registry nodes."""
    await conn.clear_project(_TEST_SCOPE)
    async with conn._driver.session(database=conn._database) as s:
        # Pass 1: by name prefix.
        await (await s.run(
            "MATCH (p:Project) WHERE p.name STARTS WITH $prefix DETACH DELETE p",
            prefix=_TEST_SCOPE,
        )).consume()
        # Pass 2: by repo_root prefix (catches derived-name registrations).
        await (await s.run(
            "MATCH (p:Project) WHERE p.repo_root STARTS WITH $root_prefix DETACH DELETE p",
            root_prefix=_TEST_REPO_ROOT,
        )).consume()
        # Also wipe any Decision nodes with decision_id containing the scope.
        await (await s.run(
            "MATCH (d:Decision) WHERE d.project STARTS WITH $prefix DETACH DELETE d",
            prefix=_TEST_SCOPE,
        )).consume()
        # server-4 captures against a REAL git repo (no runner), so the Decision
        # is scoped under the git-derived name (e.g. github.com/org/srv4), not the
        # test scope. Wipe by session_id prefix too so those records do not leak.
        await (await s.run(
            "MATCH (d:Decision) WHERE d.session_id STARTS WITH $prefix DETACH DELETE d",
            prefix=_TEST_SCOPE,
        )).consume()
        # And wipe the :Project nodes the test git repos register (by the test
        # remote-url orgs used in this file).
        await (await s.run(
            "MATCH (p:Project) WHERE p.remote_url STARTS WITH 'git@github.com:org/' "
            "DETACH DELETE p",
        )).consume()
        # Wipe test-seeded Rule nodes (the fake TEST1C-* rules) so they never
        # collide with the live writ corpus on a later run.
        await (await s.run(
            "MATCH (r:Rule) WHERE r.rule_id STARTS WITH 'TEST1C' DETACH DELETE r",
        )).consume()


# ---------------------------------------------------------------------------
# Server-route fixtures (client, isolated_cache) are shared -- imported at the
# top of this file from tests/fixtures/server_routes.py.
# ---------------------------------------------------------------------------


def _write_gate_token(session_id: str, token: str) -> None:
    """Mint the bound gate token file (matching gate_token_path semantics).

    The token binds the gate it authorizes and the plan fingerprint it was given for, and
    the advance route claims through claim_gate_token with no unbound fallback, so a bare
    one-line secret is refused before the route reaches any gate logic. The binding is
    derived from the seeded session cache exactly as the production mint derives it, so
    every caller below keeps seeding the cache first and needs no other change.
    """
    write_bound_gate_token(session_id, token)


def _token_exists(session_id: str) -> bool:
    """Check whether the gate token file exists on disk (not consumed yet)."""
    path = os.path.join("/tmp", f"writ-gate-token-{session_id}")
    return os.path.exists(path)


def _advance_post(client: TestClient, sid: str, token: str,
                  project_root: str = "", extra: dict | None = None) -> dict:
    """POST /session/{sid}/advance-phase with the given token and project_root."""
    body: dict = {"confirmation_source": "tool", "token": token, "project_root": project_root}
    if extra:
        body.update(extra)
    resp = client.post(f"/session/{sid}/advance-phase", json=body)
    return resp.json()


def _seed_planning_phase(cache_dir: Path, session_id: str) -> None:
    """Write a session cache with current_phase=planning so there is something to advance."""
    cache_path = cache_dir / f"writ-session-{session_id}.json"
    data = {
        "current_phase": "planning",
        "mode": "work",
        "phase_transitions": [],
    }
    with open(cache_path, "w") as f:
        json.dump(data, f)


def _seed_testing_phase(cache_dir: Path, session_id: str) -> None:
    """Write a session cache at the testing gate (planning already approved).

    gates_approved=["phase-a"] reflects the realistic state: a session only reaches
    current_phase=testing after the phase-a gate is approved. The advance path is
    gates_approved-driven (mode_engine._next_pending_gate), so a testing-phase
    session without phase-a recorded would resolve the next gate as phase-a instead
    of test-skeletons.
    """
    cache_path = cache_dir / f"writ-session-{session_id}.json"
    data = {
        "current_phase": "testing",
        "mode": "work",
        "gates_approved": ["phase-a"],
        "phase_transitions": [],
    }
    with open(cache_path, "w") as f:
        json.dump(data, f)


def _read_phase(cache_dir: Path, session_id: str) -> str:
    """Read current_phase from the session cache file."""
    cache_path = cache_dir / f"writ-session-{session_id}.json"
    if not cache_path.exists():
        return "planning"
    with open(cache_path) as f:
        return json.load(f).get("current_phase", "planning")


def _read_friction_events(log_path: Path, event: str) -> list[dict]:
    """Parse friction log and return events matching the given event name."""
    if not log_path.exists():
        return []
    from writ.analysis.friction import parse_log
    return [e.model_dump() for e in parse_log(log_path) if e.event == event]


# ---------------------------------------------------------------------------
# Pure-Python: _validate_phase_a gate
# ---------------------------------------------------------------------------

class TestValidatePhaseAReasonGate:
    """Caps [validate-1], [validate-2], [validate-3]."""

    def test_rejects_reasonless_files_line(self, tmp_path: Path) -> None:
        # [validate-1]: a ## Files line with no reason must return an error string
        # naming the offending line. RED: _validate_phase_a does not yet check
        # per-file reasons.
        (tmp_path / "plan.md").write_text(_REASONLESS_PLAN)
        from writ.session.approval_workflow import _validate_phase_a
        error = _validate_phase_a(str(tmp_path))
        assert error is not None, (
            "_validate_phase_a must return an error string for a ## Files line "
            "with no reason"
        )
        assert "plan_harvest.py" in error or "## Files" in error, (
            "the error must name the offending line or section; got: " + repr(error)
        )

    def test_passes_fully_annotated_plan(self, tmp_path: Path) -> None:
        # [validate-2]: a plan where every ## Files line carries path (change_type) -- reason
        # must return None (no error).
        (tmp_path / "plan.md").write_text(_VALID_PLAN)
        from writ.session.approval_workflow import _validate_phase_a
        error = _validate_phase_a(str(tmp_path))
        assert error is None, (
            "_validate_phase_a must pass a fully-annotated plan; got: " + repr(error)
        )

    def test_accepts_legacy_markdown_table_row(self, tmp_path: Path) -> None:
        # [validate-3]: a legacy markdown-table Files row must not cause a false failure.
        # RED if the new reason-check regex also flags table rows.
        (tmp_path / "plan.md").write_text(_LEGACY_TABLE_PLAN)
        from writ.session.approval_workflow import _validate_phase_a
        error = _validate_phase_a(str(tmp_path))
        assert error is None, (
            "_validate_phase_a must not falsely reject legacy markdown-table Files rows; "
            "got: " + repr(error)
        )

    def test_passes_bold_change_type_files_line(self, tmp_path: Path) -> None:
        # [validate-bold-pass]: a bold-change-type ## Files line
        # (- **create** `path` -- reason) carries a reason and must pass with no
        # missing-reason error. Guard test: may already pass today (the bold
        # shape currently matches neither PATH_ONLY regex, so no flag is raised);
        # still asserted explicitly as one half of the parity contract.
        (tmp_path / "plan.md").write_text(_BOLD_PLAN)
        from writ.session.approval_workflow import _validate_phase_a
        error = _validate_phase_a(str(tmp_path))
        assert error is None, (
            "_validate_phase_a must pass a bold-change-type ## Files line that "
            "carries a reason; got: " + repr(error)
        )

    def test_rejects_bold_change_type_line_missing_reason(self, tmp_path: Path) -> None:
        # [validate-bold-missing-reason]: a bold ## Files line missing its reason
        # (- **create** `path`) must be flagged with the same "missing a reason"
        # message as the canonical shape. RED today: neither _FILES_LINE_RE nor
        # _FILES_PATH_ONLY_RE matches a bold-prefixed bullet (the first token
        # after '- ' is '**', not a backtick), so the gate silently passes it.
        (tmp_path / "plan.md").write_text(_BOLD_REASONLESS_PLAN)
        from writ.session.approval_workflow import _validate_phase_a
        error = _validate_phase_a(str(tmp_path))
        assert error is not None, (
            "_validate_phase_a must reject a bold ## Files line with no reason; got None"
        )
        assert "missing a reason" in error, (
            "the error must contain the 'missing a reason' message; got: " + repr(error)
        )


# ---------------------------------------------------------------------------
# Pure-Python: harvest_plan parser
# ---------------------------------------------------------------------------

class TestHarvestPlan:
    """Caps [harvest-1] through [harvest-5]."""

    def test_extracts_analysis_as_rationale(self) -> None:
        # [harvest-1]: harvest_plan must return the ## Analysis body as "rationale".
        # RED: module does not exist yet (ImportError).
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_VALID_PLAN)
        assert "rationale" in result, "harvest_plan must return a 'rationale' key"
        assert "rationale for the plan" in result["rationale"], (
            "rationale must contain the ## Analysis body text"
        )

    def test_extracts_files_entries(self) -> None:
        # [harvest-2]: each annotated ## Files entry comes back as {path, change_type, reason}.
        # RED: ImportError on the missing module.
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_VALID_PLAN)
        assert "files" in result, "harvest_plan must return a 'files' key"
        files = result["files"]
        assert len(files) == 1, f"expected 1 file entry, got {len(files)}: {files}"
        entry = files[0]
        assert entry["path"] == "writ/session/plan_harvest.py", (
            "path must be the backtick content without backticks"
        )
        assert entry["change_type"] == "create", "change_type must match the parenthesised token"
        assert entry["reason"] == "new harvest module", "reason must be the text after ' -- '"

    def test_captures_reasonless_line_as_blank_reason(self) -> None:
        # [harvest-3]: a reasonless ## Files line must NOT be dropped; it must produce
        # an entry with reason == "" (defensive fallback). RED: ImportError.
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_REASONLESS_PLAN)
        files = result.get("files", [])
        assert len(files) >= 1, (
            "harvest_plan must capture a reasonless ## Files line as a blank-reason "
            "entry rather than dropping it"
        )
        reasonless = [f for f in files if f["path"] == "writ/session/plan_harvest.py"]
        assert reasonless, "the reasonless file must appear in the files list"
        assert reasonless[0]["reason"] == "", (
            "a reasonless ## Files line must produce reason==''; got: " + repr(reasonless[0])
        )

    def test_extracts_only_rules_applied_ids(self) -> None:
        # [harvest-4]: rule IDs in ## Analysis or ## Files reasons must NOT be extracted;
        # only IDs in ## Rules Applied are returned. RED: ImportError.
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_RULE_ID_NOISE_PLAN)
        cited = result.get("cited_rules", [])
        # ERR-HANDLE-001 is in Rules Applied; ENF-POST-004 and TEST-COVERAGE-001 are noise.
        assert "ERR-HANDLE-001" in cited, (
            "ERR-HANDLE-001 is in ## Rules Applied and must be extracted"
        )
        assert "ENF-POST-004" not in cited, (
            "ENF-POST-004 appears in ## Analysis prose only, not in ## Rules Applied -- "
            "must NOT be extracted; got cited_rules=" + repr(cited)
        )
        assert "TEST-COVERAGE-001" not in cited, (
            "TEST-COVERAGE-001 appears in ## Analysis prose only -- must NOT be extracted"
        )

    def test_robust_against_code_fence_files_heading(self) -> None:
        # [harvest-5]: a ## Files literal inside a code fence must not be mis-targeted
        # as the section heading. The path inside the fence must NOT appear in files[].
        # RED: ImportError.
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_CODE_FENCE_PLAN)
        files = result.get("files", [])
        paths = [f["path"] for f in files]
        assert "should-not-be-parsed" not in paths, (
            "harvest_plan must not parse ## Files content inside a code fence; "
            "got paths=" + repr(paths)
        )
        assert "writ/session/plan_harvest.py" in paths, (
            "harvest_plan must still parse the real ## Files section"
        )

    def test_extracts_multiple_cited_rules_deduped(self) -> None:
        # Subsidiary of [harvest-4]: when ## Rules Applied lists multiple distinct IDs,
        # all are returned; duplicates are dropped. RED: ImportError.
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_CODE_FENCE_PLAN)
        cited = result.get("cited_rules", [])
        assert "ERR-HANDLE-001" in cited, "ERR-HANDLE-001 must be extracted from Rules Applied"
        assert "DOC-ARCH-001" in cited, "DOC-ARCH-001 must be extracted from Rules Applied"
        # No duplicates.
        assert len(cited) == len(set(cited)), "cited_rules must be deduplicated"

    def test_harvests_bold_change_type_files_line(self) -> None:
        # [harvest-bold]: harvest_plan must admit a bold-change-type ## Files
        # line with its path, change_type, and reason, and cited_rules must be
        # non-empty (from ## Rules Applied). RED today: _extract_files admits a
        # file only via _FILES_LINE_RE or _FILES_PATH_ONLY_RE, neither of which
        # matches a bold-prefixed bullet, so the entry is silently dropped.
        from writ.session.plan_harvest import harvest_plan
        result = harvest_plan(_BOLD_PLAN)
        files = result.get("files", [])
        matches = [f for f in files if f["path"] == "writ/session/plan_harvest.py"]
        assert matches, (
            "harvest_plan must admit the bold-change-type ## Files line; got files=" +
            repr(files)
        )
        entry = matches[0]
        assert entry["change_type"] == "create", (
            "change_type must be captured from the bold prefix; got: " + repr(entry)
        )
        assert entry["reason"] == "new harvest module", (
            "reason must be the text after ' -- '; got: " + repr(entry)
        )
        assert result.get("cited_rules"), (
            "cited_rules must be non-empty (## Rules Applied cites ERR-HANDLE-001); "
            "got: " + repr(result.get("cited_rules"))
        )


# ---------------------------------------------------------------------------
# Gate<->harvest parity invariant (ENF-POST-004)
# ---------------------------------------------------------------------------

class TestPlanFilesHarvestParity:
    """Caps [parity-bold] (ENF-POST-004): gate-passing implies harvestable."""

    def test_bold_plan_gate_pass_implies_harvestable(self, tmp_path: Path) -> None:
        # [parity-bold] ENF-POST-004: the SAME _BOLD_PLAN text that passes
        # _validate_phase_a must yield a non-empty harvest_plan(...)["files"].
        # RED today: the gate does not flag the bold shape (passes) AND the
        # harvester drops the bold entry (files == []) -- the exact
        # gate<->harvest parity gap this fix closes.
        (tmp_path / "plan.md").write_text(_BOLD_PLAN)
        from writ.session.approval_workflow import _validate_phase_a
        from writ.session.plan_harvest import harvest_plan

        gate_error = _validate_phase_a(str(tmp_path))
        assert gate_error is None, (
            "precondition: _BOLD_PLAN must pass the gate; got: " + repr(gate_error)
        )

        harvested = harvest_plan(_BOLD_PLAN)
        assert harvested.get("files"), (
            "ENF-POST-004 violated: a bold ## Files line that PASSES the gate must "
            "be harvestable (non-empty files); got: " + repr(harvested.get("files"))
        )


# ---------------------------------------------------------------------------
# Neo4j-gated: create_record_edge helpers
# ---------------------------------------------------------------------------

class TestCreateRecordEdge:
    """Caps [edge-1], [edge-2], [edge-3], [edge-4]."""

    @pytest.mark.asyncio
    async def test_wires_has_decision_project_to_decision(self, db_clean: Neo4jConnection) -> None:
        # [edge-1]: create_record_edge wires Project-[HAS_DECISION]->Decision,
        # matching Project by name (no project filter) and Decision by decision_id.
        # RED: create_record_edge does not exist yet (AttributeError).
        project_name = f"{_TEST_SCOPE}-edge1"
        decision_id = f"DEC-{_TEST_SCOPE}-edge1-planning"

        await db_clean.create_project(
            project_name, _TEST_REPO_ROOT + "/edge1", _TEST_BIBLE_ROOT
        )
        await db_clean.create_decision(
            decision_id=decision_id,
            project=_TEST_SCOPE,
            title="edge1 test",
            rationale="rationale",
            planned_files=[],
            governing_rule_ids=[],
            phase="planning",
            session_id="edge1-session",
            ts="2026-01-01T00:00:00",
        )

        await db_clean.create_record_edge(
            "HAS_DECISION",
            src_label="Project",
            src_id_field="name",
            src_id=project_name,
            tgt_label="Decision",
            tgt_id_field="decision_id",
            tgt_id=decision_id,
            project=_TEST_SCOPE,
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (p:Project {name: $name})-[e:HAS_DECISION]->(d:Decision {decision_id: $did}) "
                "RETURN count(e) AS cnt",
                name=project_name, did=decision_id,
            )
            record = await result.single()
        assert record["cnt"] == 1, (
            "create_record_edge must create exactly one HAS_DECISION edge; got count=" +
            str(record["cnt"])
        )

    @pytest.mark.asyncio
    async def test_wires_governed_by_decision_to_rule(self, db_clean: Neo4jConnection) -> None:
        # [edge-2]: create_record_edge wires Decision-[GOVERNED_BY]->Rule,
        # matching Decision by decision_id and Rule by rule_id.
        # RED: create_record_edge does not exist yet.
        decision_id = f"DEC-{_TEST_SCOPE}-edge2-planning"
        rule_id = "TEST1C-EDGE-001"

        await db_clean.create_decision(
            decision_id=decision_id,
            project=_TEST_SCOPE,
            title="edge2 test",
            rationale="rationale",
            planned_files=[],
            governing_rule_ids=[rule_id],
            phase="planning",
            session_id="edge2-session",
            ts="2026-01-01T00:00:00",
        )
        await db_clean.create_rule(
            {"rule_id": rule_id, "project": _TEST_SCOPE, "title": "fail-open",
             "body": "body text", "domain": "test", "authority": "mandatory",
             "mandatory": False, "always_on": False}
        )

        await db_clean.create_record_edge(
            "GOVERNED_BY",
            src_label="Decision",
            src_id_field="decision_id",
            src_id=decision_id,
            tgt_label="Rule",
            tgt_id_field="rule_id",
            tgt_id=rule_id,
            project=_TEST_SCOPE,
        )

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (d:Decision {decision_id: $did})-[e:GOVERNED_BY]->(r:Rule {rule_id: $rid}) "
                "RETURN count(e) AS cnt",
                did=decision_id, rid=rule_id,
            )
            record = await result.single()
        assert record["cnt"] == 1, (
            "create_record_edge must create exactly one GOVERNED_BY edge"
        )

    @pytest.mark.asyncio
    async def test_raises_value_error_for_unknown_edge_type(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [edge-3]: an edge type not in ALLOWED_EDGE_TYPES must raise ValueError.
        # RED: create_record_edge does not exist yet.
        with pytest.raises(ValueError, match="[Uu]nknown edge type|not.*ALLOWED"):
            await db_clean.create_record_edge(
                "COMPLETELY_FAKE_EDGE",
                src_label="Project",
                src_id_field="name",
                src_id="any",
                tgt_label="Decision",
                tgt_id_field="decision_id",
                tgt_id="any",
                project=_TEST_SCOPE,
            )

    @pytest.mark.asyncio
    async def test_raises_value_error_for_unknown_endpoint(
        self, db_clean: Neo4jConnection
    ) -> None:
        # [edge-5]: an endpoint whose (label, id_field) is not in the allowlist must
        # raise ValueError before any Cypher is built (SEC-INJ: labels/fields are
        # interpolated, so an unknown label or a mismatched id_field is rejected).
        with pytest.raises(ValueError, match="[Uu]nknown record-edge endpoint"):
            await db_clean.create_record_edge(
                "HAS_DECISION",
                src_label="Project",
                src_id_field="name",
                src_id="any",
                tgt_label="Decision",
                tgt_id_field="not_a_field",  # mismatched id-field for Decision
                tgt_id="any",
                project=_TEST_SCOPE,
            )

    @pytest.mark.asyncio
    async def test_idempotent_same_edge_twice(self, db_clean: Neo4jConnection) -> None:
        # [edge-4]: wiring the same edge twice must produce exactly one relationship.
        # RED: create_record_edge does not exist yet.
        project_name = f"{_TEST_SCOPE}-idempotent"
        decision_id = f"DEC-{_TEST_SCOPE}-idempotent-planning"

        await db_clean.create_project(
            project_name, _TEST_REPO_ROOT + "/idempotent", _TEST_BIBLE_ROOT
        )
        await db_clean.create_decision(
            decision_id=decision_id,
            project=_TEST_SCOPE,
            title="idempotent test",
            rationale="rationale",
            planned_files=[],
            governing_rule_ids=[],
            phase="planning",
            session_id="idempotent-session",
            ts="2026-01-01T00:00:00",
        )
        edge_kwargs = dict(
            src_label="Project",
            src_id_field="name",
            src_id=project_name,
            tgt_label="Decision",
            tgt_id_field="decision_id",
            tgt_id=decision_id,
            project=_TEST_SCOPE,
        )
        # Wire twice.
        await db_clean.create_record_edge("HAS_DECISION", **edge_kwargs)
        await db_clean.create_record_edge("HAS_DECISION", **edge_kwargs)

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (p:Project {name: $name})-[e:HAS_DECISION]->(d:Decision {decision_id: $did}) "
                "RETURN count(e) AS cnt",
                name=project_name, did=decision_id,
            )
            record = await result.single()
        assert record["cnt"] == 1, (
            "create_record_edge must be idempotent: same edge twice must yield exactly one "
            "relationship, got count=" + str(record["cnt"])
        )


# ---------------------------------------------------------------------------
# Neo4j-gated: capture_decision_at_approve
# ---------------------------------------------------------------------------

class TestCaptureDecisionAtApprove:
    """Caps [capture-1] through [capture-5] and [e2e-1]."""

    @pytest.mark.asyncio
    async def test_creates_open_planned_files_claims(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [capture-1]: capture_decision_at_approve creates a Decision whose planned_files
        # are OPEN claims (resolved==False) for every planned file in the plan.
        # RED: capture module does not exist yet (ImportError).
        from writ.session.decision_capture import capture_decision_at_approve

        (tmp_path / "plan.md").write_text(_VALID_PLAN)
        repo_root = _TEST_REPO_ROOT + "/capture1"
        runner = _runner_with_remote(repo_root, "git@github.com:org/cap1.git")
        sid = f"{_TEST_SCOPE}-capture1-{uuid.uuid4().hex[:6]}"

        decision_id = await capture_decision_at_approve(
            db_clean, str(tmp_path), sid, "planning",
            cwd=repo_root, runner=runner,
        )
        assert decision_id is not None, "capture must return a decision_id for a valid plan"

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (d:Decision {decision_id: $did}) RETURN d.planned_files AS pf",
                did=decision_id,
            )
            record = await result.single()
        assert record is not None, "Decision node must exist after capture"

        import json as _json
        pf = record["pf"]
        if isinstance(pf, str):
            pf = _json.loads(pf)
        assert isinstance(pf, list) and len(pf) >= 1, (
            "planned_files must be a non-empty list; got: " + repr(pf)
        )
        for claim in pf:
            if isinstance(claim, str):
                claim = _json.loads(claim)
            assert claim.get("resolved") is False, (
                "every planned_files claim must have resolved==False after capture; "
                "got: " + repr(claim)
            )

    @pytest.mark.asyncio
    async def test_wires_governed_by_only_for_existing_rules(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [capture-2]: GOVERNED_BY is wired only for cited rules that actually exist in
        # the graph. A cited-but-nonexistent rule is skipped (no dangling edge).
        # RED: ImportError.
        from writ.session.decision_capture import capture_decision_at_approve

        # TEST1C-GOV-001 is seeded below (exists); NONEXISTENT-999 is not seeded.
        # A UNIQUE fake rule_id avoids colliding with the live writ ERR-HANDLE-001
        # (which would make get_rule.single() error on a duplicated rule_id).
        plan_text = _plan_citing_rules([
            ("TEST1C-GOV-001", "seeded fake rule"),
            ("NONEXISTENT-999", "fake rule"),
        ])
        (tmp_path / "plan.md").write_text(plan_text)

        # Pre-create the real (test-scoped) rule.
        await db_clean.create_rule(
            {"rule_id": "TEST1C-GOV-001", "project": _TEST_SCOPE,
             "title": "fail-open", "body": "body", "domain": "test",
             "authority": "mandatory", "mandatory": False, "always_on": False}
        )

        repo_root = _TEST_REPO_ROOT + "/capture2"
        runner = _runner_with_remote(repo_root, "git@github.com:org/cap2.git")
        sid = f"{_TEST_SCOPE}-capture2-{uuid.uuid4().hex[:6]}"
        decision_id = await capture_decision_at_approve(
            db_clean, str(tmp_path), sid, "planning",
            cwd=repo_root, runner=runner,
        )
        assert decision_id is not None

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (d:Decision {decision_id: $did})-[e:GOVERNED_BY]->(r) "
                "RETURN collect(r.rule_id) AS rule_ids",
                did=decision_id,
            )
            record = await result.single()
        rule_ids = record["rule_ids"] if record else []
        assert "TEST1C-GOV-001" in rule_ids, (
            "GOVERNED_BY edge must exist for the cited rule that exists in the graph"
        )
        assert "NONEXISTENT-999" not in rule_ids, (
            "GOVERNED_BY must NOT be wired for a cited rule that does not exist in the graph"
        )

    @pytest.mark.asyncio
    async def test_wires_has_decision_from_project(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [capture-3]: capture_decision_at_approve wires HAS_DECISION from the registered
        # Project to the created Decision. RED: ImportError.
        from writ.session.decision_capture import capture_decision_at_approve

        (tmp_path / "plan.md").write_text(_VALID_PLAN)
        repo_root = _TEST_REPO_ROOT + "/capture3"
        runner = _runner_with_remote(repo_root, "git@github.com:org/cap3.git")
        sid = f"{_TEST_SCOPE}-capture3-{uuid.uuid4().hex[:6]}"
        decision_id = await capture_decision_at_approve(
            db_clean, str(tmp_path), sid, "planning",
            cwd=repo_root, runner=runner,
        )
        assert decision_id is not None

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (p:Project)-[e:HAS_DECISION]->(d:Decision {decision_id: $did}) "
                "RETURN count(e) AS cnt, p.name AS project_name",
                did=decision_id,
            )
            record = await result.single()
        assert record is not None and record["cnt"] >= 1, (
            "a HAS_DECISION edge from a :Project to the created Decision must exist"
        )

    @pytest.mark.asyncio
    async def test_ensure_project_registered_called_before_create_decision(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [capture-4]: ensure_project_registered is called BEFORE create_decision, so the
        # Decision is scoped under the derived project name, never the bare "writ" fallback.
        # Verify: the Decision's project property matches the :Project node's name, not "writ".
        # RED: ImportError.
        from writ.session.decision_capture import capture_decision_at_approve

        (tmp_path / "plan.md").write_text(_VALID_PLAN)
        repo_root = _TEST_REPO_ROOT + "/capture4"
        runner = _runner_with_remote(repo_root, "git@github.com:org/cap4.git")
        sid = f"{_TEST_SCOPE}-capture4-{uuid.uuid4().hex[:6]}"
        decision_id = await capture_decision_at_approve(
            db_clean, str(tmp_path), sid, "planning",
            cwd=repo_root, runner=runner,
        )
        assert decision_id is not None

        async with db_clean._driver.session(database=db_clean._database) as s:
            result = await s.run(
                "MATCH (d:Decision {decision_id: $did}) RETURN d.project AS proj",
                did=decision_id,
            )
            record = await result.single()
        project_prop = record["proj"] if record else None
        assert project_prop != "writ", (
            "Decision.project must be the derived project name, not the bare 'writ' fallback; "
            "got: " + repr(project_prop)
        )
        assert project_prop is not None and len(project_prop) > 0, (
            "Decision.project must be a non-empty derived name"
        )

    @pytest.mark.asyncio
    async def test_returns_none_when_cwd_not_in_git_repo(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [capture-5]: when cwd is not inside any git repo, capture_decision_at_approve
        # must return None and not create any Decision node.
        # RED: ImportError.
        from writ.session.decision_capture import capture_decision_at_approve

        (tmp_path / "plan.md").write_text(_VALID_PLAN)
        sid = f"{_TEST_SCOPE}-capture5-{uuid.uuid4().hex[:6]}"

        # Use a tmp_path cwd that is genuinely not a git repo.
        no_repo_cwd = str(tmp_path / "not-a-repo")
        os.makedirs(no_repo_cwd, exist_ok=True)

        result = await capture_decision_at_approve(
            db_clean, str(tmp_path), sid, "planning",
            cwd=no_repo_cwd, runner=_runner_no_repo(),
        )
        assert result is None, (
            "capture_decision_at_approve must return None when cwd is not in a git repo; "
            "got: " + repr(result)
        )

    @pytest.mark.asyncio
    async def test_end_to_end_cypher_confirms_full_graph_state(
        self, db_clean: Neo4jConnection, tmp_path: Path
    ) -> None:
        # [e2e-1]: after capture, Cypher confirms:
        # - the Decision node exists with planned_files as open claims
        # - a HAS_DECISION edge from :Project to :Decision
        # - a GOVERNED_BY edge per cited-and-existing rule
        # RED: ImportError or missing methods.
        from writ.session.decision_capture import capture_decision_at_approve

        # Pre-create the cited rule under the test scope with a UNIQUE fake id so
        # it does not collide with the live writ corpus (a duplicated rule_id makes
        # get_rule.single() error).
        await db_clean.create_rule(
            {"rule_id": "TEST1C-GOV-001", "project": _TEST_SCOPE,
             "title": "fail-open", "body": "body", "domain": "test",
             "authority": "mandatory", "mandatory": False, "always_on": False}
        )

        (tmp_path / "plan.md").write_text(
            _plan_citing_rules([("TEST1C-GOV-001", "seeded fake rule")])
        )
        repo_root = _TEST_REPO_ROOT + "/e2e"
        runner = _runner_with_remote(repo_root, "git@github.com:org/e2e.git")
        sid = f"{_TEST_SCOPE}-e2e-{uuid.uuid4().hex[:6]}"
        decision_id = await capture_decision_at_approve(
            db_clean, str(tmp_path), sid, "planning",
            cwd=repo_root, runner=runner,
        )
        assert decision_id is not None, "end-to-end capture must return a decision_id"

        async with db_clean._driver.session(database=db_clean._database) as s:
            # Decision exists.
            dec_result = await s.run(
                "MATCH (d:Decision {decision_id: $did}) RETURN d.planned_files AS pf",
                did=decision_id,
            )
            dec_rec = await dec_result.single()
            assert dec_rec is not None, "Decision node must exist after end-to-end capture"

            # HAS_DECISION edge.
            has_dec = await s.run(
                "MATCH (p:Project)-[:HAS_DECISION]->(d:Decision {decision_id: $did}) "
                "RETURN count(p) AS cnt",
                did=decision_id,
            )
            has_dec_rec = await has_dec.single()
            assert has_dec_rec["cnt"] >= 1, "HAS_DECISION edge from :Project must exist"

            # GOVERNED_BY edge for cited+existing rule.
            gov_result = await s.run(
                "MATCH (d:Decision {decision_id: $did})-[:GOVERNED_BY]->(r:Rule) "
                "RETURN collect(r.rule_id) AS rule_ids",
                did=decision_id,
            )
            gov_rec = await gov_result.single()
            assert "TEST1C-GOV-001" in (gov_rec["rule_ids"] if gov_rec else []), (
                "GOVERNED_BY edge to TEST1C-GOV-001 must exist"
            )

        # Planned files are open claims.
        import json as _json
        pf = dec_rec["pf"]
        if isinstance(pf, str):
            pf = _json.loads(pf)
        assert all(
            (c if isinstance(c, dict) else _json.loads(c)).get("resolved") is False
            for c in pf
        ), "all planned_files claims must be open (resolved==False) after capture"


# ---------------------------------------------------------------------------
# Server-route tests
# ---------------------------------------------------------------------------

class TestServerRouteGateAndCapture:
    """Caps [server-1] through [server-7]."""

    def test_rejects_reasonless_plan_returns_error_and_does_not_advance(
        self, client: TestClient, isolated_cache: Path
    ) -> None:
        # [server-1]: a planning->testing advance with a reasonless ## Files plan must return
        # {"advanced": false, "error": ...}, not change the cache phase, and consume the token.
        # RED: session_advance_phase does not yet validate phase-a.
        cache_dir = isolated_cache / "writ-cache"
        sid = f"{_TEST_SCOPE}-srv1-{uuid.uuid4().hex[:6]}"
        _seed_planning_phase(cache_dir, sid)

        # Write plan.md with a reasonless ## Files line.
        plan_dir = isolated_cache / "plan-dir-srv1"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text(_REASONLESS_PLAN)

        token = uuid.uuid4().hex
        _write_gate_token(sid, token)

        result = _advance_post(client, sid, token, project_root=str(plan_dir))

        assert result.get("advanced") is False or "error" in result, (
            "a reasonless plan must produce an error response; got: " + repr(result)
        )
        assert "error" in result, (
            "response must include an 'error' key for the validation failure"
        )
        phase_after = _read_phase(cache_dir, sid)
        assert phase_after == "planning", (
            "cache phase must remain 'planning' after a validation rejection; "
            "got: " + repr(phase_after)
        )
        assert not _token_exists(sid), (
            "gate token must be CONSUMED on a validation rejection (no token reuse)"
        )

    def test_rejected_token_is_consumed_same_token_repost_refused(
        self, client: TestClient, isolated_cache: Path
    ) -> None:
        # [server-2]: after a validation rejection, re-posting with the SAME token must be
        # refused at the token check. Only a fresh token + fixed plan advances.
        # RED: server does not yet validate or consume-on-rejection.
        cache_dir = isolated_cache / "writ-cache"
        sid = f"{_TEST_SCOPE}-srv2-{uuid.uuid4().hex[:6]}"
        _seed_planning_phase(cache_dir, sid)

        plan_dir = isolated_cache / "plan-dir-srv2"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text(_REASONLESS_PLAN)

        token = uuid.uuid4().hex
        _write_gate_token(sid, token)

        # First post: rejected (token consumed).
        first = _advance_post(client, sid, token, project_root=str(plan_dir))
        assert "error" in first, "first post with reasonless plan must be rejected"

        # Re-post with the SAME token: must be refused (token is gone).
        # Write a fixed plan so the phase-a gate would pass -- only the token check
        # should refuse at this point.
        (plan_dir / "plan.md").write_text(_VALID_PLAN)
        second = _advance_post(client, sid, token, project_root=str(plan_dir))
        assert second.get("advanced") is False or "error" in second, (
            "re-posting with the SAME token after a rejection must be refused; got: " +
            repr(second)
        )
        assert "token" in json.dumps(second).lower(), (
            "the refusal of the stale token must mention 'token'"
        )

        # With a FRESH token, the valid plan advances.
        fresh_token = uuid.uuid4().hex
        _write_gate_token(sid, fresh_token)
        third = _advance_post(client, sid, fresh_token, project_root=str(plan_dir))
        assert third.get("phase") == "testing" or "phase" in third, (
            "a fresh token + valid plan must advance; got: " + repr(third)
        )

    def test_empty_project_root_hard_rejected_loud_error(
        self, client: TestClient, isolated_cache: Path
    ) -> None:
        # [server-3]: a planning advance with an EMPTY project_root must return
        # {"advanced": false, "error": <project-root msg>, "gate": "phase-a"} and must
        # NOT advance.
        #
        # Token policy CHANGED: the token is now KEPT, not consumed. Spending it here
        # punished the human for an infrastructure failure they cannot fix by editing an
        # artifact -- and every retry burned a fresh approval. Spend-on-rejection still
        # holds where the gate actually JUDGED an artifact and it failed (see
        # test_advance_gate_validation_parity.py::test_rejection_spends_the_token).
        cache_dir = isolated_cache / "writ-cache"
        sid = f"{_TEST_SCOPE}-srv3-{uuid.uuid4().hex[:6]}"
        _seed_planning_phase(cache_dir, sid)

        token = uuid.uuid4().hex
        _write_gate_token(sid, token)

        result = _advance_post(client, sid, token, project_root="")

        assert result.get("advanced") is False or "error" in result, (
            "empty project_root must produce an error response; got: " + repr(result)
        )
        assert "error" in result, "response must include 'error' key"
        assert result.get("gate") == "phase-a", (
            "error response must include 'gate': 'phase-a'; got: " + repr(result)
        )
        # The error message must name project-root failure.
        assert "project" in result["error"].lower() or "root" in result["error"].lower(), (
            "error message must describe the project-root failure; got: " + repr(result["error"])
        )
        phase_after = _read_phase(cache_dir, sid)
        assert phase_after == "planning", (
            "cache phase must remain 'planning' after hard-reject; got: " + repr(phase_after)
        )
        assert result.get("token_spent") is False, (
            "an unresolvable root is an infra failure, not a judged artifact; got: "
            + repr(result)
        )
        assert _token_exists(sid), (
            "gate token must be KEPT on empty-project_root hard-reject so the user can "
            "retry with a root instead of re-approving"
        )

    @pytest.mark.asyncio
    async def test_valid_plan_advances_and_captures_decision(
        self, isolated_cache: Path, db_clean: Neo4jConnection
    ) -> None:
        # [server-4]: a planning advance with a valid plan advances (phase->testing)
        # AND triggers capture (Decision exists in Neo4j). Neo4j-gated.
        # RED: server does not yet call capture_decision_at_approve.
        # Uses httpx.AsyncClient + ASGITransport so this async test can both hold
        # the async db_clean fixture and await the Neo4j check directly.
        import writ.server as _srv
        cache_dir = isolated_cache / "writ-cache"
        sid = f"{_TEST_SCOPE}-srv4-{uuid.uuid4().hex[:6]}"
        _seed_planning_phase(cache_dir, sid)

        plan_dir = isolated_cache / "plan-dir-srv4"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text(_VALID_PLAN)

        # The HTTP route does NOT thread a runner, so capture runs REAL git against
        # project_root. Make plan_dir a real git repo with a remote so
        # ensure_project_registered (rev-parse + remote get-url) resolves a derived
        # project name instead of returning None. Mirrors the git-init tmp-repo
        # pattern in test_read_junk_gate.py.
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            pytest.skip("git not available")
        subprocess.run(["git", "init", str(plan_dir)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(plan_dir), "remote", "add", "origin",
             "git@github.com:org/srv4.git"],
            capture_output=True, check=True,
        )

        # Monkeypatch _db so the server route has a real connection.
        original_db = _srv._db
        _srv._db = db_clean
        try:
            token = uuid.uuid4().hex
            _write_gate_token(sid, token)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    f"/session/{sid}/advance-phase",
                    json={
                        "confirmation_source": "tool",
                        "token": token,
                        "project_root": str(plan_dir),
                    },
                )
            result = resp.json()
        finally:
            _srv._db = original_db

        assert result.get("phase") == "testing", (
            "a valid plan must advance to 'testing'; got: " + repr(result)
        )

        # The Decision must exist in Neo4j (awaited directly, no run_until_complete).
        async with db_clean._driver.session(database=db_clean._database) as s:
            r = await s.run(
                "MATCH (d:Decision) WHERE d.session_id = $sid RETURN count(d) AS cnt",
                sid=sid,
            )
            rec = await r.single()
        found = rec["cnt"] >= 1 if rec else False
        assert found, (
            "after a valid planning advance, a Decision node must exist in Neo4j "
            "scoped to this session"
        )

    def test_non_phase_a_advance_unaffected(
        self, client: TestClient, isolated_cache: Path
    ) -> None:
        # [server-5]: a testing->implementation advance must NOT be gated on phase-a
        # validation (no plan.md check) and must advance exactly as before.
        # RED if the new server-side validation is too broad and gates non-planning advances.
        cache_dir = isolated_cache / "writ-cache"
        sid = f"{_TEST_SCOPE}-srv5-{uuid.uuid4().hex[:6]}"
        _seed_testing_phase(cache_dir, sid)

        token = uuid.uuid4().hex
        _write_gate_token(sid, token)
        # A root holding a test skeleton but NO plan.md: the route now runs the TARGET
        # gate's validator (test-skeletons here), which it previously skipped entirely on
        # this path, and this test's point stands -- a non-planning advance must never
        # look for a plan.md.
        skel_root = isolated_cache / "srv5-proj"
        (skel_root / "tests").mkdir(parents=True)
        (skel_root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
        assert not (skel_root / "plan.md").exists()
        result = _advance_post(client, sid, token, project_root=str(skel_root))

        # The advance must succeed: testing -> implementation.
        assert result.get("phase") == "implementation", (
            "a testing->implementation advance must succeed without a plan.md check; "
            "got: " + repr(result)
        )

    def test_fail_open_capture_raise_advance_succeeds_friction_logged(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [server-6]: when capture_decision_at_approve raises, the advance still succeeds
        # and a decision_capture_failed friction event is logged.
        # RED: server does not yet call capture and does not log the event.
        import writ.server as _srv

        cache_dir = isolated_cache / "writ-cache"
        log_path = isolated_cache / "workflow-friction.log"
        sid = f"{_TEST_SCOPE}-srv6-{uuid.uuid4().hex[:6]}"
        _seed_planning_phase(cache_dir, sid)

        plan_dir = isolated_cache / "plan-dir-srv6"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text(_VALID_PLAN)

        # Monkeypatch _db with a sentinel (non-None) so the capture branch is entered.
        # Then monkeypatch capture_decision_at_approve to raise inside it.
        class _FakeDb:
            pass

        original_db = _srv._db
        _srv._db = _FakeDb()

        async def _raise_capture(*args, **kwargs):
            raise RuntimeError("simulated capture failure")

        # Patch at the server's USE site (the name bound in writ.server's namespace
        # via `from writ.session.decision_capture import capture_decision_at_approve`).
        # Patching the definition site would not intercept the already-imported reference
        # the route calls, so post-impl the real capture would run and the test would
        # pass for the wrong reason. The try/except handles the pre-impl RED state where
        # writ.server does not yet import the name.
        try:
            monkeypatch.setattr(
                "writ.server.capture_decision_at_approve",
                _raise_capture,
            )
        except AttributeError:
            # writ.server does not yet import capture_decision_at_approve ->
            # test will RED at the friction-event assertion below.
            pass


        try:
            token = uuid.uuid4().hex
            _write_gate_token(sid, token)
            result = _advance_post(client, sid, token, project_root=str(plan_dir))
        finally:
            _srv._db = original_db

        # The advance must succeed even when capture raised.
        assert result.get("phase") == "testing", (
            "FAIL-OPEN: advance must succeed even when capture raises; got: " + repr(result)
        )

        # A decision_capture_failed friction event must have been logged.
        events = _read_friction_events(log_path, "decision_capture_failed")
        assert len(events) >= 1, (
            "FAIL-OPEN: a decision_capture_failed friction event must be logged when "
            "capture raises; got events: " + repr(events)
        )

    def test_fail_open_db_none_advance_succeeds_no_error(
        self, client: TestClient, isolated_cache: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [server-7]: when _db is None, the advance succeeds and capture is skipped
        # without raising. RED: server does not yet have this guard.
        import writ.server as _srv

        cache_dir = isolated_cache / "writ-cache"
        sid = f"{_TEST_SCOPE}-srv7-{uuid.uuid4().hex[:6]}"
        _seed_planning_phase(cache_dir, sid)

        plan_dir = isolated_cache / "plan-dir-srv7"
        plan_dir.mkdir()
        (plan_dir / "plan.md").write_text(_VALID_PLAN)

        original_db = _srv._db
        _srv._db = None
        try:
            token = uuid.uuid4().hex
            _write_gate_token(sid, token)
            result = _advance_post(client, sid, token, project_root=str(plan_dir))
        finally:
            _srv._db = original_db

        # Must advance: _db is None -> skip capture, not block.
        assert result.get("phase") == "testing", (
            "FAIL-OPEN: when _db is None the advance must still succeed; got: " + repr(result)
        )
        # No error key.
        assert "error" not in result, (
            "FAIL-OPEN: _db is None must not add an error key; got: " + repr(result)
        )
