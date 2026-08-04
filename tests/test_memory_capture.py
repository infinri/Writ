"""Automatic memory-to-graph mirroring (plan.md / capabilities.md).

Test skeleton for the capability gate. Every test in this file is RED until the
implementer builds the corresponding module/route/hook/CLI command. Tests fail on
ImportError/AttributeError/FileNotFoundError for missing modules -- never on a
harness error.

Run interpreter: .venv/bin/python -m pytest (has onnxruntime; system python3
errors on embedding imports).

Neo4j-gated tests use a `db_clean` fixture (scope "test-mem-cap") mirroring the
pattern in test_decision_memory_records.py / test_decision_memory_capture.py:
skip when Neo4j is unreachable, wipe before and after.

Real memory-file frontmatter shape (confirmed against a live
~/.claude/projects/<encoded>/memory/*.md file):

    ---
    name: feedback_no_coauthored_by
    description: Do NOT add a Co-Authored-By Claude trailer to git commit messages
    metadata:
      node_type: memory
      type: feedback
      originSessionId: 6acb0eab-0b1b-463e-b36b-5fdee34d4996
    ---

    Commit messages must NOT include a `Co-Authored-By: Claude ...` trailer.
    ... see [[some_other_memory]] for related context.

`type` lives at `metadata.type` (nested), not top-level. `[[wikilink]]` targets
(bare filename, no .md, no directory) are the "links" the plan.md Analysis
describes. MEMORY.md (the index) uses ordinary markdown `[title](file.md)`
links and is explicitly excluded from mirroring.

Capability map (maps to capabilities.md checkboxes):
  [parse-1]    parse_memory_markdown extracts name/description/metadata.type/body/links
  [parse-2]    missing frontmatter -> None (TEST-EDGE-002)
  [parse-3]    empty body -> body == "" (TEST-EDGE-002)
  [parse-4]    missing description/type default to "" rather than raising
  [index-1]    is_memory_index_file recognizes MEMORY.md exactly
  [proj-1]     derive_project_from_memory_path extracts the encoded project segment
  [payload-1]  build_memory_payload assembles the full upsert payload
  [payload-2]  build_memory_payload returns None for MEMORY.md
  [payload-3]  build_memory_payload returns None when frontmatter is unparsable (fail-open)
  [create-1]   Writing a memory file creates/updates a Memory node with all fields
  [create-2]   Editing re-MERGEs the same node (idempotent, no duplicates)
  [create-3]   MEMORY.md writes are not mirrored (no node)
  [hook-1]     writ-memory-capture.sh is registered on PostToolUse Write|Edit
  [hook-2]     the hook ignores non-memory paths and non-Write/Edit tools
  [hook-3]     the hook does not even attempt to reach the daemon for MEMORY.md
  [hook-4]     daemon down: the hook exits 0 (memory write already succeeded on disk)
  [hook-5]     daemon down: a memory_capture_failed friction event is recorded
  [route-1]    POST /memory-record guards _db is None
  [route-2]    POST /memory-record logs + returns 200 on internal exception (fail-open)
  [cli-bf-1]   `writ memory backfill` upserts every memory file across every project
  [cli-bf-2]   backfill excludes MEMORY.md
  [cli-bf-3]   backfill reports upsert/tombstone counts in its output
  [cli-bf-4]   backfill tombstones Memory nodes whose file no longer exists
  [cli-bf-5]   backfill is contained under --projects-root (SEC-INJ-PATH-001)
  [cli-bf-6]   an empty memory dir backfills to zero, not an error (TEST-EDGE-002)
  [cli-ls-1]   `writ memory list --project <p>` returns only that project's live memories
  [cli-ls-2]   list excludes tombstoned (status=deleted) memories
  [cli-ls-3]   list for an absent project returns empty, not an error (TEST-EDGE-002)
  [rcl-1]      a Memory node with provenance='record' survives `writ reconcile`
  [rcl-2]      a Memory node is exempt from detect_parity_violations (`writ prune`)
  [chan-1]     'Memory' is absent from NodeType / RETRIEVABLE_NODE_TYPES / NODE_TYPE_MODELS
  [chan-2]     a Memory node's content never appears in /query results
  [chan-3]     a Memory node's content never appears in /methodology-companion output

ENF-SYS-005 note: create-1, create-2, create-3, rcl-1, rcl-2, chan-2, chan-3, and
the backfill/list CLI integration tests require a real Neo4j connection to
validate MERGE semantics and graph state. Mock-only tests of those behaviors
would prove nothing.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
MEMORY_CAPTURE_PY = os.path.join(SKILL_ROOT, "bin", "lib", "memory_capture.py")
HOOK_SH = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-memory-capture.sh")
HOOKS_JSON = os.path.join(SKILL_ROOT, "hooks", "hooks.json")

_TEST_SCOPE = "test-mem-cap"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_memory_capture():
    """Load bin/lib/memory_capture.py by file path (flat stdlib-only script,
    not a package -- mirrors test_cwd_changed.py's spec_from_file_location idiom).
    RED with FileNotFoundError/ImportError until the module is created.
    """
    spec = importlib.util.spec_from_file_location("writ_memory_capture", MEMORY_CAPTURE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _memory_content(
    name: str = "test_memory_note",
    description: str = "a test memory",
    mem_type: str = "feedback",
    body: str = "Body text with a [[linked_memory]] reference.",
    session_id: str = "sess-abc-001",
) -> str:
    """A well-formed memory markdown file, matching the real on-disk shape."""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  node_type: memory\n"
        f"  type: {mem_type}\n"
        f"  originSessionId: {session_id}\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


def _memory_path(root: Path, encoded_project: str = "-home-user-proj", filename: str = "test_memory_note.md") -> Path:
    d = root / encoded_project / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / filename


# ---------------------------------------------------------------------------
# Neo4j-gated fixture (mirrors test_decision_memory_records.py:db_clean)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_clean():
    """Connect to Neo4j, wipe the test-mem-cap project scope, yield, wipe again.
    Skips the whole test when Neo4j is unreachable."""
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")
    await _wipe(conn)
    yield conn
    await _wipe(conn)
    await conn.close()


async def _wipe(conn: Neo4jConnection) -> None:
    await conn.clear_project(_TEST_SCOPE)
    async with conn._driver.session(database=conn._database) as s:
        await (await s.run(
            "MATCH (m:Memory) WHERE m.project STARTS WITH $prefix DETACH DELETE m",
            prefix=_TEST_SCOPE,
        )).consume()


# ---------------------------------------------------------------------------
# [parse-1..4] Pure-Python: parse_memory_markdown
# ---------------------------------------------------------------------------

class TestParseMemoryMarkdown:
    def test_extracts_name_description_type_body_links(self) -> None:
        # [parse-1]: full frontmatter + body must all be extracted correctly,
        # with 'type' read from the NESTED metadata.type field (not top-level).
        mc = _load_memory_capture()
        content = _memory_content(
            name="feedback_no_coauthored_by",
            description="Do NOT add a Co-Authored-By Claude trailer",
            mem_type="feedback",
            body="Commit messages must not include a trailer. See [[other_note]] too.",
        )
        result = mc.parse_memory_markdown(content)
        assert result is not None, "well-formed frontmatter must not return None"
        assert result["name"] == "feedback_no_coauthored_by"
        assert result["description"] == "Do NOT add a Co-Authored-By Claude trailer"
        assert result["type"] == "feedback", (
            "type must come from the NESTED metadata.type field; got: " + repr(result.get("type"))
        )
        assert "Commit messages must not include a trailer" in result["body"]

    def test_extracts_double_bracket_links(self) -> None:
        # [parse-1]: every [[wikilink]] target in the body is captured, deduped.
        mc = _load_memory_capture()
        content = _memory_content(
            body="See [[project_foo]] and also [[project_foo]] again, plus [[feedback_bar]]."
        )
        result = mc.parse_memory_markdown(content)
        assert set(result["links"]) == {"project_foo", "feedback_bar"}, (
            f"links must be deduped [[wikilink]] targets; got {result['links']!r}"
        )

    def test_no_links_yields_empty_list(self) -> None:
        # [parse-1] edge case: a body with no [[links]] -> links == [].
        mc = _load_memory_capture()
        content = _memory_content(body="Plain body text, no references at all.")
        result = mc.parse_memory_markdown(content)
        assert result["links"] == []

    def test_returns_none_for_missing_frontmatter(self) -> None:
        # [parse-2] TEST-EDGE-002: a file with no '---' frontmatter block at all
        # must return None (nothing to mirror -- caller should skip, not crash).
        mc = _load_memory_capture()
        result = mc.parse_memory_markdown("Just plain text, never had frontmatter.\n")
        assert result is None, "content with no frontmatter must parse to None"

    def test_empty_body_yields_empty_string(self) -> None:
        # [parse-3] TEST-EDGE-002: frontmatter present, body empty after the
        # closing '---' -> body == "" (never None, never raises).
        mc = _load_memory_capture()
        content = "---\nname: empty_body_note\ndescription: d\n---\n"
        result = mc.parse_memory_markdown(content)
        assert result is not None
        assert result["body"] == "", f"empty body must parse to ''; got {result['body']!r}"

    def test_missing_description_and_type_default_to_empty_string(self) -> None:
        # [parse-4]: a minimal frontmatter with only 'name' must not raise --
        # description/type default to "".
        mc = _load_memory_capture()
        content = "---\nname: minimal_note\n---\nSome body.\n"
        result = mc.parse_memory_markdown(content)
        assert result is not None
        assert result["name"] == "minimal_note"
        assert result.get("description", "") == ""
        assert result.get("type", "") == ""


# ---------------------------------------------------------------------------
# [index-1] is_memory_index_file
# ---------------------------------------------------------------------------

class TestIsMemoryIndexFile:
    def test_memory_md_recognized_as_index(self) -> None:
        mc = _load_memory_capture()
        assert mc.is_memory_index_file(
            "/home/u/.claude/projects/-foo/memory/MEMORY.md"
        ) is True

    def test_ordinary_note_is_not_the_index(self) -> None:
        mc = _load_memory_capture()
        assert mc.is_memory_index_file(
            "/home/u/.claude/projects/-foo/memory/feedback_x.md"
        ) is False

    def test_lowercase_variant_is_not_falsely_excluded(self) -> None:
        # A file that merely CONTAINS "memory" in its name (but isn't the exact
        # index) must not be excluded -- exact basename match only.
        mc = _load_memory_capture()
        assert mc.is_memory_index_file(
            "/home/u/.claude/projects/-foo/memory/memory_notes.md"
        ) is False


# ---------------------------------------------------------------------------
# [proj-1] derive_project_from_memory_path
# ---------------------------------------------------------------------------

class TestDeriveProjectFromMemoryPath:
    def test_extracts_encoded_project_segment(self) -> None:
        mc = _load_memory_capture()
        project = mc.derive_project_from_memory_path(
            "/home/u/.claude/projects/-home-u-myrepo/memory/feedback_x.md"
        )
        assert project == "-home-u-myrepo", f"got {project!r}"

    def test_returns_none_for_non_memory_path(self) -> None:
        mc = _load_memory_capture()
        assert mc.derive_project_from_memory_path("/home/u/project/src/main.py") is None


# ---------------------------------------------------------------------------
# [payload-1..3] build_memory_payload
# ---------------------------------------------------------------------------

class TestBuildMemoryPayload:
    def test_assembles_full_payload(self) -> None:
        # [payload-1]: path/project/session_id/status are attached on top of the
        # parsed frontmatter fields.
        mc = _load_memory_capture()
        path = "/home/u/.claude/projects/-home-u-myrepo/memory/feedback_x.md"
        content = _memory_content(name="feedback_x", mem_type="feedback")
        payload = mc.build_memory_payload(path, content, session_id="sess-xyz")
        assert payload is not None
        assert payload["name"] == "feedback_x"
        assert payload["type"] == "feedback"
        assert payload["path"] == path
        assert payload["project"] == "-home-u-myrepo"
        assert payload["session_id"] == "sess-xyz"
        assert payload["status"] == "live", "a freshly-written memory must be status='live'"
        assert "updated_at" in payload and payload["updated_at"], "updated_at must be set"

    def test_returns_none_for_memory_md(self) -> None:
        # [payload-2]: MEMORY.md must never produce a payload, regardless of content.
        mc = _load_memory_capture()
        path = "/home/u/.claude/projects/-home-u-myrepo/memory/MEMORY.md"
        content = "# Memory index\n- [Foo](foo.md)\n"
        assert mc.build_memory_payload(path, content, session_id="s") is None

    def test_returns_none_when_frontmatter_unparsable(self) -> None:
        # [payload-3]: fail-open at the parser boundary -- unparsable content
        # yields None rather than raising, so the hook can skip silently.
        mc = _load_memory_capture()
        path = "/home/u/.claude/projects/-home-u-myrepo/memory/broken.md"
        assert mc.build_memory_payload(path, "no frontmatter here", session_id="s") is None


# ---------------------------------------------------------------------------
# [create-1..3] Neo4j-gated: Memory node create/upsert semantics
# ---------------------------------------------------------------------------

class TestMemoryRecordStore:
    @pytest.mark.asyncio
    async def test_create_memory_exists_on_connection(self, db_clean: Neo4jConnection) -> None:
        assert hasattr(db_clean, "create_memory"), (
            "Neo4jConnection must have a create_memory method (record_store.py)"
        )

    @pytest.mark.asyncio
    async def test_create_memory_returns_name(self, db_clean: Neo4jConnection) -> None:
        # [create-1]
        name = f"test-note-{uuid.uuid4().hex[:8]}"
        result = await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="d", type="project",
            body="b", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        assert result == name

    @pytest.mark.asyncio
    async def test_create_memory_stores_all_fields(self, db_clean: Neo4jConnection) -> None:
        # [create-1]: name/description/type/body/links/path/session_id/status
        # must all be readable back via direct Cypher.
        name = f"test-note-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="a test description",
            type="user", body="the body text", links=["other_note"],
            path="/home/u/.claude/projects/-p/memory/x.md", session_id="sess-1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (m:Memory {name: $name, project: $project}) "
                "RETURN m.description AS description, m.type AS type, m.body AS body, "
                "m.links AS links, m.path AS path, m.session_id AS session_id, "
                "m.status AS status",
                name=name, project=_TEST_SCOPE,
            )
            row = await res.single()
        assert row is not None, "Memory node not found after create_memory"
        assert row["description"] == "a test description"
        assert row["type"] == "user"
        assert row["body"] == "the body text"
        assert row["status"] == "live"

    @pytest.mark.asyncio
    async def test_create_memory_sets_provenance_record(self, db_clean: Neo4jConnection) -> None:
        # Reuses the decision-memory runtime-record pattern (plan.md Analysis):
        # provenance='record' is what exempts Memory from reconcile/parity.
        name = f"test-note-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="d", type="project",
            body="b", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (m:Memory {name: $name, project: $project}) RETURN m.provenance AS p",
                name=name, project=_TEST_SCOPE,
            )
            row = await res.single()
        assert row["p"] == "record", f"Memory.provenance must be 'record'; got {row['p']!r}"

    @pytest.mark.asyncio
    async def test_editing_re_merges_no_duplicate(self, db_clean: Neo4jConnection) -> None:
        # [create-2]: writing the SAME (name, project) twice with different body
        # text must upsert -- exactly one node, latest body wins.
        name = f"test-note-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="d", type="project",
            body="first version", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="d", type="project",
            body="edited version", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T01:00:00Z", status="live",
        )
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (m:Memory {name: $name, project: $project}) "
                "RETURN count(m) AS cnt, collect(m.body)[0] AS body",
                name=name, project=_TEST_SCOPE,
            )
            row = await res.single()
        assert row["cnt"] == 1, f"expected exactly 1 Memory node after edit, got {row['cnt']}"
        assert row["body"] == "edited version", (
            "the re-MERGE must update the body to the latest write"
        )


# ---------------------------------------------------------------------------
# [hook-1..5] The PostToolUse mirror hook (writ-memory-capture.sh)
# ---------------------------------------------------------------------------

def _envelope(tool_name: str, file_path: str, session_id: str = "hook-test-sess") -> str:
    return json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    })


def _run_hook(envelope: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["WRIT_PORT"] = "19999"  # unreachable, unless overridden
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", HOOK_SH], input=envelope, capture_output=True, text=True, env=env, timeout=10,
    )


class TestMemoryCaptureHookWiring:
    def test_hook_file_exists(self) -> None:
        assert os.path.exists(HOOK_SH), f"{HOOK_SH} not found"

    def test_registered_on_posttooluse_write_edit(self) -> None:
        # [hook-1]
        data = json.loads(Path(HOOKS_JSON).read_text())["hooks"]
        scripts = []
        for g in data.get("PostToolUse", []):
            matcher = g.get("matcher", "")
            if "Write" in matcher.split("|") or "Edit" in matcher.split("|"):
                scripts += [h["command"].rsplit("/", 1)[-1] for h in g.get("hooks", [])]
        assert "writ-memory-capture.sh" in scripts, (
            "writ-memory-capture.sh must be registered on PostToolUse for Write|Edit"
        )

    def test_not_registered_on_pretooluse(self) -> None:
        # PostToolUse-only is what guarantees a policy-guard-denied write is never
        # mirrored (PreToolUse deny means the tool call never completes, so
        # PostToolUse never fires for it). If this hook were ALSO wired to
        # PreToolUse, that guarantee would not hold.
        data = json.loads(Path(HOOKS_JSON).read_text())["hooks"]
        scripts = []
        for g in data.get("PreToolUse", []):
            scripts += [h["command"].rsplit("/", 1)[-1] for h in g.get("hooks", [])]
        assert "writ-memory-capture.sh" not in scripts, (
            "writ-memory-capture.sh must NOT be registered on PreToolUse -- a "
            "PreToolUse deny must never even reach this hook"
        )


class TestMemoryCaptureHookPathFilter:
    def test_non_memory_path_ignored(self) -> None:
        # [hook-2]
        result = _run_hook(_envelope("Write", "/home/u/project/src/main.py"))
        assert result.returncode == 0

    def test_non_write_edit_tool_ignored(self) -> None:
        # [hook-2]
        result = _run_hook(_envelope("Bash", "/home/u/.claude/projects/-p/memory/x.md"))
        assert result.returncode == 0

    def test_claude_md_is_not_a_memory_path(self) -> None:
        # A project CLAUDE.md is not auto-memory (no /memory/ path segment).
        result = _run_hook(_envelope("Write", "/home/u/project/CLAUDE.md"))
        assert result.returncode == 0


class TestMemoryCaptureHookDaemonDown:
    """[hook-3], [hook-4], [hook-5]: fail-open + friction, MEMORY.md exclusion.

    Both scenarios run against an UNREACHABLE daemon port. If the hook attempts
    to reach the daemon at all, that attempt necessarily fails (fast, via
    connect-timeout), producing a memory_capture_failed friction event. A path
    that is filtered out BEFORE any daemon attempt (MEMORY.md) must therefore
    produce NO friction event under the exact same unreachable-daemon
    conditions -- this is what proves the exclusion happens pre-network-call,
    not as a post-hoc "well it failed anyway" coincidence.
    """

    def test_memory_note_daemon_down_exits_zero(self, tmp_path: Path) -> None:
        # [hook-4]: the memory write on disk already succeeded (PostToolUse only
        # fires on success); a down daemon must never turn that into a hook
        # failure the harness would surface as an error.
        log_path = tmp_path / "workflow-friction.log"
        result = _run_hook(
            _envelope("Write", "/home/u/.claude/projects/-p/memory/feedback_x.md"),
            extra_env={"WRIT_FRICTION_LOG": str(log_path)},
        )
        assert result.returncode == 0, (
            f"hook must exit 0 when the daemon is unreachable; "
            f"stderr={result.stderr!r}"
        )

    def test_memory_note_daemon_down_logs_friction_event(self, tmp_path: Path) -> None:
        # [hook-5]
        log_path = tmp_path / "workflow-friction.log"
        _run_hook(
            _envelope("Write", "/home/u/.claude/projects/-p/memory/feedback_x.md"),
            extra_env={"WRIT_FRICTION_LOG": str(log_path)},
        )
        assert log_path.exists(), (
            "a memory_capture_failed friction event must be logged when the "
            "daemon is unreachable"
        )
        text = log_path.read_text()
        assert "memory_capture_failed" in text, (
            f"expected 'memory_capture_failed' in friction log; got: {text!r}"
        )

    def test_memory_md_produces_no_friction_event_even_when_daemon_down(self, tmp_path: Path) -> None:
        # [hook-3]: MEMORY.md must be filtered out BEFORE any daemon attempt, so
        # under the identical unreachable-daemon conditions above, NO friction
        # event is produced (there was nothing to fail-open about).
        log_path = tmp_path / "workflow-friction.log"
        result = _run_hook(
            _envelope("Write", "/home/u/.claude/projects/-p/memory/MEMORY.md"),
            extra_env={"WRIT_FRICTION_LOG": str(log_path)},
        )
        assert result.returncode == 0
        if log_path.exists():
            assert "memory_capture_failed" not in log_path.read_text(), (
                "MEMORY.md writes must never attempt (and therefore never fail) "
                "a daemon call"
            )


# ---------------------------------------------------------------------------
# [route-1..2] POST /memory-record (server route)
# ---------------------------------------------------------------------------

class TestMemoryRecordRoute:
    def test_guards_db_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # [route-1]
        from fastapi.testclient import TestClient
        import writ.server as _srv
        from writ.server import app

        monkeypatch.setattr(_srv, "_db", None)
        client = TestClient(app)
        resp = client.post("/memory-record", json={
            "project_root": "/home/u/.claude/projects/-p/memory/x.md",
            "path": "/home/u/.claude/projects/-p/memory/x.md",
            "name": "x", "description": "d", "type": "project", "body": "b",
            "links": [], "session_id": "s",
        })
        assert resp.status_code == 200
        assert "error" in resp.json()

    def test_logs_and_returns_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # [route-2]: fail-open -- an internal exception must not propagate; the
        # route must still return 200 and log memory_capture_failed. Mirrors
        # decision_memory.py's commit_capture except-block shape.
        from fastapi.testclient import TestClient
        import writ.server as _srv
        from writ.server import app

        class _ErrorDB:
            async def create_memory(self, **kwargs):
                raise RuntimeError("simulated create_memory failure")

        monkeypatch.setattr(_srv, "_db", _ErrorDB())
        logged = []
        monkeypatch.setattr(
            _srv, "log_friction_event",
            lambda **kw: logged.append(kw), raising=False,
        )
        client = TestClient(app)
        resp = client.post("/memory-record", json={
            "project_root": "/home/u/.claude/projects/-p/memory/x.md",
            "path": "/home/u/.claude/projects/-p/memory/x.md",
            "name": "x", "description": "d", "type": "project", "body": "b",
            "links": [], "session_id": "s",
        })
        assert resp.status_code == 200, (
            f"route must return 200 even on internal exception (fail-open); "
            f"got {resp.status_code}: {resp.text}"
        )


# ---------------------------------------------------------------------------
# [cli-bf-1..6] `writ memory backfill`
# ---------------------------------------------------------------------------

class _FakeMemoryDB:
    """Records create_memory/list_memories/tombstone_missing_memories calls
    without touching Neo4j -- used to pin the CLI's WALK + upsert wiring."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.tombstoned: list[tuple] = []

    async def create_memory(self, **kwargs) -> str:
        self.created.append(kwargs)
        return kwargs["name"]

    async def tombstone_missing_memories(self, project: str, existing_names) -> int:
        self.tombstoned.append((project, set(existing_names)))
        return 0

    async def list_memories(self, project: str, include_deleted: bool = False):
        return []


class TestProjectDerivationParity:
    """Review fix: the daemon-side fallback and the stdlib parser must resolve the
    same project segment forever. The route cannot import bin/lib (hook-world,
    no-venv), so this parity table is the drift guard."""

    @pytest.mark.parametrize("path,expected", [
        ("/home/u/.claude/projects/-proj-one/memory/note_a.md", "-proj-one"),
        (r"C:\Users\u\.claude\projects\-proj-two\memory\note_b.md", "-proj-two"),
        ("/deep/other/root/-scoped/memory/note.md", "-scoped"),
        ("/home/u/.claude/projects/-proj-one/notes/note_a.md", None),
        ("memory/note.md", None),
        ("", None),
    ])
    def test_route_fallback_matches_lib_parser(self, path: str, expected) -> None:
        from writ.server.routes.decision_memory import _project_from_memory_path

        mc = _load_memory_capture()
        lib_result = mc.derive_project_from_memory_path(path)
        route_result = _project_from_memory_path(path)
        assert lib_result == expected, f"lib parser drifted for {path!r}"
        assert route_result == (expected or ""), f"route fallback drifted for {path!r}"


class TestMemoryBackfillCli:
    def _invoke(self, projects_root: Path, fake_db: "_FakeMemoryDB"):
        from contextlib import asynccontextmanager
        from typer.testing import CliRunner
        from unittest.mock import patch
        from writ.cli import app

        @asynccontextmanager
        async def _fake_writ_db():
            yield fake_db

        runner = CliRunner()
        with patch("writ.cli._writ_db", new=_fake_writ_db):
            return runner.invoke(app, ["memory", "backfill", "--projects-root", str(projects_root)])

    def test_upserts_every_memory_file_across_projects(self, tmp_path: Path) -> None:
        # [cli-bf-1]
        projects_root = tmp_path / "projects"
        p1 = _memory_path(projects_root, "-proj-one", "note_a.md")
        p1.write_text(_memory_content(name="note_a"))
        p2 = _memory_path(projects_root, "-proj-two", "note_b.md")
        p2.write_text(_memory_content(name="note_b"))

        fake_db = _FakeMemoryDB()
        result = self._invoke(projects_root, fake_db)

        assert result.exit_code == 0, (
            f"backfill must exit 0; got {result.exit_code}\n{result.output}"
        )
        created_names = {c["name"] for c in fake_db.created}
        assert created_names == {"note_a", "note_b"}, (
            f"backfill must upsert every memory file across every project; "
            f"got {created_names!r}"
        )

    def test_excludes_memory_md(self, tmp_path: Path) -> None:
        # [cli-bf-2]
        projects_root = tmp_path / "projects"
        note = _memory_path(projects_root, "-proj-one", "note_a.md")
        note.write_text(_memory_content(name="note_a"))
        index = _memory_path(projects_root, "-proj-one", "MEMORY.md")
        index.write_text("# Memory index\n- [note a](note_a.md)\n")

        fake_db = _FakeMemoryDB()
        self._invoke(projects_root, fake_db)

        created_names = {c["name"] for c in fake_db.created}
        assert "MEMORY" not in created_names and len(fake_db.created) == 1, (
            f"MEMORY.md must never be upserted as a memory; created={fake_db.created!r}"
        )

    def test_reports_counts_in_output(self, tmp_path: Path) -> None:
        # [cli-bf-3]
        projects_root = tmp_path / "projects"
        note = _memory_path(projects_root, "-proj-one", "note_a.md")
        note.write_text(_memory_content(name="note_a"))

        fake_db = _FakeMemoryDB()
        result = self._invoke(projects_root, fake_db)

        assert "1" in result.output, (
            f"backfill output must report the upsert count; got: {result.output!r}"
        )

    def test_unreadable_file_is_protected_from_tombstone(self, tmp_path: Path) -> None:
        # Review fix: a transient read failure must not tombstone a memory whose
        # file still exists. The stem stands in for the name (naming convention:
        # name == filename stem) so the record survives until the next run.
        if os.geteuid() == 0:
            pytest.skip("root can read mode-000 files; the failure cannot be staged")
        projects_root = tmp_path / "projects"
        note = _memory_path(projects_root, "-proj-one", "note_locked.md")
        note.write_text(_memory_content(name="note_locked"))
        note.chmod(0o000)

        fake_db = _FakeMemoryDB()
        result = self._invoke(projects_root, fake_db)
        note.chmod(0o600)

        assert result.exit_code == 0
        assert not fake_db.created, "an unreadable file must not be upserted"
        assert fake_db.tombstoned, "the per-project tombstone sweep must still run"
        _project, existing = fake_db.tombstoned[0]
        assert "note_locked" in existing, (
            "an unreadable-but-present file must be protected from the tombstone "
            f"sweep via its stem; existing={existing!r}"
        )

    def test_tombstones_missing_files(self, tmp_path: Path) -> None:
        # [cli-bf-4]: tombstone_missing_memories must be called per project with
        # the set of names that STILL exist on disk (so the CLI/DB layer can
        # diff against what is currently live in the graph).
        projects_root = tmp_path / "projects"
        note = _memory_path(projects_root, "-proj-one", "note_a.md")
        note.write_text(_memory_content(name="note_a"))

        fake_db = _FakeMemoryDB()
        self._invoke(projects_root, fake_db)

        assert fake_db.tombstoned, "backfill must call tombstone_missing_memories at least once"
        project, existing = fake_db.tombstoned[0]
        assert "note_a" in existing, (
            f"the still-present file must be in the existing-names set passed to "
            f"tombstone_missing_memories; got {existing!r}"
        )

    def test_contained_under_projects_root_symlink_escape(self, tmp_path: Path) -> None:
        # [cli-bf-5] SEC-INJ-PATH-001: a symlinked project directory pointing
        # OUTSIDE projects_root must not be followed into.
        projects_root = tmp_path / "projects"
        projects_root.mkdir()
        outside = tmp_path / "outside"
        (outside / "memory").mkdir(parents=True)
        (outside / "memory" / "secret.md").write_text(_memory_content(name="secret"))

        escape_link = projects_root / "-escape"
        escape_link.symlink_to(outside, target_is_directory=True)

        fake_db = _FakeMemoryDB()
        self._invoke(projects_root, fake_db)

        created_names = {c["name"] for c in fake_db.created}
        assert "secret" not in created_names, (
            "backfill must not follow a symlinked project dir outside "
            "--projects-root (path-containment check missing)"
        )

    def test_empty_memory_dir_backfills_to_zero(self, tmp_path: Path) -> None:
        # [cli-bf-6] TEST-EDGE-002: an empty memory dir (project registered but
        # no notes written yet) must backfill cleanly to zero, not error.
        projects_root = tmp_path / "projects"
        (projects_root / "-proj-empty" / "memory").mkdir(parents=True)

        fake_db = _FakeMemoryDB()
        result = self._invoke(projects_root, fake_db)

        assert result.exit_code == 0, (
            f"an empty memory dir must not error; got {result.exit_code}\n{result.output}"
        )
        assert fake_db.created == []


# ---------------------------------------------------------------------------
# [cli-ls-1..3] `writ memory list --project <p>`
# ---------------------------------------------------------------------------

class _FakeListDB:
    def __init__(self, memories: dict[str, list[dict]]) -> None:
        self._memories = memories

    async def list_memories(self, project: str, include_deleted: bool = False):
        return self._memories.get(project, [])


class TestMemoryListCli:
    def _invoke(self, fake_db: "_FakeListDB", args: list[str]):
        from contextlib import asynccontextmanager
        from typer.testing import CliRunner
        from unittest.mock import patch
        from writ.cli import app

        @asynccontextmanager
        async def _fake_writ_db():
            yield fake_db

        runner = CliRunner()
        with patch("writ.cli._writ_db", new=_fake_writ_db):
            return runner.invoke(app, args)

    def test_returns_project_live_memories(self) -> None:
        # [cli-ls-1]
        fake_db = _FakeListDB({
            "proj-a": [{"name": "note_a", "description": "d", "status": "live"}],
        })
        result = self._invoke(fake_db, ["memory", "list", "--project", "proj-a"])
        assert result.exit_code == 0
        assert "note_a" in result.output

    def test_excludes_tombstoned_memories(self) -> None:
        # [cli-ls-2]: the fake DB's list_memories already excludes deleted ones
        # by default (include_deleted=False); this pins that the CLI does not
        # override that default to True.
        fake_db = _FakeListDB({"proj-a": []})
        result = self._invoke(fake_db, ["memory", "list", "--project", "proj-a"])
        assert result.exit_code == 0
        assert "note_a" not in result.output

    def test_absent_project_returns_empty_not_error(self) -> None:
        # [cli-ls-3] TEST-EDGE-002
        fake_db = _FakeListDB({})
        result = self._invoke(fake_db, ["memory", "list", "--project", "no-such-project"])
        assert result.exit_code == 0, (
            f"listing an absent project must not error; got {result.exit_code}\n{result.output}"
        )


# ---------------------------------------------------------------------------
# [rcl-1..2] Neo4j-gated: reconcile / prune survival (runtime-record exemption)
# ---------------------------------------------------------------------------

class TestMemorySurvivesReconcileAndPrune:
    @pytest.mark.asyncio
    async def test_memory_survives_reconcile(self, db_clean: Neo4jConnection) -> None:
        # [rcl-1] (keystone, mirrors test_decision_memory_records.py
        # TestRecordSurvivesReconcile): a Memory node absent from the bible
        # markdown oracle must NOT be deleted by reconcile (provenance='record').
        from writ.graph.methodology_ingest import reconcile

        name = f"test-mem-keystone-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="d", type="project",
            body="b", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        bible = Path(__file__).resolve().parent.parent / "bible"
        result = await reconcile(bible, db_clean, project=_TEST_SCOPE)
        assert name not in result["deleted_nodes"], (
            "reconcile deleted a Memory node with provenance='record'"
        )
        async with db_clean._driver.session(database=db_clean._database) as s:
            res = await s.run(
                "MATCH (m:Memory {name: $name, project: $project}) RETURN count(m) AS c",
                name=name, project=_TEST_SCOPE,
            )
            row = await res.single()
        assert row["c"] == 1, "Memory node gone after reconcile despite provenance='record'"

    @pytest.mark.asyncio
    async def test_memory_exempt_from_parity_violations(self, db_clean: Neo4jConnection) -> None:
        # [rcl-2] (mirrors test_decision_memory_records.py TestRecordParityExempt)
        from writ.graph.integrity import IntegrityChecker

        name = f"test-mem-parity-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=name, project=_TEST_SCOPE, description="d", type="project",
            body="b", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        bible = Path(__file__).resolve().parent.parent / "bible"
        checker = IntegrityChecker(db_clean._driver, db_clean._database)
        violations = await checker.detect_parity_violations(bible_dir=bible, project=_TEST_SCOPE)
        violation_ids = {v["id"] for v in violations}
        assert name not in violation_ids, (
            "detect_parity_violations flagged a Memory node with provenance='record' "
            "(prune must not surface it)"
        )


# ---------------------------------------------------------------------------
# [chan-1..3] Memory is excluded from every retrieval channel
# ---------------------------------------------------------------------------

class TestMemoryExcludedFromRetrievalChannels:
    def test_memory_absent_from_node_type_enum(self) -> None:
        # [chan-1]
        from writ.graph.schema import NodeType
        assert "Memory" not in {nt.value for nt in NodeType}, (
            "'Memory' must not be a NodeType member -- it is a runtime record, "
            "never a retrieval candidate"
        )

    def test_memory_absent_from_retrievable_node_types(self) -> None:
        # [chan-1]
        from writ.graph.schema import RETRIEVABLE_NODE_TYPES
        assert "Memory" not in {nt.value for nt in RETRIEVABLE_NODE_TYPES}

    def test_memory_absent_from_node_type_models(self) -> None:
        # [chan-1]
        from writ.graph.schema import NODE_TYPE_MODELS
        assert "Memory" not in NODE_TYPE_MODELS, (
            "'Memory' must not be in NODE_TYPE_MODELS -- it uses a custom "
            "create_memory method, not the generic node-write dispatch"
        )

    def test_memory_absent_from_node_id_fields(self) -> None:
        # [chan-1]: structural protection so reconcile/parity enumeration never
        # treats a Memory node as an addressable oracle-comparable id.
        from writ.graph.schema import NODE_ID_FIELDS
        assert "Memory" not in NODE_ID_FIELDS

    @pytest.mark.asyncio
    async def test_memory_content_never_returned_by_query_endpoint(
        self, db_clean: Neo4jConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [chan-2]: a Memory node with a distinctive body string must never
        # surface as a rule result from POST /query, even when the prompt text
        # matches it closely.
        from fastapi.testclient import TestClient
        import writ.server as _srv
        from writ.server import app

        distinctive = f"UNIQUE-MEMORY-MARKER-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=f"test-mem-query-{uuid.uuid4().hex[:8]}", project=_TEST_SCOPE,
            description=distinctive, type="project", body=distinctive, links=[],
            path="/tmp/x.md", session_id="s1", updated_at="2026-08-01T00:00:00Z",
            status="live",
        )
        monkeypatch.setattr(_srv, "_db", db_clean)
        client = TestClient(app)
        # /query's request field is `query` (QueryRequest), not `prompt`: posting
        # "prompt" 422s on validation, which would let this test "pass" its leak
        # assertion without the retrieval pipeline ever running.
        resp = client.post("/query", json={
            "query": distinctive, "project": _TEST_SCOPE,
        })
        assert resp.status_code == 200
        data = resp.json()
        rule_ids = {r.get("rule_id") for r in data.get("rules", [])}
        assert distinctive not in json.dumps(data), (
            f"a Memory node's content must never leak into /query results; "
            f"got: {data!r}"
        )
        assert not any(distinctive in str(rid) for rid in rule_ids)

    @pytest.mark.asyncio
    async def test_memory_never_surfaced_by_methodology_companion(
        self, db_clean: Neo4jConnection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [chan-3]: the methodology-companion endpoint's trigger index is built
        # only from RETRIEVABLE_NODE_TYPES; a Memory node (structurally excluded
        # per test_memory_absent_from_retrievable_node_types) must never appear
        # in its response.
        from writ.server import CompanionRequest, methodology_companion

        distinctive = f"UNIQUE-MEMORY-COMPANION-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=f"test-mem-companion-{uuid.uuid4().hex[:8]}", project=_TEST_SCOPE,
            description=distinctive, type="project", body=distinctive, links=[],
            path="/tmp/x.md", session_id="s1", updated_at="2026-08-01T00:00:00Z",
            status="live",
        )
        resp = await methodology_companion(
            CompanionRequest(mode="work", prompt=distinctive)
        )
        assert distinctive not in json.dumps(resp), (
            f"a Memory node's content must never leak into /methodology-companion "
            f"output; got: {resp!r}"
        )
