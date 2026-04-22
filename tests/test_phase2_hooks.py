"""Phase 2 hooks: mode-scope, executability, and core behaviors.

Six enforcement hooks + one quality-judge hook introduced in Phase 2.
Feature-flag-free: hooks are gated at install time via settings.json
registration. At runtime, methodology-enforcement hooks self-gate to
Work mode via is_work_mode (plan Section 0.4 decision 1).

- writ-verify-before-claim.sh (PreToolUse TodoWrite + Stop)
- writ-sdd-review-order.sh    (PreToolUse Task)
- writ-worktree-safety.sh     (PreToolUse Bash)
- writ-pressure-audit.sh      (SessionEnd)
- writ-quality-judge.sh       (PostToolUse Write artifact)
- validate-test-file.sh       (PreToolUse Write src/**)
- validate-design-doc.sh      (PreToolUse Write docs/**/*-design.md)
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOKS = WRIT_ROOT / ".claude" / "hooks"

PHASE2_HOOKS = [
    "writ-verify-before-claim.sh",
    "writ-sdd-review-order.sh",
    "writ-worktree-safety.sh",
    "writ-pressure-audit.sh",
    "writ-quality-judge.sh",
    "validate-test-file.sh",
    "validate-design-doc.sh",
]


def _run_hook(hook: str, stdin_json: dict, extra_env: dict | None = None) -> tuple[str, int]:
    """Run a hook with the given stdin envelope. Returns (stdout, exit_code)."""
    import os
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [str(HOOKS / hook)],
        input=json.dumps(stdin_json),
        capture_output=True, text=True, env=env,
        cwd=str(WRIT_ROOT),
    )
    return proc.stdout, proc.returncode


class TestHooksExitCleanlyOutsideWorkMode:
    """Enforcement hooks self-gate to Work mode. Outside Work, they no-op.

    The test session we use here has no mode set (None), which is treated
    as non-Work. Every enforcement hook should exit 0 with no deny output.
    Pressure-audit runs unconditionally (it's observational, not gating).
    """

    @pytest.mark.parametrize("hook", [
        "writ-verify-before-claim.sh",
        "writ-sdd-review-order.sh",
        "writ-worktree-safety.sh",
        "writ-quality-judge.sh",
        "validate-test-file.sh",
        "validate-design-doc.sh",
    ])
    def test_hook_no_deny_in_non_work_mode(self, hook: str) -> None:
        stdin = {
            "session_id": "non-work-test",
            "tool_name": "TodoWrite",
            "tool_input": {"todos": [{"id": "x", "status": "completed"}]},
            "file_path": "src/foo.py",
            "command": "git worktree add .worktrees/feat branch",
        }
        stdout, code = _run_hook(hook, stdin)
        assert code == 0
        assert '"permissionDecision":"deny"' not in stdout.replace(" ", "")
        assert '"permissionDecision": "deny"' not in stdout


class TestWorktreeSafetyBoundary:
    """Unit-level check: outside-repo paths never deny."""

    def test_worktree_outside_repo_passes(self) -> None:
        stdin = {
            "session_id": "wt-test",
            "tool_name": "Bash",
            "tool_input": {"command": "git worktree add /tmp/elsewhere feat"},
        }
        stdout, code = _run_hook("writ-worktree-safety.sh", stdin)
        assert code == 0
        assert "deny" not in stdout.lower()


class TestQualityJudgeArtifactClassification:
    """The quality-judge hook emits a directive only for artifact types."""

    def test_non_artifact_file_no_directive(self) -> None:
        stdin = {
            "session_id": "qj-test",
            "tool_name": "Write",
            "file_path": "random.txt",
            "tool_input": {"file_path": "random.txt"},
        }
        stdout, code = _run_hook("writ-quality-judge.sh", stdin)
        assert code == 0
        assert "[WRIT QUALITY-JUDGE]" not in stdout


class TestHookSyntaxAndExecutability:
    """All Phase 2 hooks must be executable and syntax-valid bash."""

    @pytest.mark.parametrize("hook", PHASE2_HOOKS)
    def test_hook_is_executable(self, hook: str) -> None:
        import os
        path = HOOKS / hook
        assert path.exists(), f"{hook} does not exist"
        assert os.access(path, os.X_OK), f"{hook} is not executable"

    @pytest.mark.parametrize("hook", PHASE2_HOOKS)
    def test_hook_syntax_valid(self, hook: str) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(HOOKS / hook)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{hook} syntax error: {proc.stderr}"


class TestNoFeatureFlagReferences:
    """The flag-free architecture: no hook should mention the removed flag."""

    @pytest.mark.parametrize("hook", PHASE2_HOOKS)
    def test_no_flag_check(self, hook: str) -> None:
        content = (HOOKS / hook).read_text()
        assert "is_superpowers_absorb_enabled" not in content
        assert "superpowers_absorb.enabled" not in content
