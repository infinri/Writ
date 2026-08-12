"""Part 5 of the isolation cycle: `writ memory audit` (plan.md Part 5 / capabilities.md).

`create_memory` MERGEs on `(name, project)` with a bare `SET m += $props` -- no
`ON CREATE` / `ON MATCH` split -- so a second write that matches an existing node
silently overwrites its body, description and path. A Memory node filed under the
wrong project is therefore not just mis-scoped, it is a way to destroy another
project's memory content. `writ memory audit` is the read-only diagnostic: it
reports, it repairs nothing (capabilities.md: "Repairing or re-tagging any Memory
node" is explicitly out of scope for this part).

GROUNDING (2026-08-11, read-only Cypher over the live graph): 202 Memory nodes, 0
empty projects, 0 empty paths, 0 stored-vs-stored mismatches. The in-graph
mismatch/empty checks are therefore INVARIANT GUARDS that are currently satisfied,
not detectors of a live problem -- a test that only runs the audit against the
live graph would pass against an empty implementation. Every positive test below
therefore constructs its own synthetic finding rather than reading live state.

SHIPPED CONTRACT (writ/cli.py:1885-2100, writ/graph/db/record_store.py:120-138):

  Neo4jConnection.list_all_memories() -> list[dict]
      Every Memory node, every project, ONE unfiltered MATCH/RETURN (no coalesce,
      so a missing property stays distinguishable from a defaulted one; tombstoned
      rows are included). Each row carries name/project/path/status/type/updated_at.
      Ordered by (project, name).

  `writ memory audit [--projects-root PATH] [--json]` (on the existing memory_app)
      Reads via `db.list_all_memories()` and, separately, walks --projects-root
      (os.walk, followlinks=False). Writes nothing. `--json` emits:
        {
          "read_only": true, "repaired": 0, "examined": <int>,
          "projects_root": str, "projects_root_present": bool,
          "project_dirs_scanned": int,
          "counts": {"mismatch": N, "empty": N, "disk_drift": N, "collision": N},
          "mismatch":   [{name, project, path, derived_project}, ...],
          "empty":      [{name, project, path, missing: [field, ...]}, ...],
          "disk_drift": [{name, project, path, kind, ...}, ...],
          "collision":  [{project: <segment>, directories: [str, ...]}, ...],
        }
      `disk_drift` entries carry a `kind` of "path_missing" (stored path is gone),
      "path_unresolvable" (Path.resolve() itself raised OSError -- a defensive
      branch not exercised here: Path.exists() already swallows ENOENT/ENOTDIR/
      EBADF/ELOOP before resolve() is ever reached, so this branch has no known
      portable trigger), or "live_project_changed" (path exists, but the project
      derived from the RESOLVED path differs from the stored one). Text mode
      prints "examined=N ... mismatch=N empty=N disk_drift=N collision=N" plus one
      line per finding and a closing "Audit is READ-ONLY" line.

  BUCKETS ARE NOT DISJOINT. mismatch (stored-vs-stored) and disk_drift's
  live_project_changed (stored-vs-live) are two different claims about the same
  node, so counts are per FINDING, not per node (writ/cli.py:2008-2012). No test
  here asserts a node appears in exactly one bucket.

  `/memory-record` (writ/server/routes/decision_memory.py:87-119): when the caller
  sends no `project`, the route derives one from `path` and logs a
  `memory_project_derived` friction event (fields: file_path, derived_project,
  resolved) BEFORE writing, because the guess decides which project's memory the
  MERGE can overwrite.

GRAPH SAFETY. There is no isolated Neo4j instance on this machine; the test
container is stopped and bolt://localhost:7687 is PRODUCTION (202 Memory nodes,
287 Rule nodes). Per the coordinator's refinement of the house pattern: prefer
pure-function / stubbed-accessor tests over live fixture nodes wherever the
behavior under test does not itself require a real Cypher round trip, because a
stub cannot leave anything behind to clean up. Every bucket-computation test
below therefore monkeypatches `writ.cli._writ_db` (the same seam
`TestMemoryBackfillCli` / `TestMemoryListCli` in test_memory_capture.py already
use) with an in-process fake whose `list_all_memories` returns synthetic rows --
no Neo4j connection is opened at all for those tests.

The ONE exception is `TestListAllMemoriesStoreMethod`, which has to prove the
real Cypher (unfiltered, no coalesce, tombstones included, ordered) actually
behaves as documented, so it touches the live graph. Its scope string
(`_TEST_SCOPE`) deliberately does NOT start with "-": every real Claude-Code-
encoded project is derived from an absolute path and therefore always starts with
"-", so a scope string that structurally cannot start with "-" can never equal a
real project value. Cleanup is `clear_project(_TEST_SCOPE)` (exact match) before
and after, never a whole-graph wipe, never `clear_all`. If Neo4j is unreachable
the fixture skips, mirroring `test_memory_capture.py`'s `db_clean`.

EXTRACTED TO MODULE LEVEL (writ/cli.py): `classify_memory_rows(rows, derive)` --
the mismatch/empty/disk_drift computation with no CliRunner, no asyncio, no
monkeypatched database -- and `render_memory_audit_text(report)`, the text
renderer. Both are unit-tested directly below (classify-1/2, render-1) in
addition to the existing CLI-level bucket tests, which stay as the integration
proof that the command wires the extracted pieces together correctly. Behavior
is otherwise unchanged by the refactor: same buckets, same reason strings,
non-disjoint buckets, counts per finding not per node.

DEPTH CAP, TWO DISTINCT COUNTS. The collision scan's directory walk is bounded
by a module-level depth cap, and the walk also stops descending into a project
directory's own subtree once its `memory/` child is found there (a project
directory does not contain other project directories). These are reported as
TWO SEPARATE counts, not one:
  - a ROUTINE skip count (the memory/-found short-circuit above): expected on
    every ordinary run and must not read as a warning.
  - a DEPTH-CAP prune count: the real truncation, and it must stay loud, with a
    number, in both the text and the --json output.
A single merged count would make the default projects root report the scan
INCOMPLETE on every healthy run (session-scratch subtrees under each project
directory trip a single undifferentiated cap), which is an alarm an operator
learns to ignore -- exactly the silent-truncation failure mode this cap exists
to avoid. depth-1/2/3 below keep the two counts separable on purpose.

Capability map:
  [store-1]   list_all_memories is unfiltered, untombstone-filtered, ordered
  [audit-1/2] mismatch bucket: positive + negative (stored vs stored-derived)
  [audit-3/4] empty bucket: empty project, empty path
  [audit-5/6] disk_drift bucket: path_missing, live_project_changed (via a real
              symlink, so the raw stored string does NOT also trigger mismatch)
  [audit-7/8] collision bucket: nested-vs-flat same segment, ordinary distinct
  [audit-9]   read-only, proven structurally (stub defines no write method)
  [audit-10]  --json shape / text-mode counts
  [route-1]   /memory-record logs memory_project_derived when it had to derive
  [classify-1] classify_memory_rows(rows, derive) called directly: mismatch,
               empty (project/path), disk_drift (path_missing,
               live_project_changed) -- no CliRunner, no monkeypatched db
  [classify-2] one row can land in BOTH mismatch and disk_drift at once,
               pinning the documented non-disjoint bucket semantics
  [render-1]   render_memory_audit_text(report) carries the per-bucket counts
               and, when one applies, the depth-cap truncation notice
  [depth-1]    a collision pair nested past the depth cap is not reported, and
               the --json/text output states the depth-cap prune count
  [depth-2]    a collision pair nested within the depth cap is still reported
  [depth-3]    the routine skip count and the depth-cap prune count are two
               DIFFERENT fields, so a future change cannot quietly merge them
               back into one number and hide a real truncation behind a
               routine one
  [route-2]    /memory-record logs memory_project_derived with resolved=False
               when the path itself derives no project either
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user
from writ.graph.db import Neo4jConnection

# Deliberately does NOT start with "-": every real Claude-Code-encoded project
# segment is derived from an absolute path and therefore always starts with "-",
# so this string can never equal a real project value.
_TEST_SCOPE = "zzz-writ-test-only-memory-audit-scope-never-a-real-project"


# ---------------------------------------------------------------------------
# [store-1] Neo4jConnection.list_all_memories -- real graph, private scope
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def db_clean():
    conn = Neo4jConnection(get_neo4j_uri(), get_neo4j_user(), get_neo4j_password())
    try:
        async with conn._driver.session(database=conn._database) as s:
            await (await s.run("RETURN 1 AS ok")).consume()
    except Exception:
        await conn.close()
        pytest.skip("Neo4j unreachable")
    await conn.clear_project(_TEST_SCOPE)
    yield conn
    await conn.clear_project(_TEST_SCOPE)
    await conn.close()


class TestListAllMemoriesStoreMethod:
    @pytest.mark.asyncio
    async def test_unfiltered_and_includes_tombstoned(self, db_clean: Neo4jConnection) -> None:
        # [store-1]: list_memories(project) excludes status='deleted' by default;
        # list_all_memories must NOT -- a mis-filed tombstone is still mis-filed.
        live_name = f"live-{uuid.uuid4().hex[:8]}"
        dead_name = f"dead-{uuid.uuid4().hex[:8]}"
        await db_clean.create_memory(
            name=live_name, project=_TEST_SCOPE, description="d", type="project",
            body="b", links=[], path="/tmp/x.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="live",
        )
        await db_clean.create_memory(
            name=dead_name, project=_TEST_SCOPE, description="d", type="project",
            body="b", links=[], path="/tmp/y.md", session_id="s1",
            updated_at="2026-08-01T00:00:00Z", status="deleted",
        )
        rows = await db_clean.list_all_memories()
        scoped = [r for r in rows if r.get("project") == _TEST_SCOPE]
        names = {r["name"] for r in scoped}
        assert {live_name, dead_name} <= names, (
            f"list_all_memories must be unfiltered; expected both {live_name!r} "
            f"and {dead_name!r} among {names!r}"
        )
        dead_row = next(r for r in scoped if r["name"] == dead_name)
        assert dead_row["status"] == "deleted", (
            "a tombstoned Memory must still be surfaced with its real status, "
            "not silently dropped"
        )
        for row in scoped:
            for field in ("name", "project", "path", "status", "type", "updated_at"):
                assert field in row, f"list_all_memories row missing {field!r}: {row!r}"

    @pytest.mark.asyncio
    async def test_ordered_by_project_then_name(self, db_clean: Neo4jConnection) -> None:
        second, first = f"b-{uuid.uuid4().hex[:6]}", f"a-{uuid.uuid4().hex[:6]}"
        for n in (second, first):
            await db_clean.create_memory(
                name=n, project=_TEST_SCOPE, description="d", type="project",
                body="b", links=[], path="/tmp/x.md", session_id="s1",
                updated_at="2026-08-01T00:00:00Z", status="live",
            )
        rows = await db_clean.list_all_memories()
        scoped_names = [r["name"] for r in rows if r.get("project") == _TEST_SCOPE]
        assert scoped_names == sorted([second, first]), (
            f"rows for one project must be ordered by name; got {scoped_names!r}"
        )


# ---------------------------------------------------------------------------
# Shared stub-DB helpers for `writ memory audit` -- no Neo4j, no cleanup needed.
# ---------------------------------------------------------------------------

def _row(**overrides) -> dict:
    """A synthetic list_all_memories() row. Only override what a test needs
    (TEST-FIXTURE-002): by default project and path agree, so a test that does
    not care about mismatch/drift gets a "clean" baseline row."""
    unique = uuid.uuid4().hex[:8]
    defaults = {
        "name": f"note-{unique}",
        "project": "-home-u-proj",
        "path": "/home/u/.claude/projects/-home-u-proj/memory/note.md",
        "status": "live",
        "type": "project",
        "updated_at": "2026-08-01T00:00:00Z",
    }
    defaults.update(overrides)
    return defaults


def _find(entries: list[dict], name: str) -> dict | None:
    return next((e for e in entries if e.get("name") == name), None)


class _FakeAuditDB:
    def __init__(self, memories: list[dict]) -> None:
        self._memories = memories

    async def list_all_memories(self) -> list[dict]:
        return self._memories


def _invoke_audit(memories: list[dict], projects_root: Path, extra_args: list[str] | None = None):
    from contextlib import asynccontextmanager
    from typer.testing import CliRunner
    from unittest.mock import patch
    from writ.cli import app

    fake_db = _FakeAuditDB(memories)

    @asynccontextmanager
    async def _fake_writ_db():
        yield fake_db

    runner = CliRunner()
    args = ["memory", "audit", "--projects-root", str(projects_root), *(extra_args or [])]
    with patch("writ.cli._writ_db", new=_fake_writ_db):
        return runner.invoke(app, args)


def _audit_json(memories: list[dict], projects_root: Path) -> dict:
    result = _invoke_audit(memories, projects_root, ["--json"])
    assert result.exit_code == 0, (
        f"writ memory audit --json must exit 0; got {result.exit_code}\n{result.output}"
    )
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# [classify-1/2] classify_memory_rows(rows, derive) -- called DIRECTLY.
#
# THE POINT OF THE EXTRACTION: no CliRunner, no asyncio.run, no monkeypatched
# `writ.cli._writ_db`. `rows` is exactly what `list_all_memories()` returns
# (see `_row()` above); `derive` is the injected project-derivation callable.
# ---------------------------------------------------------------------------

def _derive_from_path(path: str) -> str:
    """A standalone stand-in for the `derive` callable classify_memory_rows takes
    as its second argument: the directory segment directly above `memory/`, or ''
    when the path does not have that shape. Mirrors
    bin/lib/memory_capture.py's derive_project_from_memory_path closely enough for
    these pure-function tests -- '' rather than None, so it compares cleanly
    against a stored project that may itself be missing."""
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    if len(parts) < 3 or parts[-2] != "memory":
        return ""
    return parts[-3]


def _classify(rows: list[dict], derive=_derive_from_path) -> dict:
    from writ.cli import classify_memory_rows
    return classify_memory_rows(rows, derive)


class TestClassifyMemoryRowsUnit:
    """[classify-1] Direct calls only -- no CliRunner, no monkeypatched db. Each
    test pins one finding and its reason string, since a moved file and a
    deleted file are different findings even though both land in disk_drift."""

    def test_mismatched_row_reports_derived_project(self) -> None:
        row = _row(
            project="-home-u-proj",
            path="/home/u/.claude/projects/-different-project/memory/note.md",
        )
        result = _classify([row])
        entry = _find(result["mismatch"], row["name"])
        assert entry is not None, (
            f"a stored project disagreeing with the path-derived project must "
            f"be reported under mismatch; got {result['mismatch']!r}"
        )
        assert entry["project"] == "-home-u-proj"
        assert entry["derived_project"] == "-different-project", (
            f"the mismatch entry must name what the path ACTUALLY derives, "
            f"not just flag that it disagrees; got {entry!r}"
        )

    def test_empty_project_reports_missing_project(self) -> None:
        row = _row(project="")
        result = _classify([row])
        entry = _find(result["empty"], row["name"])
        assert entry is not None, (
            f"an empty project must be reported under empty; got {result['empty']!r}"
        )
        assert entry["missing"] == ["project"], (
            f"only 'project' is missing on this row; got {entry['missing']!r}"
        )

    def test_empty_path_reports_missing_path(self) -> None:
        row = _row(path="")
        result = _classify([row])
        entry = _find(result["empty"], row["name"])
        assert entry is not None, (
            f"an empty path must be reported under empty; got {result['empty']!r}"
        )
        assert entry["missing"] == ["path"], (
            f"only 'path' is missing on this row; got {entry['missing']!r}"
        )

    def test_path_missing_on_disk_reports_path_missing_kind(self) -> None:
        ghost = f"/tmp/writ-audit-ghost-{uuid.uuid4().hex}/memory/note.md"
        assert not Path(ghost).exists(), (
            "the ghost path must not exist for this test to mean anything"
        )
        row = _row(project="-home-u-proj", path=ghost)
        result = _classify([row])
        entry = _find(result["disk_drift"], row["name"])
        assert entry is not None, (
            f"a stored path gone from disk must be reported under disk_drift; "
            f"got {result['disk_drift']!r}"
        )
        assert entry["kind"] == "path_missing", f"got kind={entry.get('kind')!r}"

    def test_live_project_changed_reports_that_kind(self, tmp_path: Path) -> None:
        real_dir = tmp_path / "real-drift-target"
        (real_dir / "memory").mkdir(parents=True)
        (real_dir / "memory" / "note.md").write_text("body\n")

        # The symlink's own name matches the stored project, so the raw stored
        # path string does not itself disagree with the stored project (no
        # mismatch); only resolving it on disk reveals the real location.
        symlinked = tmp_path / "-home-u-proj"
        os.symlink(real_dir, symlinked, target_is_directory=True)
        stored_path = str(symlinked / "memory" / "note.md")

        row = _row(project="-home-u-proj", path=stored_path)
        result = _classify([row])
        assert _find(result["mismatch"], row["name"]) is None, (
            "a raw stored path whose own segment matches the stored project "
            "must not also be flagged as a mismatch"
        )
        entry = _find(result["disk_drift"], row["name"])
        assert entry is not None, (
            f"a node whose real (symlink-resolved) location implies a "
            f"different project than the one stored must be reported under "
            f"disk_drift; got {result['disk_drift']!r}"
        )
        assert entry["kind"] == "live_project_changed", f"got kind={entry.get('kind')!r}"
        assert entry["live_project"] == "real-drift-target", (
            f"got live_project={entry.get('live_project')!r}"
        )


class TestClassifyMemoryRowsBucketsAreNotDisjoint:
    """[classify-2] One row, two buckets, at once -- pinned explicitly so a
    future refactor cannot "clean up" the overlap by accident. writ/cli.py's own
    comment calls mismatch and disk_drift's live_project_changed two different
    claims about the same node (stored-vs-stored, stored-vs-live); this test
    states that as an assertion instead of leaving it as a comment."""

    def test_one_row_lands_in_mismatch_and_disk_drift_together(self) -> None:
        ghost = (
            f"/tmp/writ-audit-ghost-{uuid.uuid4().hex}"
            "/-different-project/memory/note.md"
        )
        row = _row(project="-home-u-proj", path=ghost)
        result = _classify([row])

        mismatch_entry = _find(result["mismatch"], row["name"])
        drift_entry = _find(result["disk_drift"], row["name"])
        assert mismatch_entry is not None, (
            f"the stored project disagrees with the path-derived project, so "
            f"this row must appear under mismatch; got {result['mismatch']!r}"
        )
        assert drift_entry is not None, (
            f"the SAME row's stored path is also gone from disk, so it must "
            f"appear under disk_drift too, not be excluded for already "
            f"matching mismatch; got {result['disk_drift']!r}"
        )
        assert drift_entry["kind"] == "path_missing", f"got kind={drift_entry.get('kind')!r}"
        assert mismatch_entry["derived_project"] == "-different-project"


# ---------------------------------------------------------------------------
# [audit-1/2] mismatch bucket
# ---------------------------------------------------------------------------

class TestMemoryAuditMismatchBucket:
    def test_mismatched_project_is_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        row = _row(
            project="-home-u-proj",
            path="/home/u/.claude/projects/-different-project/memory/note.md",
        )
        data = _audit_json([row], root)
        entry = _find(data["mismatch"], row["name"])
        assert entry is not None, (
            f"a node whose stored project disagrees with its path-derived project "
            f"must be reported as a mismatch; got {data['mismatch']!r}"
        )
        assert entry["project"] == "-home-u-proj"
        assert entry["derived_project"] == "-different-project"

    def test_consistent_node_not_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        row = _row()  # default: project == path-derived segment
        data = _audit_json([row], root)
        assert _find(data["mismatch"], row["name"]) is None, (
            f"a node whose stored project matches its path-derived project must "
            f"not be reported as a mismatch; got {data['mismatch']!r}"
        )


# ---------------------------------------------------------------------------
# [audit-3/4] empty bucket
# ---------------------------------------------------------------------------

class TestMemoryAuditEmptyBucket:
    def test_empty_project_is_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        row = _row(project="")
        data = _audit_json([row], root)
        entry = _find(data["empty"], row["name"])
        assert entry is not None, (
            f"a node with an empty project must be reported under empty; "
            f"got {data['empty']!r}"
        )
        assert entry["missing"] == ["project"], (
            f"only 'project' is missing on this row; got {entry['missing']!r}"
        )

    def test_empty_path_is_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        row = _row(path="")
        data = _audit_json([row], root)
        entry = _find(data["empty"], row["name"])
        assert entry is not None, (
            f"a node with an empty path must be reported under empty; "
            f"got {data['empty']!r}"
        )
        assert entry["missing"] == ["path"], (
            f"only 'path' is missing on this row; got {entry['missing']!r}"
        )

    def test_empty_node_excluded_from_mismatch_and_drift(self, tmp_path: Path) -> None:
        # An empty project/path can never be compared against anything, so the
        # audit must `continue` past it rather than also filing a spurious
        # mismatch/disk_drift finding for the same row (writ/cli.py:1984-1992).
        root = tmp_path / "projects"
        root.mkdir()
        row = _row(project="")
        data = _audit_json([row], root)
        assert _find(data["mismatch"], row["name"]) is None
        assert _find(data["disk_drift"], row["name"]) is None


# ---------------------------------------------------------------------------
# [audit-5/6] disk_drift bucket
# ---------------------------------------------------------------------------

class TestMemoryAuditDiskDriftBucket:
    def test_missing_path_reported_with_path_missing_kind(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        ghost = f"/tmp/writ-audit-ghost-{uuid.uuid4().hex}/memory/note.md"
        assert not Path(ghost).exists(), "the ghost path must not exist for this test to mean anything"
        row = _row(project="-home-u-proj", path=ghost)
        data = _audit_json([row], root)
        entry = _find(data["disk_drift"], row["name"])
        assert entry is not None, (
            f"a node whose stored path is gone must be reported under disk_drift; "
            f"got {data['disk_drift']!r}"
        )
        assert entry["kind"] == "path_missing", f"got kind={entry.get('kind')!r}"

    def test_existing_path_with_changed_live_project_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()

        real_dir = tmp_path / "real-drift-target"
        (real_dir / "memory").mkdir(parents=True)
        (real_dir / "memory" / "note.md").write_text("body\n")

        # The symlink's OWN name matches the stored project, so the raw STORED
        # path string does not disagree with the stored project (no mismatch);
        # only resolving the symlink on disk reveals that the file really lives
        # under a different project directory.
        symlinked = tmp_path / "-home-u-proj"
        os.symlink(real_dir, symlinked, target_is_directory=True)
        stored_path = str(symlinked / "memory" / "note.md")

        row = _row(project="-home-u-proj", path=stored_path)
        data = _audit_json([row], root)

        assert _find(data["mismatch"], row["name"]) is None, (
            "a stored path whose raw string segment matches the stored project "
            "must not also be flagged as a mismatch"
        )
        entry = _find(data["disk_drift"], row["name"])
        assert entry is not None, (
            f"a node whose real (symlink-resolved) location implies a different "
            f"project than the one stored must be reported under disk_drift; "
            f"got {data['disk_drift']!r}"
        )
        assert entry["kind"] == "live_project_changed", f"got kind={entry.get('kind')!r}"
        assert entry["live_project"] == "real-drift-target", (
            f"got live_project={entry.get('live_project')!r}"
        )


# ---------------------------------------------------------------------------
# [audit-7/8] collision bucket
# ---------------------------------------------------------------------------

class TestMemoryAuditCollisionBucket:
    def test_nested_and_flat_directories_deriving_same_segment_collide(
        self, tmp_path: Path
    ) -> None:
        # The derivation keys ONLY on the segment directly above `memory/` and
        # discards everything above it (writ/cli.py:1944-1947), so a nested
        # <root>/a/proj/memory and a flat <root>/proj/memory both derive "proj"
        # even though their full paths differ.
        root = tmp_path / "projects"
        (root / "a" / "proj" / "memory").mkdir(parents=True)
        (root / "proj" / "memory").mkdir(parents=True)

        data = _audit_json([], root)
        matches = [c for c in data["collision"] if c.get("project") == "proj"]
        assert matches, f"expected a 'proj' collision; got {data['collision']!r}"
        dirs = {str(Path(d).resolve()) for d in matches[0]["directories"]}
        assert str((root / "a" / "proj").resolve()) in dirs
        assert str((root / "proj").resolve()) in dirs

    def test_distinct_directories_not_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        (root / "widget" / "memory").mkdir(parents=True)
        (root / "gadget" / "memory").mkdir(parents=True)

        data = _audit_json([], root)
        assert data["collision"] == [], (
            f"two ordinary, non-colliding project directories must not be "
            f"reported; got {data['collision']!r}"
        )


# ---------------------------------------------------------------------------
# [depth-1/2/3] the collision scan's directory-walk depth cap.
#
# TWO DISTINCT COUNTS (per the coordinator's refinement of THE CONTRACT): a
# ROUTINE skip (a project directory's own subtree is never re-walked once its
# memory/ is found -- expected on every healthy run, not a warning) and a
# DEPTH-CAP prune (the real truncation, and it must stay loud). The key names
# for both are NOT specified anywhere upstream of this file, so they are
# discovered by keyword rather than hardcoded; what IS pinned is that they are
# two DIFFERENT fields, never one merged number.
# ---------------------------------------------------------------------------

_SKIP_KEYWORDS = ("skip",)
_DEPTH_CAP_KEYWORDS = ("prun", "depth", "cap")


def _discover_collision_depth_cap() -> int:
    """The collision scan's directory-walk depth cap, found BY NAME rather than
    hardcoded: THE CONTRACT for this cycle names only that a module-level
    constant exists, not what it is called."""
    import writ.cli as cli_mod

    candidates = {
        name: getattr(cli_mod, name)
        for name in dir(cli_mod)
        if "DEPTH" in name.upper()
        and isinstance(getattr(cli_mod, name), int)
        and not isinstance(getattr(cli_mod, name), bool)
    }
    assert candidates, (
        "expected a module-level int constant on writ.cli naming the "
        "collision scan's directory-walk depth cap (name containing 'DEPTH'); "
        f"found none among the module's uppercase names: "
        f"{[n for n in dir(cli_mod) if n.isupper()]!r}"
    )
    assert len(candidates) == 1, f"ambiguous depth-cap constants: {candidates!r}"
    return next(iter(candidates.values()))


def _nest_project_dir(root: Path, prefix: str, levels: int, leaf: str) -> None:
    """<root>/<prefix>0/<prefix>1/.../<prefix>{levels-1}/<leaf>/memory/ --
    `levels` directories of plain nesting above a directory named `leaf` that
    itself holds `memory/`."""
    target = root
    for i in range(levels):
        target = target / f"{prefix}{i}"
    (target / leaf / "memory").mkdir(parents=True)


def _find_count_by_single_keyword(data, keyword: str) -> tuple[str, int] | None:
    """Recursively search a --json report for a (key, value) pair whose key
    contains `keyword` (case-insensitively) and whose value is a plain int."""
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                keyword in key.lower()
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                return key, value
        for value in data.values():
            found = _find_count_by_single_keyword(value, keyword)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_count_by_single_keyword(item, keyword)
            if found is not None:
                return found
    return None


def _find_count_matching(data, keywords: tuple[str, ...]) -> tuple[str, int] | None:
    """Search a --json report for a (key, value) pair whose key contains one of
    `keywords`, without pinning the exact key name THE CONTRACT does not
    specify. Keywords are tried IN PRIORITY ORDER (an earlier keyword's match
    wins over a later one), so a narrower, more specific keyword such as
    "prun" is never shadowed by a broader fallback such as "depth" or "cap"
    that could also match an unrelated field -- e.g. the depth cap's
    CONFIGURED LIMIT (a plain int itself) rather than how many directories it
    actually pruned."""
    for keyword in keywords:
        found = _find_count_by_single_keyword(data, keyword)
        if found is not None:
            return found
    return None


class TestMemoryAuditCollisionDepthCap:
    """[depth-1/2/3] The collision scan's directory walk is depth-capped, and
    the cap must not be silent: a truncated scan that LOOKS complete is worse
    than no scan at all, so pruning must be stated, with a count, in both the
    text and the --json output -- separately from the routine memory/-found
    skip, so a real truncation can never hide behind a routine one."""

    def test_collision_deeper_than_cap_is_not_reported_and_prune_is_stated(
        self, tmp_path: Path
    ) -> None:
        cap = _discover_collision_depth_cap()
        root = tmp_path / "projects"
        root.mkdir()
        beyond = cap + 5
        _nest_project_dir(root, "branch-a-", beyond, "deepproj")
        _nest_project_dir(root, "branch-b-", beyond, "deepproj")

        data = _audit_json([], root)
        matches = [c for c in data["collision"] if c.get("project") == "deepproj"]
        assert matches == [], (
            f"a colliding pair nested past the depth cap ({cap}) must NOT be "
            f"reported -- the walk should stop descending before ever reaching "
            f"it; got {data['collision']!r}"
        )

        prune_found = _find_count_matching(data, _DEPTH_CAP_KEYWORDS)
        assert prune_found is not None, (
            f"a depth cap that silently truncates the scan is worse than no "
            f"scan at all -- the --json output must state HOW MANY "
            f"directories were pruned by the depth cap (a field naming the "
            f"prune, holding a plain int); got report keys={sorted(data)!r}, "
            f"report={data!r}"
        )
        _, prune_count = prune_found
        assert prune_count > 0, (
            f"the depth cap genuinely fired in this fixture, so its prune "
            f"count must be nonzero, not silently 0; got {prune_count}"
        )

        text_result = _invoke_audit([], root)
        assert text_result.exit_code == 0, text_result.output
        window = 40
        proximity = re.search(
            rf"(?i)prun.{{0,{window}}}{prune_count}\b|\b{prune_count}.{{0,{window}}}prun",
            text_result.output,
        )
        assert proximity, (
            f"text mode must ALSO state how many directories the depth cap "
            f"pruned (found {prune_count} via --json) -- not only in --json, "
            f"and not just that some were pruned without saying how many; "
            f"got output=\n{text_result.output}"
        )

    def test_collision_within_cap_is_still_reported(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        (root / "nested" / "shallowproj" / "memory").mkdir(parents=True)
        (root / "shallowproj" / "memory").mkdir(parents=True)

        data = _audit_json([], root)
        matches = [c for c in data["collision"] if c.get("project") == "shallowproj"]
        assert matches, (
            f"a colliding pair nested only one level deep is well within any "
            f"sane depth cap and must still be reported -- the cap bounds the "
            f"scan, it does not disable collision detection outright; got "
            f"{data['collision']!r}"
        )

    def test_routine_skip_and_depth_cap_prune_are_distinct_counts(
        self, tmp_path: Path
    ) -> None:
        cap = _discover_collision_depth_cap()
        root = tmp_path / "projects"
        root.mkdir()

        # ROUTINE: proj1's own subtree is never re-walked once its memory/ is
        # found there -- a project directory does not contain other project
        # directories, so `scratch` must be skipped as a matter of course, not
        # flagged as a truncation.
        (root / "proj1" / "memory").mkdir(parents=True)
        (root / "proj1" / "scratch" / "leftover").mkdir(parents=True)

        # GENUINE: a colliding pair nested past the depth cap.
        beyond = cap + 5
        _nest_project_dir(root, "branch-a-", beyond, "deepproj")
        _nest_project_dir(root, "branch-b-", beyond, "deepproj")

        data = _audit_json([], root)

        skip_found = _find_count_matching(data, _SKIP_KEYWORDS)
        prune_found = _find_count_matching(data, _DEPTH_CAP_KEYWORDS)
        assert skip_found is not None, (
            f"expected a field naming the ROUTINE skip count (a project "
            f"directory's own subtree is never re-walked once its memory/ is "
            f"found there); got report={data!r}"
        )
        assert prune_found is not None, (
            f"expected a field naming the DEPTH-CAP prune count, separate "
            f"from the routine skip count; got report={data!r}"
        )
        skip_key, skip_count = skip_found
        prune_key, prune_count = prune_found
        assert skip_key != prune_key, (
            f"the routine skip count and the depth-cap prune count must be "
            f"TWO DIFFERENT fields -- merging them back into one number is "
            f"exactly the regression this test guards against; both "
            f"resolved to {skip_key!r}"
        )
        assert skip_count > 0, (
            f"proj1's 'scratch' subtree must be counted as a routine skip; "
            f"got {skip_count}"
        )
        assert prune_count > 0, (
            f"the deepproj pair nested past the depth cap must be counted as "
            f"a depth-cap prune; got {prune_count}"
        )


# ---------------------------------------------------------------------------
# [audit-9] read-only, proven structurally
# ---------------------------------------------------------------------------

class _ReadOnlyProbeDB:
    """Defines ONLY list_all_memories -- no create/update/tombstone method exists
    at all, so any write attempt would raise AttributeError and fail this test
    with a non-zero exit code. Stronger than a before/after node count, which
    could miss an in-place property overwrite (exactly record_store.py's
    `create_memory`'s `MERGE ... SET m += $props` failure mode)."""

    def __init__(self, memories: list[dict]) -> None:
        self._memories = memories
        self.list_all_memories_calls = 0

    async def list_all_memories(self) -> list[dict]:
        self.list_all_memories_calls += 1
        return self._memories


class TestMemoryAuditIsReadOnly:
    def test_never_calls_anything_but_list_all_memories(self, tmp_path: Path) -> None:
        from contextlib import asynccontextmanager
        from typer.testing import CliRunner
        from unittest.mock import patch
        from writ.cli import app

        root = tmp_path / "projects"
        root.mkdir()
        probe = _ReadOnlyProbeDB([_row(project=""), _row()])

        @asynccontextmanager
        async def _fake_writ_db():
            yield probe

        runner = CliRunner()
        with patch("writ.cli._writ_db", new=_fake_writ_db):
            result = runner.invoke(
                app, ["memory", "audit", "--projects-root", str(root), "--json"]
            )

        assert result.exit_code == 0, (
            f"writ memory audit must call nothing on the db beyond "
            f"list_all_memories -- a write attempt raises AttributeError on this "
            f"stub; got exit_code={result.exit_code}\n{result.output}"
        )
        assert probe.list_all_memories_calls == 1, (
            "the audit must read the Memory population exactly once, not skip "
            "the db layer entirely"
        )
        data = json.loads(result.output)
        assert data["read_only"] is True
        assert data["repaired"] == 0


# ---------------------------------------------------------------------------
# [render-1] render_memory_audit_text -- pure function, called directly.
# ---------------------------------------------------------------------------

class TestRenderMemoryAuditTextUnit:
    def test_rendered_text_carries_per_bucket_counts(self) -> None:
        from writ.cli import render_memory_audit_text

        report = {
            "read_only": True,
            "repaired": 0,
            "examined": 7,
            "projects_root": "/tmp/wherever",
            "projects_root_present": True,
            "project_dirs_scanned": 5,
            "counts": {"mismatch": 2, "empty": 1, "disk_drift": 3, "collision": 4},
            "mismatch": [
                {"name": "m1", "project": "-p", "path": "x1", "derived_project": "-q"},
                {"name": "m2", "project": "-p", "path": "x2", "derived_project": "-r"},
            ],
            "empty": [
                {"name": "e1", "project": "", "path": "y1", "missing": ["project"]},
            ],
            "disk_drift": [
                {"name": "d1", "project": "-p", "path": "z1", "kind": "path_missing"},
                {"name": "d2", "project": "-p", "path": "z2", "kind": "path_missing"},
                {
                    "name": "d3", "project": "-p", "path": "z3",
                    "kind": "live_project_changed", "live_project": "-s",
                },
            ],
            "collision": [
                {"project": "c1", "directories": ["dir1", "dir2"]},
                {"project": "c2", "directories": ["dir3", "dir4"]},
                {"project": "c3", "directories": ["dir5", "dir6"]},
                {"project": "c4", "directories": ["dir7", "dir8"]},
            ],
        }

        text = render_memory_audit_text(report)
        assert isinstance(text, str) and text.strip(), (
            "render_memory_audit_text must return a non-empty string"
        )
        # Deliberately about the COUNTS, not exact prose (ordinary wording
        # changes must not break this): each bucket's number must appear near
        # that bucket's own name.
        for label, n in (("mismatch", 2), ("empty", 1), ("disk_drift", 3), ("collision", 4)):
            assert re.search(rf"(?i){label}\D{{0,6}}{n}\b", text), (
                f"expected the rendered text to state the {label} count "
                f"({n}) near the word {label!r}; got:\n{text}"
            )

    def test_rendered_text_carries_the_depth_cap_truncation_notice(
        self, tmp_path: Path
    ) -> None:
        # Built from a REAL report (produced by a real depth-cap-exceeding
        # directory tree) rather than a hand-built dict, so this test does not
        # have to guess the truncation field's name to exercise it -- it reads
        # back whatever key the report actually used.
        from writ.cli import render_memory_audit_text

        cap = _discover_collision_depth_cap()
        root = tmp_path / "projects"
        root.mkdir()
        beyond = cap + 5
        _nest_project_dir(root, "branch-a-", beyond, "deepproj")
        _nest_project_dir(root, "branch-b-", beyond, "deepproj")

        report = _audit_json([], root)
        prune_found = _find_count_matching(report, _DEPTH_CAP_KEYWORDS)
        assert prune_found is not None and prune_found[1] > 0, (
            f"this fixture must actually trigger the depth cap for the "
            f"truncation-notice assertion below to mean anything; got "
            f"report={report!r}"
        )
        _, prune_count = prune_found

        text = render_memory_audit_text(report)
        assert isinstance(text, str) and text.strip()
        window = 40
        assert re.search(
            rf"(?i)prun.{{0,{window}}}{prune_count}\b|\b{prune_count}.{{0,{window}}}prun",
            text,
        ), (
            f"render_memory_audit_text must state the truncation notice -- "
            f"HOW MANY directories the depth cap pruned ({prune_count}), not "
            f"just the per-bucket counts; got text=\n{text}"
        )


# ---------------------------------------------------------------------------
# [audit-10] --json shape / text-mode counts
# ---------------------------------------------------------------------------

class TestMemoryAuditOutputShape:
    def test_json_mode_emits_all_buckets_and_counts(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        data = _audit_json([_row()], root)
        assert data["read_only"] is True
        assert data["repaired"] == 0
        for key in ("mismatch", "empty", "disk_drift", "collision"):
            assert key in data and isinstance(data[key], list), (
                f"--json output missing list bucket {key!r}: keys={sorted(data)!r}"
            )
        assert data["counts"] == {
            "mismatch": len(data["mismatch"]),
            "empty": len(data["empty"]),
            "disk_drift": len(data["disk_drift"]),
            "collision": len(data["collision"]),
        }

    def test_text_mode_prints_counts_per_bucket(self, tmp_path: Path) -> None:
        root = tmp_path / "projects"
        root.mkdir()
        result = _invoke_audit([_row()], root)
        assert result.exit_code == 0, result.output
        assert "mismatch=" in result.output
        assert "empty=" in result.output
        assert "disk_drift=" in result.output
        assert "collision=" in result.output
        assert "examined=1" in result.output, result.output
        assert "READ-ONLY" in result.output, (
            "text mode must state plainly that nothing was modified"
        )


# ---------------------------------------------------------------------------
# [route-1/2] /memory-record friction on derived project
# ---------------------------------------------------------------------------

class TestMemoryRecordRouteDerivedProjectFriction:
    def test_logs_friction_event_when_project_derived_from_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient
        import writ.server as _srv
        from writ.server import app

        created = []

        class _FakeDB:
            async def create_memory(self, **kwargs):
                created.append(kwargs)
                return kwargs["name"]

        monkeypatch.setattr(_srv, "_db", _FakeDB())
        logged = []
        monkeypatch.setattr(
            _srv, "log_friction_event",
            lambda **kw: logged.append(kw), raising=False,
        )
        client = TestClient(app)
        path = "/home/u/.claude/projects/-derived-project/memory/x.md"
        resp = client.post("/memory-record", json={
            "project_root": "/home/u/project",
            "path": path,
            "name": "x", "description": "d", "type": "project", "body": "b",
            "links": [], "session_id": "s",
            # "project" deliberately omitted (Pydantic default "") -- the route
            # must derive it from `path` AND log that it had to.
        })
        assert resp.status_code == 200
        assert created and created[0]["project"] == "-derived-project", (
            f"create_memory must receive the path-derived project when the "
            f"caller sent none; got {created!r}"
        )
        derived_events = [e for e in logged if e.get("event") == "memory_project_derived"]
        assert derived_events, (
            f"/memory-record must log a memory_project_derived friction event "
            f"when the caller sent no project and one had to be derived from "
            f"path; got logged={logged!r}"
        )
        event = derived_events[0]
        assert event.get("derived_project") == "-derived-project"
        assert event.get("file_path") == path
        assert event.get("resolved") is True

    def test_logs_friction_event_with_resolved_false_when_nothing_derivable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # [route-2], the missing branch from review: the sibling test above only
        # covers resolved=True. A caller that sends no project AND a path with no
        # `memory` parent segment at all derives nothing -- the guess must still
        # get logged, because "we could not tell" is exactly the case an
        # operator most needs to see, not the one safe to skip logging for.
        from fastapi.testclient import TestClient
        import writ.server as _srv
        from writ.server import app

        created = []

        class _FakeDB:
            async def create_memory(self, **kwargs):
                created.append(kwargs)
                return kwargs["name"]

        monkeypatch.setattr(_srv, "_db", _FakeDB())
        logged = []
        monkeypatch.setattr(
            _srv, "log_friction_event",
            lambda **kw: logged.append(kw), raising=False,
        )
        client = TestClient(app)
        # No "memory" parent segment anywhere in this path (unlike the sibling
        # test's path), so the fallback derivation resolves to "".
        path = "/home/u/project/notes/x.md"
        resp = client.post("/memory-record", json={
            "project_root": "/home/u/project",
            "path": path,
            "name": "x", "description": "d", "type": "project", "body": "b",
            "links": [], "session_id": "s",
            # "project" deliberately omitted, same as the sibling test.
        })
        assert resp.status_code == 200
        assert created and created[0]["project"] == "", (
            f"create_memory must still be called fail-open with an empty "
            f"project when nothing could be derived; got {created!r}"
        )
        derived_events = [e for e in logged if e.get("event") == "memory_project_derived"]
        assert derived_events, (
            f"the friction event must fire even when derivation FAILS -- an "
            f"unresolved guess is exactly the case that most needs to be "
            f"auditable, not the one safe to skip logging for; got "
            f"logged={logged!r}"
        )
        event = derived_events[0]
        assert event.get("derived_project") == "", f"got {event.get('derived_project')!r}"
        assert event.get("file_path") == path
        assert event.get("resolved") is False, (
            f"resolved must be False when nothing could be derived; got "
            f"{event.get('resolved')!r}"
        )
