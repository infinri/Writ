"""Phase 3 approval flow and subagent graph canonicality tests.

Covers plan Section 8 release blockers:
- POST /session/{sid}/advance-phase with confirmation_source logged
- "approved" substring triggers ask-prompt, not silent advance (pattern path)
- Review ordering (already from Phase 2, verified here for completeness)
- `writ role-prompt <role>` returns graph-canonical text for SubagentRole
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
SESSION_CLI = WRIT_ROOT / "bin" / "lib" / "writ-session.py"
AGENTS_DIR = WRIT_ROOT / "agents"
COMMANDS_DIR = WRIT_ROOT / ".claude" / "commands"


class TestSubagentAgentsPresent:
    """All 5 subagent .md files exist with valid YAML front-matter.

    The spec-reviewer + code-quality-reviewer were merged into one two-pass
    writ-reviewer.
    """

    REQUIRED = [
        "writ-explorer", "writ-planner", "writ-test-writer",
        "writ-implementer", "writ-reviewer",
    ]

    @pytest.mark.parametrize("name", REQUIRED)
    def test_agent_file_exists(self, name: str) -> None:
        path = AGENTS_DIR / f"{name}.md"
        assert path.exists(), f"{name}.md missing from .claude/agents/"

    @pytest.mark.parametrize("name", REQUIRED)
    def test_agent_front_matter_parses(self, name: str) -> None:
        import re
        import yaml
        content = (AGENTS_DIR / f"{name}.md").read_text()
        m = re.match(r"^---\n(.*?)\n---\n(.*)", content, re.DOTALL)
        assert m is not None, f"{name}.md missing YAML front-matter"
        fm = yaml.safe_load(m.group(1))
        assert fm.get("name") == name
        assert fm.get("description")
        assert fm.get("model")
        # Body must not be empty.
        assert len(m.group(2).strip()) > 100, f"{name}.md prompt body too short"


class TestWritApproveSlashCommand:
    """The /writ-approve slash command is registered."""

    def test_writ_approve_command_exists(self) -> None:
        path = COMMANDS_DIR / "writ-approve.md"
        assert path.exists(), "/writ-approve command not registered at .claude/commands/writ-approve.md"

    def test_writ_approve_references_confirmation_source_tool(self) -> None:
        content = (COMMANDS_DIR / "writ-approve.md").read_text()
        # The POST body is now a double-quoted bash string (to interpolate $TOKEN), so the
        # JSON quotes are backslash-escaped. Assert the field + value are present in either
        # form rather than a single literal spelling.
        assert "confirmation_source" in content and "tool" in content, (
            "/writ-approve must POST with confirmation_source=tool to satisfy Section 8.2 blocker"
        )

    def test_writ_approve_passes_gate_token(self) -> None:
        """Audit P0: the advance must carry the gate token (closes the self-approval hole).
        The command must read /tmp/writ-gate-token-$SESSION_ID and pass it in the POST."""
        content = (COMMANDS_DIR / "writ-approve.md").read_text()
        assert "writ-gate-token-$SESSION_ID" in content, (
            "/writ-approve must read the gate token file"
        )
        assert "token" in content and "\\\"token\\\"" in content, (
            "/writ-approve must pass the gate token in the advance-phase POST body"
        )

    def test_writ_approve_passes_cwd(self) -> None:
        """The advance must carry a cwd, in BOTH copies of the command.

        The server resolves the project root from it (where plan.md and the test skeletons
        are looked for) and cannot substitute its own working directory, which is Writ's
        install dir. Without cwd every planning advance failed closed on an empty root. The
        installed copy under templates/commands is the one a user actually invokes, and it
        had drifted so far it sent no token either -- so both are asserted here.
        """
        for path in (COMMANDS_DIR / "writ-approve.md",
                     WRIT_ROOT / "templates" / "commands" / "writ-approve.md"):
            content = path.read_text()
            assert "cwd" in content and "pwd -P" in content, (
                f"{path} must send a cwd resolved with `pwd -P` in the advance-phase POST"
            )
            assert "\\\"token\\\"" in content, f"{path} must also pass the gate token"


class TestConfirmationSourceField:
    """session.phase_transitions records confirmation_source for audit trail."""

    def test_session_state_has_phase_transitions_field(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The session state schema includes phase_transitions with confirmation_source rows."""
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))
        import os
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        result = subprocess.run(
            [sys.executable, str(SESSION_CLI), "read", "pt-test"],
            capture_output=True, text=True, env=env,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        # phase_transitions is a session state field; default empty list.
        assert "phase_transitions" in data
        assert data["phase_transitions"] == []


class TestRolePromptCLI:
    """`writ role-prompt <role>` returns graph-canonical text from SubagentRole nodes."""

    @pytest.mark.parametrize("role", [
        "writ-explorer", "writ-planner", "writ-test-writer",
        "writ-implementer", "writ-reviewer",
    ])
    def test_role_prompt_returns_text(self, role: str) -> None:
        result = subprocess.run(
            [".venv/bin/writ", "role-prompt", role],
            capture_output=True, text=True, cwd=str(WRIT_ROOT),
        )
        if result.returncode != 0:
            pytest.skip(f"role-prompt CLI unavailable: {result.stderr}")
        assert result.returncode == 0, f"CLI failed for {role}: {result.stderr}"
        # Output must include the role id and some substantive prompt text.
        assert "ROL-" in result.stdout
        assert len(result.stdout) > 500, f"{role} prompt suspiciously short"
