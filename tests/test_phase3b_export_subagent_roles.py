"""Phase 3b: scripts/export_subagent_roles.py round-trips the graph to files.

Plan Section 8.1 deliverable 2: graph is canonical for subagent prompts;
.claude/agents/*.md files regenerate from SubagentRole nodes. This test
verifies the render function is byte-stable and that the check mode
detects drift.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
EXPORT_SCRIPT = WRIT_ROOT / "scripts" / "export_subagent_roles.py"
AGENTS_DIR = WRIT_ROOT / ".claude" / "agents"


def _load_export_module():
    spec = importlib.util.spec_from_file_location("export_subagent_roles", EXPORT_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["export_subagent_roles"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestRenderAgentMd:
    """render_agent_md produces the canonical .md format from a node row."""

    def setup_method(self) -> None:
        self.render = _load_export_module().render_agent_md

    def test_basic_render_has_front_matter(self) -> None:
        row = {
            "name": "writ-example",
            "description": "An example agent.",
            "model_preference": "sonnet",
            "tools": "Read Glob",
            "prompt_template": "You are an example.",
        }
        out = self.render(row)
        assert out.startswith("---\n")
        assert "\nname: writ-example\n" in out
        assert "\ndescription: An example agent.\n" in out
        assert "\nmodel: sonnet\n" in out
        assert "\ntools: Read Glob\n" in out
        assert "\nYou are an example.\n" in out

    def test_missing_optional_fields_omitted(self) -> None:
        row = {
            "name": "writ-bare",
            "description": "Bare agent.",
            "model_preference": None,
            "tools": None,
            "prompt_template": "Body.",
        }
        out = self.render(row)
        assert "model:" not in out
        assert "tools:" not in out
        assert "description: Bare agent." in out

    def test_statement_fallback_when_description_missing(self) -> None:
        """Older nodes might only have statement; description falls back to it."""
        row = {
            "name": "writ-fallback",
            "description": None,
            "statement": "Legacy statement.",
            "model_preference": "haiku",
            "tools": None,
            "prompt_template": "Body.",
        }
        out = self.render(row)
        assert "description: Legacy statement." in out


class TestExportCheckMode:
    """--check verifies existing files match graph; exit 0 if clean."""

    def test_export_check_passes_after_ingest(self) -> None:
        """After ingest, --check must report clean. If it fails, the Neo4j
        fixture is unavailable -- skip rather than fail the suite."""
        proc = subprocess.run(
            [".venv/bin/python", str(EXPORT_SCRIPT), "--check"],
            capture_output=True, text=True, cwd=str(WRIT_ROOT),
        )
        if proc.returncode != 0 and "No SubagentRole nodes" in proc.stderr:
            pytest.skip("SubagentRole nodes not present in Neo4j (fixture unavailable)")
        if proc.returncode != 0 and "refused" in proc.stderr.lower():
            pytest.skip("Neo4j not reachable")
        assert proc.returncode == 0, f"Drift detected: {proc.stdout}\n{proc.stderr}"


class TestExportDryRun:
    """--dry-run prints planned writes without touching files."""

    def test_dry_run_does_not_modify_files(self, tmp_path: Path) -> None:
        before = {p.name: p.read_text() for p in AGENTS_DIR.glob("*.md")}
        proc = subprocess.run(
            [".venv/bin/python", str(EXPORT_SCRIPT), "--dry-run"],
            capture_output=True, text=True, cwd=str(WRIT_ROOT),
        )
        if proc.returncode != 0 and ("No SubagentRole" in proc.stderr or "refused" in proc.stderr.lower()):
            pytest.skip("Neo4j fixture unavailable")
        assert proc.returncode == 0
        assert "DRY RUN" in proc.stdout
        after = {p.name: p.read_text() for p in AGENTS_DIR.glob("*.md")}
        assert before == after, "--dry-run must not modify files"
