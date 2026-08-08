"""v1.5.0 -- Unified `writ import-markdown` CLI tests.

Covers the full behavior matrix introduced in v1.5.0:
- Default (no flags) imports every node type under bible/
- --only TYPE[,TYPE,...] filters to the named subset
- --dry-run parses + validates without DB writes
- Error reporting: structured IngestError (no raw Pydantic tracebacks)
- Edge creation alongside node creation
- Idempotency (MERGE semantics)
- scripts/migrate.py shim contract preservation
- Version-bump assertions

All tests FAIL until the implementation phase lands.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = (Path(__file__).resolve().parent.parent)
REPO_ROOT = Path(__file__).resolve().parent.parent

# Shared resolver -- one source of truth for invoking `writ` from tests.
from tests._writ_cmd import WRIT_CMD_PREFIX as _WRIT_CMD_PREFIX, WRIT_CLI

# Single source for the record labels a corpus wipe must spare.
from writ.graph.db._common import RECORD_LABELS

# Credentials via the central loader: env-independent defaults keep CI (no
# writ.toml checked out) collecting and running; a local writ.toml overrides.
from writ.config import get_neo4j_password, get_neo4j_uri, get_neo4j_user

from tests._bible_guard import requires_bible

pytestmark = requires_bible


NEO4J_PASSWORD = get_neo4j_password()
NEO4J_USER = get_neo4j_user()
NEO4J_URI = get_neo4j_uri()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# THE CONTAINER NAME IS A SEAM, not a constant. These two files reach Neo4j through
# `docker exec <container> cypher-shell` rather than through Neo4jConnection, so they are
# the one place WRIT_NEO4J_URI does NOT redirect: pointing the rest of the suite at a
# disposable instance would silently leave these hitting production. Reading the name from
# the environment puts them back under the same switch as everything else.
def _neo4j_container() -> str:
    return os.environ.get("WRIT_TEST_NEO4J_CONTAINER", "writ-neo4j")


def _cypher(query: str) -> int:
    """Run a read-only Cypher query via docker exec and return the integer result."""
    result = subprocess.run(
        [
            "docker", "exec", _neo4j_container(), "cypher-shell",
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
    """Run `writ import-markdown` with the given args and return the completed process."""
    return subprocess.run(
        [*_WRIT_CMD_PREFIX, "import-markdown", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
    )


def _clear_graph() -> None:
    """Wipe the CORPUS so each test starts from a clean slate.

    Runtime records (Memory, Decision, FileChange, Commit) are spared, matching
    Neo4jConnection.clear_all's default. This ran as a raw whole-graph
    `MATCH (n) DETACH DELETE n` through cypher-shell, which bypassed that guard
    entirely and destroyed every mirrored memory on each suite run; the records
    have no dump home, so nothing that rebuilds the corpus should take them.
    A repo guard in tests/test_graph_dump.py fails on any new raw whole-graph
    delete outside clear_all.
    """
    preserve = ", ".join(f"'{label}'" for label in sorted(RECORD_LABELS))
    result = subprocess.run(
        [
            "docker", "exec", _neo4j_container(), "cypher-shell",
            "-u", NEO4J_USER, "-p", NEO4J_PASSWORD,
            "--format", "plain",
            f"MATCH (n) WHERE NOT any(l IN labels(n) WHERE l IN [{preserve}]) "
            "DETACH DELETE n",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        pytest.skip(f"Could not clear Neo4j: {result.stderr[:200]}")


# ---------------------------------------------------------------------------
# Class TestImportMarkdownDefaultBehavior
# ---------------------------------------------------------------------------

class TestImportMarkdownDefaultBehavior:
    """writ import-markdown (no flags) must import all node types."""

    def test_default_imports_everything_from_bible(self) -> None:
        """Run 'writ import-markdown bible/' and assert Rule + Skill + Playbook exist
        plus at least one methodology edge."""
        _clear_graph()
        result = _run_import("bible/")
        assert result.returncode == 0, (
            f"import-markdown bible/ failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count > 0, "Expected at least one Rule node after full import"

        skill_count = _cypher("MATCH (n:Skill) RETURN count(n)")
        assert skill_count > 0, "Expected at least one Skill node after full import"

        playbook_count = _cypher("MATCH (n:Playbook) RETURN count(n)")
        assert playbook_count > 0, "Expected at least one Playbook node after full import"

        edge_count = _cypher(
            "MATCH ()-[e]->() WHERE type(e) IN "
            "['TEACHES','GATES','PRECEDES','DEMONSTRATES','COUNTERS',"
            "'DISPATCHES','PRESSURE_TESTS','CONTAINS','ATTACHED_TO'] "
            "RETURN count(e)"
        )
        assert edge_count > 0, (
            "Expected at least one methodology edge (TEACHES/GATES/PRECEDES/...) "
            "after full import"
        )

    def test_default_no_path_defaults_to_bible(self) -> None:
        """'writ import-markdown' with no path arg must behave identically to
        'writ import-markdown bible/'."""
        _clear_graph()
        no_arg = _run_import()
        assert no_arg.returncode == 0, (
            f"import-markdown (no args) failed:\nstdout={no_arg.stdout}\nstderr={no_arg.stderr}"
        )
        count_no_arg = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert count_no_arg > 0, (
            "No Rule nodes after no-arg invocation; default path may not be 'bible/'"
        )

        skill_no_arg = _cypher("MATCH (n:Skill) RETURN count(n)")
        assert skill_no_arg > 0, (
            "No Skill nodes after no-arg invocation; default import should include methodology"
        )

    def test_default_reports_counts_per_node_type(self) -> None:
        """Stdout must include per-type count breakdown mentioning Rule, Skill, and Playbook."""
        result = _run_import("bible/")
        assert result.returncode == 0, (
            f"import-markdown failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        output = result.stdout + result.stderr

        # All three type names must appear.
        assert "Rule" in output, (
            f"Expected 'Rule' in import output; got:\n{output}"
        )
        assert "Skill" in output, (
            f"Expected 'Skill' in import output; got:\n{output}"
        )
        assert "Playbook" in output, (
            f"Expected 'Playbook' in import output; got:\n{output}"
        )

        # Each type name must appear near a digit on the same or adjacent line.
        for type_name in ("Rule", "Skill", "Playbook"):
            pattern = re.compile(
                rf"(?:{type_name}\D{{0,30}}\d|\d\D{{0,30}}{type_name})"
            )
            assert pattern.search(output), (
                f"Expected a numeric count near '{type_name}' in output; got:\n{output}"
            )


# ---------------------------------------------------------------------------
# Class TestImportMarkdownOnlyFilter
# ---------------------------------------------------------------------------

class TestImportMarkdownNoExport:
    """B5: --no-export suppresses the auto-export round-trip (the dominant per-test
    setup cost -- every full-corpus import otherwise rewrites 273 rule files back to
    disk). Setup/fixture imports only need the graph populated, not the source
    regenerated. Default behaviour still exports (production regenerates rules.md)."""

    def test_no_export_suppresses_export(self) -> None:
        _clear_graph()
        result = _run_import("bible/", "--no-export")
        assert result.returncode == 0, (
            f"import-markdown --no-export failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        # Graph is still fully populated...
        assert _cypher("MATCH (n:Rule) RETURN count(n)") > 0, "Rules must still import with --no-export"
        # ...but the export round-trip is skipped.
        assert "Exported" not in result.stdout, (
            f"--no-export must suppress the auto-export; stdout still reports it:\n{result.stdout}"
        )

    def test_default_still_exports(self) -> None:
        """Regression guard: a default full-root import must KEEP exporting rules.md
        (production relies on it). Pins that --no-export is opt-in, not the default."""
        _clear_graph()
        result = _run_import("bible/")
        assert result.returncode == 0, (
            f"import-markdown bible/ failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert "Exported" in result.stdout, (
            f"default import-markdown bible/ must auto-export; stdout:\n{result.stdout}"
        )


class TestImportMarkdownOnlyFilter:
    """--only TYPE[,TYPE,...] must restrict ingestion to the named types."""

    def test_only_rule_matches_old_behavior(self) -> None:
        """--only Rule imports Rule nodes only; no non-Rule nodes created."""
        _clear_graph()
        result = _run_import("bible/", "--only", "Rule")
        assert result.returncode == 0, (
            f"--only Rule failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count > 0, "Expected Rule nodes after --only Rule"

        # RUNTIME RECORDS ARE NOT PART OF THE CLAIM. `_clear_graph` deliberately preserves
        # Memory / Decision / Commit / FileChange -- they have no bible or dump source, so a
        # corpus rebuild that took them with it would destroy state nothing can restore.
        # This assertion is about what the IMPORTER created, and counting the survivors made
        # it fail with "got 191" on any machine that had ever recorded a memory.
        non_rule_count = _cypher(
            "MATCH (n) WHERE NOT n:Rule "
            "AND NOT (n:Memory OR n:Decision OR n:Commit OR n:FileChange) "
            "RETURN count(n)"
        )
        assert non_rule_count == 0, (
            f"Expected zero non-Rule corpus nodes after --only Rule; got {non_rule_count}"
        )

    def test_only_skill_imports_only_skills(self) -> None:
        """--only Skill imports Skill nodes only; no Rule / Playbook nodes."""
        _clear_graph()
        result = _run_import("bible/", "--only", "Skill")
        assert result.returncode == 0, (
            f"--only Skill failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        skill_count = _cypher("MATCH (n:Skill) RETURN count(n)")
        assert skill_count > 0, "Expected Skill nodes after --only Skill"

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count == 0, (
            f"Expected zero Rule nodes after --only Skill; got {rule_count}"
        )

        playbook_count = _cypher("MATCH (n:Playbook) RETURN count(n)")
        assert playbook_count == 0, (
            f"Expected zero Playbook nodes after --only Skill; got {playbook_count}"
        )

    def test_only_multiple_types_comma_separated(self) -> None:
        """--only Skill,Playbook creates only Skill and Playbook; no Rule or AntiPattern."""
        _clear_graph()
        result = _run_import("bible/", "--only", "Skill,Playbook")
        assert result.returncode == 0, (
            f"--only Skill,Playbook failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        skill_count = _cypher("MATCH (n:Skill) RETURN count(n)")
        assert skill_count > 0, "Expected Skill nodes after --only Skill,Playbook"

        playbook_count = _cypher("MATCH (n:Playbook) RETURN count(n)")
        assert playbook_count > 0, "Expected Playbook nodes after --only Skill,Playbook"

        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count == 0, (
            f"Expected zero Rule nodes after --only Skill,Playbook; got {rule_count}"
        )

        antipattern_count = _cypher("MATCH (n:AntiPattern) RETURN count(n)")
        assert antipattern_count == 0, (
            f"Expected zero AntiPattern nodes after --only Skill,Playbook; got {antipattern_count}"
        )

    def test_only_with_whitespace_in_csv(self) -> None:
        """--only 'Skill, Playbook' (space after comma) must be tolerated."""
        _clear_graph()
        # Pass as a single string with embedded space; Typer receives it as one arg value.
        result = _run_import("bible/", "--only", "Skill, Playbook")
        assert result.returncode == 0, (
            f"--only with space in CSV failed (code {result.returncode}):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        skill_count = _cypher("MATCH (n:Skill) RETURN count(n)")
        assert skill_count > 0, "Expected Skill nodes after --only 'Skill, Playbook'"

        playbook_count = _cypher("MATCH (n:Playbook) RETURN count(n)")
        assert playbook_count > 0, "Expected Playbook nodes after --only 'Skill, Playbook'"

    def test_only_unknown_type_errors_cleanly(self) -> None:
        """--only Garbage must exit non-zero, mention 'Garbage', name a valid type,
        and not print a Python traceback."""
        result = _run_import("bible/", "--only", "Garbage")
        assert result.returncode != 0, (
            "Expected non-zero exit for unknown --only type 'Garbage'"
        )
        output = result.stdout + result.stderr
        assert "Garbage" in output, (
            f"Expected the unknown type 'Garbage' to appear in error output; got:\n{output}"
        )
        # At least one valid type must be named so the user knows what to type.
        valid_types_present = any(
            t in output for t in ("Rule", "Skill", "Playbook", "AntiPattern", "Technique")
        )
        assert valid_types_present, (
            f"Expected at least one valid type name in error output; got:\n{output}"
        )
        assert "Traceback (most recent call last):" not in output, (
            "Expected no raw Python traceback in output for unknown --only type"
        )

    def test_only_known_with_one_unknown_errors(self) -> None:
        """--only Skill,BogusType must exit non-zero and mention 'BogusType'."""
        result = _run_import("bible/", "--only", "Skill,BogusType")
        assert result.returncode != 0, (
            "Expected non-zero exit when one type in --only is unknown"
        )
        output = result.stdout + result.stderr
        assert "BogusType" in output, (
            f"Expected 'BogusType' to appear in error output; got:\n{output}"
        )


# ---------------------------------------------------------------------------
# Class TestImportMarkdownDryRun
# ---------------------------------------------------------------------------

class TestImportMarkdownDryRun:
    """--dry-run must parse + validate without writing to Neo4j."""

    def test_dry_run_no_writes(self) -> None:
        """Rule count in Neo4j must be identical before and after --dry-run."""
        before = _cypher("MATCH (n:Rule) RETURN count(n)")
        result = _run_import("bible/", "--dry-run")
        assert result.returncode == 0, (
            f"--dry-run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        after = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert before == after, (
            f"Rule count changed during --dry-run: before={before}, after={after}"
        )

    def test_dry_run_reports_what_would_be_imported(self) -> None:
        """--dry-run stdout must announce dry-run mode and include per-type counts."""
        result = _run_import("bible/", "--dry-run")
        assert result.returncode == 0, (
            f"--dry-run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        output = (result.stdout + result.stderr).lower()
        announced = (
            "dry" in output
            or "would import" in output
            or "validate only" in output
        )
        assert announced, (
            "Expected --dry-run to announce dry-run mode in output; got:\n"
            + result.stdout + result.stderr
        )
        # At least one node type name with a digit should appear.
        full_output = result.stdout + result.stderr
        has_count = re.search(r"(Rule|Skill|Playbook)\D{0,30}\d", full_output)
        assert has_count, (
            "Expected per-type count in --dry-run output; got:\n" + full_output
        )

    def test_dry_run_combined_with_only(self) -> None:
        """--only Skill --dry-run must not write AND report only Skill counts."""
        before = _cypher("MATCH (n:Skill) RETURN count(n)")
        result = _run_import("bible/", "--only", "Skill", "--dry-run")
        assert result.returncode == 0, (
            f"--only Skill --dry-run failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        after = _cypher("MATCH (n:Skill) RETURN count(n)")
        assert before == after, (
            f"Skill count changed during --only Skill --dry-run: before={before}, after={after}"
        )
        output = result.stdout + result.stderr
        assert "Skill" in output, (
            "Expected 'Skill' in --only Skill --dry-run output; got:\n" + output
        )
        # Rule must NOT appear as an ingested type since --only Skill was set.
        # (It may appear as "0 Rule" or not at all; either is acceptable, but
        # if it appears with a positive count that is a bug.)
        rule_count_in_report = re.search(r"Rule\D{0,20}([1-9]\d*)", output)
        assert rule_count_in_report is None, (
            f"Expected no positive Rule count in --only Skill --dry-run report; got:\n{output}"
        )


# ---------------------------------------------------------------------------
# Class TestImportMarkdownErrorReporting
# ---------------------------------------------------------------------------

class TestImportMarkdownErrorReporting:
    """Validation errors must surface with file path + field name; no raw tracebacks."""

    def test_validation_error_includes_file_path(self, tmp_path: Path) -> None:
        """A methodology YAML with staleness_window: P6M (string) must produce
        an error that names the file path and 'staleness_window', with no raw
        pydantic_core ValidationError substring."""
        bad_md = tmp_path / "BAD-SKL-001.md"
        bad_md.write_text(
            "---\n"
            "node_type: Skill\n"
            "skill_id: BAD-SKL-001\n"
            "name: Bad Skill\n"
            "staleness_window: P6M\n"
            "---\n"
            "# Bad Skill\n"
            "Content here.\n",
            encoding="utf-8",
        )
        result = _run_import(str(tmp_path))
        output = result.stdout + result.stderr

        assert str(bad_md) in output or bad_md.name in output, (
            f"Expected the file path or name in error output; got:\n{output}"
        )
        assert "staleness_window" in output, (
            f"Expected 'staleness_window' field name in error output; got:\n{output}"
        )
        assert "pydantic_core._pydantic_core.ValidationError" not in output, (
            "Expected no raw pydantic_core.ValidationError in output (API-ERROR-002)"
        )

    def test_validation_error_does_not_abort_other_files(self, tmp_path: Path) -> None:
        """With one valid and one invalid file, the valid file is ingested,
        the invalid one is reported, and the run does not silently drop both."""
        good_md = tmp_path / "SKL-TEST-PARTIAL-001.md"
        good_md.write_text(
            "---\n"
            "node_type: Skill\n"
            "skill_id: SKL-TEST-PARTIAL-001\n"
            "name: Good Test Skill\n"
            "domain: test\n"
            "severity: low\n"
            "scope: session\n"
            "trigger: \"Test trigger for partial-success ingestion.\"\n"
            "statement: \"Test statement for partial-success ingestion.\"\n"
            "rationale: \"Test rationale for partial-success ingestion.\"\n"
            "last_validated: 2026-05-21\n"
            "staleness_window: 365\n"
            "---\n"
            "# Good Skill\n"
            "Content here.\n",
            encoding="utf-8",
        )
        bad_md = tmp_path / "SKL-TEST-PARTIAL-002.md"
        bad_md.write_text(
            "---\n"
            "node_type: Skill\n"
            "skill_id: SKL-TEST-PARTIAL-002\n"
            "name: Bad Skill\n"
            "domain: test\n"
            "severity: low\n"
            "scope: session\n"
            "trigger: \"Test trigger.\"\n"
            "statement: \"Test statement.\"\n"
            "rationale: \"Test rationale.\"\n"
            "last_validated: 2026-05-21\n"
            "staleness_window: P6M\n"
            "---\n"
            "# Bad Skill\n"
            "Content here.\n",
            encoding="utf-8",
        )

        result = _run_import(str(tmp_path))
        output = result.stdout + result.stderr

        # The run should exit non-zero (partial failure) OR 0 if partial success is
        # acceptable; either way it must mention both files.
        assert good_md.name in output or "SKL-TEST-PARTIAL-001" in output, (
            f"Expected the valid file or its ID in output; got:\n{output}"
        )
        assert bad_md.name in output or "SKL-TEST-PARTIAL-002" in output, (
            f"Expected the invalid file or its ID in output; got:\n{output}"
        )

        # The valid skill must have been ingested.
        skill_count = _cypher(
            "MATCH (n:Skill {skill_id: 'SKL-TEST-PARTIAL-001'}) RETURN count(n)"
        )
        assert skill_count > 0, (
            "Expected SKL-TEST-PARTIAL-001 to be ingested despite co-existing invalid file"
        )


# ---------------------------------------------------------------------------
# Class TestImportMarkdownEdgeCases
# ---------------------------------------------------------------------------

class TestImportMarkdownEdgeCases:
    """Edge creation, idempotency, and path-scoping."""

    def test_edges_created_alongside_methodology_nodes(self) -> None:
        """After a full import, at least one TEACHES edge must exist."""
        _clear_graph()
        result = _run_import("bible/")
        assert result.returncode == 0, (
            f"Full import failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        teaches_count = _cypher("MATCH ()-[e:TEACHES]->() RETURN count(e)")
        assert teaches_count > 0, (
            "Expected at least one TEACHES edge after full import; "
            "edge creation pipeline may be broken"
        )

    def test_rerun_is_idempotent(self) -> None:
        """Running import twice must not duplicate nodes (MERGE semantics)."""
        _clear_graph()
        _run_import("bible/")
        rule_count_first = _cypher("MATCH (n:Rule) RETURN count(n)")
        skill_count_first = _cypher("MATCH (n:Skill) RETURN count(n)")

        _run_import("bible/")
        rule_count_second = _cypher("MATCH (n:Rule) RETURN count(n)")
        skill_count_second = _cypher("MATCH (n:Skill) RETURN count(n)")

        assert rule_count_first == rule_count_second, (
            f"Rule count changed on re-import: first={rule_count_first}, "
            f"second={rule_count_second}; MERGE semantics violated"
        )
        assert skill_count_first == skill_count_second, (
            f"Skill count changed on re-import: first={skill_count_first}, "
            f"second={skill_count_second}; MERGE semantics violated"
        )

    def test_path_argument_can_be_subdirectory(self) -> None:
        """'writ import-markdown bible/methodology' must create methodology nodes
        and the Rule nodes that legitimately live in bible/methodology/ (the
        ENF-PROC-* enforcement rules), but must NOT pull in Rule nodes from
        other bible subdirectories like bible/security/."""
        _clear_graph()
        # Count Rule files actually under bible/methodology/ via YAML front-matter.
        # Keep this dynamic so the assertion tracks the on-disk corpus.
        methodology_rule_files = sorted(
            p for p in (SKILL_DIR / "bible" / "methodology").glob("*.md")
            if p.read_text(encoding="utf-8").splitlines()[:15].__iter__()
            and any(
                line.startswith("rule_id:")
                for line in p.read_text(encoding="utf-8").splitlines()[:15]
            )
        )
        expected_methodology_rules = len(methodology_rule_files)

        result = _run_import("bible/methodology")
        assert result.returncode == 0, (
            f"import-markdown bible/methodology failed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        methodology_count = _cypher(
            "MATCH (n) WHERE n:Skill OR n:Playbook OR n:AntiPattern "
            "OR n:Technique OR n:Phase RETURN count(n)"
        )
        assert methodology_count > 0, (
            "Expected methodology nodes after targeting bible/methodology"
        )

        # Rule count must equal exactly the number of Rule files under
        # bible/methodology/ (subdirectory scoping must not pull in security,
        # architecture, etc.). Currently this is the 10 ENF-PROC-*
        # enforcement rules attached to methodology nodes.
        rule_count = _cypher("MATCH (n:Rule) RETURN count(n)")
        assert rule_count == expected_methodology_rules, (
            f"Expected exactly {expected_methodology_rules} Rule nodes (the "
            f"Rule files actually under bible/methodology/); got {rule_count}"
        )

        # Spot-check: a known non-methodology rule (security) must NOT be
        # present, proving subdirectory scoping was honored.
        sec_rule_count = _cypher(
            "MATCH (n:Rule {rule_id: 'SEC-AUTH-MFA-001'}) RETURN count(n)"
        )
        assert sec_rule_count == 0, (
            "Expected SEC-AUTH-MFA-001 (security rule) to NOT be ingested when "
            "targeting bible/methodology only; subdirectory scoping broken"
        )

    def test_subdir_import_does_not_trigger_full_graph_auto_export(self) -> None:
        """Subdirectory imports must skip the auto-export step.

        Regression: prior to the fix, `writ import-markdown bible/methodology/`
        would auto-export the WHOLE graph through a file-location lookup that
        only scanned the imported subdir. Rules whose original files lived
        outside scope (e.g. process domain in `bible/process/rules.md`) fell
        through to `<output_dir>/<domain>/rules.md`, creating bogus duplicates
        like `bible/methodology/process/rules.md`. Fixed by gating auto-export
        on `path.resolve() == DEFAULT_BIBLE_DIR.resolve()`.
        """
        methodology_dir = SKILL_DIR / "bible" / "methodology"
        # Snapshot existing top-level direct children so we can detect new ones.
        before = {p.name for p in methodology_dir.iterdir()}

        result = _run_import("bible/methodology")
        assert result.returncode == 0, (
            f"import-markdown bible/methodology failed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        after = {p.name for p in methodology_dir.iterdir()}
        new_entries = after - before
        # If any new directory appears under bible/methodology/ containing a
        # rules.md, that is the regression: the auto-export wrote the full
        # graph into the subdir scope.
        bogus_subdirs = {
            name for name in new_entries
            if (methodology_dir / name).is_dir()
            and (methodology_dir / name / "rules.md").exists()
        }
        assert not bogus_subdirs, (
            f"Auto-export wrote bogus domain subdirs under bible/methodology/: "
            f"{sorted(bogus_subdirs)}. Auto-export must not fire on subdirectory "
            f"imports."
        )
        # Also confirm the methodology-scope .export_timestamp was not written.
        meth_ts = methodology_dir / ".export_timestamp"
        assert not meth_ts.exists() or meth_ts.name in before, (
            f"Auto-export wrote {meth_ts} on a subdirectory import; "
            f"the export-timestamp should only appear at the bible/ root."
        )

    def test_default_root_import_still_triggers_auto_export(self) -> None:
        """The fix must NOT break the full-bible-root case.

        Running `writ import-markdown bible/` (the default) should still
        produce an export-timestamp at bible/.export_timestamp, because that
        is a true round-trip from the canonical source.
        """
        bible_dir = SKILL_DIR / "bible"
        ts_path = bible_dir / ".export_timestamp"
        before_mtime = ts_path.stat().st_mtime if ts_path.exists() else 0.0

        result = _run_import("bible/")
        assert result.returncode == 0, (
            f"import-markdown bible/ failed:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )

        assert ts_path.exists(), (
            "bible/.export_timestamp must exist after a default-root import "
            "(auto-export should have fired)"
        )
        assert ts_path.stat().st_mtime >= before_mtime, (
            "bible/.export_timestamp mtime did not advance after default-root "
            "import; auto-export likely did not fire"
        )


# ---------------------------------------------------------------------------
# Class TestMigrateScriptShimContract
# ---------------------------------------------------------------------------

class TestMigrateScriptShimContract:
    """scripts/migrate.py must remain importable and callable as a thin shim."""

    def test_migrate_script_still_imports(self) -> None:
        """'import scripts.migrate' must succeed after the shim refactor."""
        import importlib
        import sys

        # Ensure the skill root is on sys.path for the import.
        skill_root = str(SKILL_DIR)
        inserted = False
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
            inserted = True
        try:
            mod = importlib.import_module("scripts.migrate")
            assert mod is not None, "scripts.migrate imported as None"
        finally:
            if inserted:
                sys.path.remove(skill_root)

    def test_migrate_script_run_migration_callable(self) -> None:
        """scripts.migrate.run_migration must be a callable (shim re-export contract)."""
        import importlib
        import sys

        skill_root = str(SKILL_DIR)
        inserted = False
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
            inserted = True
        try:
            mod = importlib.import_module("scripts.migrate")
            assert callable(getattr(mod, "run_migration", None)), (
                "scripts.migrate.run_migration is missing or not callable"
            )
        finally:
            if inserted:
                sys.path.remove(skill_root)

    def test_migrate_script_run_methodology_migration_callable(self) -> None:
        """scripts.migrate.run_methodology_migration must be callable."""
        import importlib
        import sys

        skill_root = str(SKILL_DIR)
        inserted = False
        if skill_root not in sys.path:
            sys.path.insert(0, skill_root)
            inserted = True
        try:
            mod = importlib.import_module("scripts.migrate")
            assert callable(getattr(mod, "run_methodology_migration", None)), (
                "scripts.migrate.run_methodology_migration is missing or not callable"
            )
        finally:
            if inserted:
                sys.path.remove(skill_root)

    def test_migrate_script_shim_is_small(self) -> None:
        """scripts/migrate.py must be under 80 lines (regression guard against
        re-accumulation of duplicated logic)."""
        migrate_path = SKILL_DIR / "scripts" / "migrate.py"
        assert migrate_path.exists(), f"scripts/migrate.py not found at {migrate_path}"
        lines = migrate_path.read_text(encoding="utf-8").splitlines()
        line_count = len(lines)
        assert line_count < 80, (
            f"scripts/migrate.py has {line_count} lines; expected < 80. "
            "The shim may have re-accumulated duplicated logic."
        )


# Version-string agreement across pyproject.toml, plugin.json and marketplace.json is
# owned by tests/test_version_consistency.py, which keeps the expected version in a single
# EXPECTED_VERSION constant. A release-pinned duplicate lived here (TestVersionBumpedTo150,
# hardcoding "1.5.0") and meant every version bump had to be applied in two test files;
# removed in v1.5.1 so there is one place to change.



class TestPostWriteVerification:
    """An ingest that reports success must have proven it, not assumed it.

    counts_by_type counts PARSED nodes and Neo4j's properties_set counter reports the
    SET operation (identical on a no-op re-run), so neither distinguishes an applied
    edit from a silent stale apply -- one observed run printed a full success report
    while the node kept its old values. The importer now reads the nodes back.
    """

    def test_report_renders_verified_count_on_a_clean_write(self) -> None:
        from writ.graph.methodology_ingest import IngestReport

        report = IngestReport(counts_by_type={"Skill": 16}, verification=(16, []))
        out = report.render()
        assert "Verified against source after write: 16 node(s)" in out
        assert "VERIFY FAILED" not in out

    def test_report_shouts_and_names_ids_on_a_stale_apply(self) -> None:
        from writ.graph.methodology_ingest import IngestReport

        report = IngestReport(
            counts_by_type={"Skill": 16},
            verification=(15, ["SKL-PROC-EXAMPLE-001"]),
        )
        out = report.render()
        assert "VERIFY FAILED: 1 node(s) did not match source" in out
        assert "SKL-PROC-EXAMPLE-001" in out
        assert "Re-run the import" in out

    def test_dry_run_reports_no_verification(self) -> None:
        from writ.graph.methodology_ingest import IngestReport

        out = IngestReport(counts_by_type={"Skill": 1}, dry_run=True).render()
        assert "Verified against source" not in out and "VERIFY FAILED" not in out

    @pytest.mark.asyncio
    async def test_verifier_flags_a_node_whose_graph_value_differs(self) -> None:
        # The stale-apply shape: the expectation says one thing, the graph holds another.
        from writ.graph.db import Neo4jConnection
        from writ.graph.methodology_ingest import _verify_written_nodes

        db = Neo4jConnection(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        try:
            await db.create_methodology_node(
                "Skill", {"skill_id": "SKL-VERIFY-TEST-001", "project": "writ",
                          "statement": "current", "last_validated": "2026-08-06"})
            stale = [("Skill", {"skill_id": "SKL-VERIFY-TEST-001", "project": "writ",
                                "statement": "stale expectation"}, {})]
            verified, mismatched = await _verify_written_nodes(db, stale)
            assert verified == 0 and mismatched == ["SKL-VERIFY-TEST-001"]

            fresh = [("Skill", {"skill_id": "SKL-VERIFY-TEST-001", "project": "writ",
                                "statement": "current"}, {})]
            verified, mismatched = await _verify_written_nodes(db, fresh)
            assert verified == 1 and mismatched == []
        finally:
            async with db._driver.session(database=db._database) as s:
                await s.run("MATCH (n:Skill {skill_id:'SKL-VERIFY-TEST-001'}) DETACH DELETE n")
            await db.close()
