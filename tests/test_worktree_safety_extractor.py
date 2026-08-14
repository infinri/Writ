"""A gate that fires on commands which merely TALK about `git worktree add`.

writ-worktree-safety.sh decided with a raw substring match on the whole command text, so
any Bash command CONTAINING the words was denied. Reproduced live on 2026-08-08: a command
passing a JSON string to a subprocess, with `git worktree add .worktrees/feature-x` sitting
inside that string, was refused outright, and the entire multi-line script was written into
the `target` column of the audit log. The same shape hid a real miss in the other
direction: `git -C . worktree add ...` was ALLOWED, because the literal words were not
adjacent.

The fix replaced the substring match with a `shlex(posix=False)` tokenizer. It shipped
without a test, which is what this file is: the extractor is exactly the kind of code that
looks obviously right and has a large space of inputs, and a revert to the old `case` arm
would be invisible to the rest of the suite.

Both directions are asserted, because either alone is satisfiable by a broken hook: one
that denies nothing passes every false-positive test, and one that denies everything passes
every true-positive test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "writ-worktree-safety.sh"
HELPER = REPO / "bin" / "lib" / "writ-session.py"

SID = "worktree-extractor-session"


def _env(cache_root: Path) -> dict:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_root)
    env["WRIT_FRICTION_LOG"] = str(cache_root / "friction.log")
    env["WRIT_LOG_ROOT"] = str(cache_root / "logs")
    return env


@pytest.fixture
def cache_root(tmp_path) -> Path:
    root = tmp_path / "writ-cache"
    root.mkdir()
    subprocess.run([sys.executable, str(HELPER), "mode", "set", "work", SID],
                   env=_env(root), check=True, capture_output=True)
    return root


@pytest.fixture
def project(tmp_path) -> Path:
    """A git project whose .gitignore covers .worktrees/ and nothing else."""
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True, capture_output=True)
    (proj / ".gitignore").write_text(".worktrees/\n")
    return proj


def _run(command: str, cache_root: Path, project: Path):
    """Run the hook on one Bash command; return (decision or None, target or None)."""
    envelope = {
        "session_id": SID,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(project),
    }
    res = subprocess.run(["bash", str(HOOK)], input=json.dumps(envelope),
                         capture_output=True, text=True, env=_env(cache_root),
                         cwd=project, timeout=30)
    assert res.returncode == 0, res.stderr
    out = res.stdout.strip()
    if not out:
        return None, None
    hso = json.loads(out).get("hookSpecificOutput", {})
    return hso.get("permissionDecision"), hso.get("permissionDecisionReason", "")


class TestAQuotedMentionIsNotAnInvocation:
    """The false positive, which is the reason the extractor was rewritten."""

    def test_the_words_inside_a_quoted_string_do_not_deny(self, cache_root, project):
        command = (
            """python3 -c 'import json; print(json.dumps("""
            """{"hint": "git worktree add .worktrees/feature-x feature-x"}))'"""
        )
        decision, reason = _run(command, cache_root, project)
        assert decision != "deny", (
            f"a command that only NAMES the guarded operation was refused: {reason}"
        )

    def test_an_echoed_mention_does_not_deny(self, cache_root, project):
        decision, reason = _run(
            'echo "run: git worktree add /tmp/elsewhere branch"', cache_root, project
        )
        assert decision != "deny", f"an echoed mention was refused: {reason}"

    def test_a_grep_for_the_words_does_not_deny(self, cache_root, project):
        decision, reason = _run(
            "grep -rn 'git worktree add' docs/", cache_root, project
        )
        assert decision != "deny", f"searching for the words was refused: {reason}"


class TestARealInvocationIsStillCaught:
    """The other direction. Without these, a hook that denied nothing would be green."""

    def test_a_local_unignored_target_is_denied(self, cache_root, project):
        decision, reason = _run(
            "git worktree add scratch/feature-x feature-x", cache_root, project
        )
        assert decision == "deny", (
            "a real `git worktree add` into an unignored project-local path was allowed"
        )
        assert "scratch/feature-x" in reason or "scratch" in reason, reason

    def test_the_dash_c_form_is_caught(self, cache_root, project):
        """The MISS the old substring match had: `git -C . worktree add` never contains
        the three words adjacently, so the old arm let it through entirely."""
        decision, _ = _run(
            "git -C . worktree add scratch/feature-y feature-y", cache_root, project
        )
        assert decision == "deny", (
            "`git -C . worktree add` was allowed; the extractor is matching adjacent "
            "words rather than parsing the command"
        )

    def test_a_gitignored_target_is_allowed(self, cache_root, project):
        """The gate's actual rule: ignored targets are fine. A hook that denied every
        real invocation would pass both tests above and fail this one."""
        decision, _ = _run(
            "git worktree add .worktrees/feature-z feature-z", cache_root, project
        )
        assert decision != "deny", "a gitignored worktree target must not be refused"


class TestTheAuditTargetIsThePathNotTheScript:
    def test_a_denial_does_not_record_the_whole_command(self, cache_root, project):
        """The second half of the incident: the audit row's `target` held the entire
        multi-line script. An audit column that sometimes contains a whole program is not
        a column anyone can read.
        """
        command = (
            "set -e\n"
            "echo preparing\n"
            "git worktree add scratch/feature-x feature-x\n"
            "echo done\n"
        )
        decision, reason = _run(command, cache_root, project)
        assert decision == "deny", "this case must reach the deny branch"
        assert "echo preparing" not in reason, (
            f"the whole script leaked into the denial record: {reason!r}"
        )


class TestTheNewlineSplitDoesNotReopenTheFalsePositive:
    """Splitting on newlines is what caught the multi-line case above, and it is also the
    change most likely to re-break the thing the extractor was rewritten to fix. Two
    places a newline is DATA rather than a separator."""

    def test_a_newline_inside_a_quoted_string_is_not_a_separator(self, cache_root, project):
        decision, reason = _run(
            'printf "%s" "step one\ngit worktree add scratch/z z"', cache_root, project
        )
        assert decision != "deny", (
            f"a newline inside a quoted argument was treated as a command break: {reason}"
        )

    def test_a_heredoc_body_is_data_not_commands(self, cache_root, project):
        """A document ABOUT worktrees, fed to a command. The old substring match refused
        this too; the newline split would have refused it again for a new reason."""
        command = (
            "cat <<'EOF' > notes.md\n"
            "To make one: git worktree add scratch/feature-x feature-x\n"
            "EOF\n"
        )
        decision, reason = _run(command, cache_root, project)
        assert decision != "deny", f"a heredoc body was judged as a command: {reason}"

    def test_a_real_invocation_after_a_heredoc_is_still_caught(self, cache_root, project):
        """Anti-vacuity for the heredoc skip: a body-skipper that swallowed the rest of
        the token stream would make every test above pass by disabling the gate."""
        command = (
            "cat <<'EOF' > notes.md\n"
            "some notes\n"
            "EOF\n"
            "git worktree add scratch/feature-x feature-x\n"
        )
        decision, _ = _run(command, cache_root, project)
        assert decision == "deny", (
            "the heredoc skip consumed the commands that follow the terminator"
        )
