"""Phase 2 hooks: flag-gating, mode-scope, core behaviors.

Six hooks introduced in Phase 2:
- writ-verify-before-claim.sh (PreToolUse TodoWrite + Stop)
- writ-sdd-review-order.sh    (PreToolUse Task)
- writ-worktree-safety.sh     (PreToolUse Bash)
- writ-pressure-audit.sh      (SessionEnd)
- validate-test-file.sh       (PreToolUse Write src/**)
- validate-design-doc.sh      (PreToolUse Write docs/**/*-design.md)

All feature-flag gated on enforcement.superpowers_absorb.enabled. All
methodology-enforcement hooks (not pressure-audit) mode-scope to Work per
plan Section 0.4 decision 1.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOKS = WRIT_ROOT / ".claude" / "hooks"


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


class TestFeatureFlagGating:
    """With enforcement.superpowers_absorb.enabled=false, every Phase-2 hook no-ops."""

    @pytest.mark.parametrize("hook", [
        "writ-verify-before-claim.sh",
        "writ-sdd-review-order.sh",
        "writ-worktree-safety.sh",
        "writ-pressure-audit.sh",
        "validate-test-file.sh",
        "validate-design-doc.sh",
    ])
    def test_hook_noops_when_flag_disabled(self, hook: str) -> None:
        stdin = {
            "session_id": "flag-disabled-test",
            "tool_name": "TodoWrite",
            "tool_input": {"todos": [{"id": "x", "status": "completed"}]},
            "file_path": "src/foo.py",
            "command": "git worktree add .worktrees/feat branch",
        }
        stdout, code = _run_hook(hook, stdin)
        # Hook must exit 0 and produce no deny output when flag is off.
        assert code == 0
        assert "deny" not in stdout.lower()


class TestWorktreeSafetyLogic:
    """Unit-level check of the gitignore-matching logic via the hook's behavior."""

    def test_worktree_outside_repo_passes(self) -> None:
        # Outside-repo target paths should never deny regardless of .gitignore.
        stdin = {
            "session_id": "wt-test",
            "tool_name": "Bash",
            "tool_input": {"command": "git worktree add /tmp/elsewhere feat"},
        }
        stdout, code = _run_hook("writ-worktree-safety.sh", stdin)
        assert code == 0
        assert "deny" not in stdout.lower()


class TestTestFileGateConvention:
    """Verify the test-file gate recognizes conventional test paths."""

    def test_file_with_existing_test_passes(self, tmp_path: Path) -> None:
        """Even when the flag is off, the hook exits 0 cleanly on a real file."""
        stdin = {
            "session_id": "tf-test",
            "tool_name": "Write",
            "file_path": str(WRIT_ROOT / "writ" / "retrieval" / "ranking.py"),
            "tool_input": {"file_path": str(WRIT_ROOT / "writ" / "retrieval" / "ranking.py")},
        }
        stdout, code = _run_hook("validate-test-file.sh", stdin)
        assert code == 0


class TestHookSyntaxAndExecutability:
    """All 6 new hooks must be executable and syntax-valid bash."""

    @pytest.mark.parametrize("hook", [
        "writ-verify-before-claim.sh",
        "writ-sdd-review-order.sh",
        "writ-worktree-safety.sh",
        "writ-pressure-audit.sh",
        "validate-test-file.sh",
        "validate-design-doc.sh",
    ])
    def test_hook_is_executable(self, hook: str) -> None:
        import os
        path = HOOKS / hook
        assert path.exists(), f"{hook} does not exist"
        assert os.access(path, os.X_OK), f"{hook} is not executable"

    @pytest.mark.parametrize("hook", [
        "writ-verify-before-claim.sh",
        "writ-sdd-review-order.sh",
        "writ-worktree-safety.sh",
        "writ-pressure-audit.sh",
        "validate-test-file.sh",
        "validate-design-doc.sh",
    ])
    def test_hook_syntax_valid(self, hook: str) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(HOOKS / hook)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{hook} syntax error: {proc.stderr}"
