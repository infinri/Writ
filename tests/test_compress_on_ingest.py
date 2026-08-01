"""`import-markdown --compress`: opt-in abstraction regeneration on ingest.

DESIGN: the compression pipeline (graph-only `Abstraction` layer) is wired into
the ingest CLI behind an opt-in `--compress / --no-compress` flag (default OFF).
The flag exists so the materialized abstraction view can be regenerated as part
of a graph rebuild. It needs the `[fallback]` sentence-transformers dep; when
that dep is absent the step warns and continues (the ingest still succeeds).

- test_import_with_artifact_present_materializes: plain `import-markdown bible/
  --no-export` WITH a bible/abstractions.json present -> Abstraction nodes > 0
  and match the artifact contents. RED today (plain ingest currently makes 0
  abstractions).
- test_import_without_artifact_has_no_abstractions: plain ingest when no
  bible/abstractions.json is present (pointed at a tmp bible dir copy that lacks
  the file) -> 0 abstractions.
- test_import_with_compress_creates_abstractions: --compress recomputes;
  Abstraction nodes + ABSTRACTS edges must exist (unchanged green test).
- test_compress_flag_graceful_when_dep_missing: a missing dep is caught and
  surfaced as a non-fatal WARNING, ingest exit 0.

Artifact materialization path override assumption
--------------------------------------------------
The new artifact-based default materialization is triggered inside `import-markdown`
only when `bible/abstractions.json` exists (relative to the ingest path root) AND
`--compress` was NOT passed. To test the "no artifact" case without mutating the
committed file, tests point `import-markdown` at a tmp directory that contains the
same rule source but lacks the artifact file.

Live-Neo4j tests skip if the graph is unreachable (mirrors
tests/test_import_markdown_unified.py).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = Path(__file__).resolve().parent.parent

from tests._writ_cmd import WRIT_CMD_PREFIX as _WRIT_CMD_PREFIX

# Credentials via the central loader: env-independent defaults keep CI (no
# writ.toml checked out) collecting and running; a local writ.toml overrides.
from writ.config import get_neo4j_password, get_neo4j_user

NEO4J_PASSWORD = get_neo4j_password()
NEO4J_USER = get_neo4j_user()

# Minimal artifact for the "artifact present" materialization test.
# Uses two well-known methodology rule_ids that survive every clean ingest.
_FIXTURE_ARTIFACT: dict = {
    "project": "writ",
    "abstractions": [
        {
            "abstraction_id": "ABS-TESTING-INGEST-000",
            "summary": "Ingest artifact materialization test abstraction A",
            "rule_ids": ["ENF-COMMS-001", "ENF-PROC-BRAIN-001"],
            "compression_ratio": 2.0,
        },
        {
            "abstraction_id": "ABS-TESTING-INGEST-001",
            "summary": "Ingest artifact materialization test abstraction B",
            "rule_ids": ["ENF-COMMS-001"],
            "compression_ratio": 1.0,
        },
    ],
}


# ---------------------------------------------------------------------------
# helpers (shape mirrors tests/test_import_markdown_unified.py)
# ---------------------------------------------------------------------------

def _cypher(query: str) -> int:
    """Run a read-only Cypher query via docker exec; return the integer result."""
    result = subprocess.run(
        [
            "docker", "exec", "writ-neo4j", "cypher-shell",
            "-u", NEO4J_USER, "-p", NEO4J_PASSWORD,
            "--format", "plain",
            query,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"Neo4j not reachable: {result.stderr[:200]}")
    lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
    for line in reversed(lines):
        try:
            return int(line)
        except ValueError:
            continue
    pytest.skip(f"Could not parse cypher output: {result.stdout!r}")


def _run_import(*args: str, cwd: Path = SKILL_DIR) -> subprocess.CompletedProcess:
    """Run `writ import-markdown` with the given args; return the completed process."""
    return subprocess.run(
        [*_WRIT_CMD_PREFIX, "import-markdown", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=600,
    )


def _clear_graph() -> None:
    """Wipe the graph so each test starts clean."""
    result = subprocess.run(
        [
            "docker", "exec", "writ-neo4j", "cypher-shell",
            "-u", NEO4J_USER, "-p", NEO4J_PASSWORD,
            "--format", "plain",
            "MATCH (n) DETACH DELETE n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"Could not clear Neo4j: {result.stderr[:200]}")


# ---------------------------------------------------------------------------
# Live-graph behaviour
# ---------------------------------------------------------------------------

class TestImportMarkdownCompressFlag:

    @pytest.fixture(scope="class", autouse=True)
    def _restore_corpus_after_class(self):
        """Prune this class's graph pollution and restore the committed baseline.

        Each test in this class wipes the shared graph and seeds fake
        Abstraction nodes (ABS-TESTING-INGEST-000/001). As of #76 all three tests
        ingest a tmp copy of bible/, so --compress no longer rewrites the
        committed bible/abstractions.json; the git-checkout in (b) below is kept
        only as a harmless safety net against future re-introduction. Without a
        restore the graph junk leaks to whatever runs next. Mirror the
        pipeline_db / _roundtrip_db contract: after the class finishes wipe the
        whole graph, revert bible/abstractions.json (a no-op now), then re-import
        bible/ so the graph returns to the committed baseline (rules plus the
        committed abstractions) with the fake test nodes gone.

        The wipe runs raw Cypher directly, NOT _clear_graph(), because
        _clear_graph() can pytest.skip and skipping inside teardown is unsafe.
        """
        yield

        import sys  # noqa: PLC0415

        # (a) Best-effort whole-graph wipe -- never skips, never raises.
        try:
            subprocess.run(
                [
                    "docker", "exec", "writ-neo4j", "cypher-shell",
                    "-u", NEO4J_USER, "-p", NEO4J_PASSWORD,
                    "--format", "plain",
                    "MATCH (n) DETACH DELETE n",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            pass

        # (b) Revert the committed bible/abstractions.json that the --compress
        # test rewrote, so the reimport below rebuilds the true committed
        # baseline (not the dirtied artifact) and leaves the working tree clean.
        try:
            subprocess.run(
                ["git", "checkout", "--", "bible/abstractions.json"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (subprocess.SubprocessError, OSError):
            pass

        # (c) Rebuild the real corpus (mirrors the _roundtrip_db teardown).
        try:
            subprocess.run(
                [*_WRIT_CMD_PREFIX, "import-markdown", "bible/"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as e:
            sys.stderr.write(
                "[test_compress_on_ingest teardown] writ import-markdown "
                f"restore failed: {e}\n"
            )

    def test_import_with_artifact_present_materializes(self, tmp_path: Path) -> None:
        """Plain `import-markdown` WITH a bible/abstractions.json present
        materializes Abstraction nodes from the artifact (dep-free path).

        RED today: plain ingest currently yields 0 abstractions regardless of
        whether the artifact file exists.

        Strategy: copy bible/ to a tmp dir, inject the fixture artifact as
        abstractions.json, run import-markdown against the tmp dir, assert
        Abstraction nodes > 0 and match the fixture's abstraction_ids.
        """
        _clear_graph()

        # Copy the full bible/ source to a tmp directory.
        tmp_bible = tmp_path / "bible"
        shutil.copytree(str(REPO_ROOT / "bible"), str(tmp_bible))

        # Inject the fixture artifact (no real abstractions.json committed yet).
        artifact_path = tmp_bible / "abstractions.json"
        artifact_path.write_text(
            json.dumps(_FIXTURE_ARTIFACT, indent=2), encoding="utf-8"
        )

        # Plain ingest -- no --compress flag.
        result = _run_import(str(tmp_bible), "--no-export", cwd=REPO_ROOT)
        assert result.returncode == 0, (
            f"import-markdown {tmp_bible} --no-export failed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count > 0, "Expected Rule nodes after ingest"

        # RED: plain ingest currently produces 0 abstractions.
        abstraction_count = _cypher("MATCH (n:Abstraction) RETURN count(n)")
        assert abstraction_count > 0, (
            f"Expected Abstraction nodes from artifact materialization; "
            f"got {abstraction_count}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        # Verify the specific abstraction_ids from the fixture landed.
        for abs_id in ("ABS-TESTING-INGEST-000", "ABS-TESTING-INGEST-001"):
            count = _cypher(
                f"MATCH (a:Abstraction {{abstraction_id: '{abs_id}'}}) RETURN count(a)"
            )
            assert count == 1, (
                f"Expected Abstraction node {abs_id!r} from artifact; got count={count}"
            )

    def test_import_without_artifact_has_no_abstractions(self, tmp_path: Path) -> None:
        """Plain `import-markdown` with NO bible/abstractions.json present
        yields 0 Abstraction nodes (the original baseline is preserved when
        the artifact file does not exist).

        Strategy: copy bible/ to a tmp dir that explicitly lacks
        abstractions.json, run import-markdown against it, assert 0 abstractions.
        """
        _clear_graph()

        # Copy the full bible/ source to a tmp directory -- explicitly without any artifact.
        tmp_bible = tmp_path / "bible"
        shutil.copytree(str(REPO_ROOT / "bible"), str(tmp_bible))

        # Remove the artifact if it somehow exists in the copy.
        artifact_path = tmp_bible / "abstractions.json"
        if artifact_path.exists():
            artifact_path.unlink()

        result = _run_import(str(tmp_bible), "--no-export", cwd=REPO_ROOT)
        assert result.returncode == 0, (
            f"import-markdown failed without artifact:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count > 0, "Expected Rule nodes after ingest"

        abstraction_count = _cypher("MATCH (n:Abstraction) RETURN count(n)")
        assert abstraction_count == 0, (
            f"Expected 0 Abstraction nodes when no artifact present; "
            f"got {abstraction_count}."
        )

    def test_import_with_compress_creates_abstractions(self, tmp_path: Path) -> None:
        """--compress recomputes and materializes Abstraction nodes + ABSTRACTS edges
        (sentence-transformers is installed in .venv). This test is GREEN today and
        must stay green after the artifact design lands.

        Ingests a tmp COPY of bible/ (not the committed dir) so --compress writes
        its regenerated abstractions.json into the copy, never mutating the
        committed bible/abstractions.json (#76). Same copytree pattern as the two
        sibling tests above; the assertions are on graph state, so where the
        artifact is written does not affect them.
        """
        _clear_graph()
        tmp_bible = tmp_path / "bible"
        shutil.copytree(str(REPO_ROOT / "bible"), str(tmp_bible))
        result = _run_import(str(tmp_bible), "--no-export", "--compress", cwd=REPO_ROOT)
        assert result.returncode == 0, (
            f"import-markdown {tmp_bible} --no-export --compress failed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count > 0, "Expected Rule nodes after a --compress ingest"

        abstraction_count = _cypher("MATCH (n:Abstraction) RETURN count(n)")
        assert abstraction_count > 0, (
            f"Expected Abstraction nodes after --compress; got {abstraction_count}.\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        abstracts_edges = _cypher("MATCH ()-[e:ABSTRACTS]->() RETURN count(e)")
        assert abstracts_edges > 0, (
            f"Expected ABSTRACTS edges after --compress; got {abstracts_edges}"
        )


# ---------------------------------------------------------------------------
# Graceful degradation when the dep is missing (no live graph required)
# ---------------------------------------------------------------------------

class TestCompressGracefulWhenDepMissing:

    def test_compress_flag_graceful_when_dep_missing(self, monkeypatch) -> None:
        """When run_compression raises ImportError (sentence-transformers absent),
        import-markdown --compress must NOT fail: exit 0, warn, ingest still done.

        Driven in-process via Typer's CliRunner so the ImportError path is
        exercised deterministically without touching the .venv install."""
        from typer.testing import CliRunner

        from writ import cli

        ingest_calls: dict[str, int] = {"count": 0}

        class _FakeReport:
            errors: list = []
            counts_by_type = {"Rule": 5}

            def render(self) -> str:
                return "Imported 5 Rule nodes"

        async def _fake_ingest_path(path, db, only=None, dry_run=False):
            ingest_calls["count"] += 1
            return _FakeReport()

        class _FakeDB:
            def __init__(self, *a, **k) -> None:
                pass

            async def close(self) -> None:
                pass

        async def _raise_import_error(db, project="writ", artifact_path=None):
            raise ImportError("No module named 'sentence_transformers'")

        # Patch the ingest + DB so no live Neo4j is needed, and force the
        # compression step down the ImportError branch.
        import writ.graph.methodology_ingest as mi
        import writ.compression.abstractions as ab

        monkeypatch.setattr(mi, "ingest_path", _fake_ingest_path)
        monkeypatch.setattr(cli, "Neo4jConnection", _FakeDB, raising=False)
        monkeypatch.setattr(
            "writ.graph.db.Neo4jConnection", _FakeDB, raising=False
        )
        monkeypatch.setattr(ab, "run_compression", _raise_import_error)

        runner = CliRunner()
        res = runner.invoke(
            cli.app,
            ["import-markdown", "bible/", "--no-export", "--compress"],
        )

        assert res.exit_code == 0, (
            f"Missing dep must not fail the ingest; got exit {res.exit_code}.\n"
            f"output={res.output}"
        )
        assert ingest_calls["count"] == 1, "The ingest itself must still have run"
        assert "WARNING" in res.output, (
            f"Expected an actionable dep-missing WARNING; got:\n{res.output}"
        )
        assert "sentence-transformers" in res.output, (
            f"Expected the warning to name sentence-transformers; got:\n{res.output}"
        )

    def test_dep_error_message_helper_is_actionable(self) -> None:
        """The shared dep-missing message names the lib + the [fallback] install
        path, and the WARNING prefix is non-fatal-flavoured for the ingest path."""
        from writ.cli import _compress_dep_error_message

        msg = _compress_dep_error_message(
            "import-markdown --compress",
            ImportError("No module named 'sentence_transformers'"),
            prefix="WARNING",
        )
        assert msg.startswith("WARNING:")
        assert "sentence-transformers" in msg
        assert "[fallback]" in msg
        assert "pip install -e '.[fallback]'" in msg
