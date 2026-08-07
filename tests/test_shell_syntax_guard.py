"""A shell file that does not parse must never reach disk.

Why this exists, concretely. On 2026-08-07 an edit to bin/lib/common.sh left an `if`
without its `fi`. All 37 hooks source that file, so every hook began failing at the
parse, and Claude Code reads a failing PreToolUse hook as a deny: Bash, Read, and Edit
all stopped working at once. The file that repairs the damage could not be reached
because reaching it requires a tool. The session had to be rescued by hand.

The recovery was worse than the crash: it commented out all 1409 lines of common.sh and
all 585 of writ-rag-inject.sh. That restored syntax and silently disabled every helper,
every gate, rule injection, and mode auto-routing. Nothing failed loudly; two autoroute
tests were the only signal.

So this guards both shapes, at the point where the content is known and nothing has been
written yet:
  1. the proposed content must parse (`bash -n`)
  2. a file that had live code must not become entirely comments

Both are cheap and neither can be satisfied by accident.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "scripts" / "pre-validate-file.sh"


def _run(file_path: str, content: str, tmp_path: Path):
    """Drive the pre-write validation hook with a proposed write."""
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(tmp_path / "cache")
    env["WRIT_LOG_ROOT"] = str(tmp_path / "logs")
    env["WRIT_PORT"] = "19999"
    (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
    envelope = json.dumps({
        "session_id": "shell-guard-probe",
        "tool_name": "Write",
        "hook_event_name": "PreToolUse",
        "tool_input": {"file_path": file_path, "content": content},
    })
    return subprocess.run(
        ["bash", str(HOOK)], input=envelope, capture_output=True, text=True,
        env=env, timeout=60,
    )


def _denied(proc) -> bool:
    """The hook denies by printing a permissionDecision of 'deny' on stdout."""
    if not proc.stdout.strip():
        return False
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return False
    return payload.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


GOOD = '#!/usr/bin/env bash\nset -euo pipefail\nfoo() {\n  echo hi\n}\nfoo\n'
UNBALANCED_IF = (
    '#!/usr/bin/env bash\n'
    'trap_fn() {\n'
    '  if [ -n "$x" ]; then\n'
    '    echo yes\n'          # no `fi`: the exact shape that bricked the session
    '  echo after\n'
    '}\n'
)


class TestSyntaxGuard:
    def test_an_unparseable_shell_file_is_denied(self, tmp_path) -> None:
        proc = _run(str(tmp_path / "victim.sh"), UNBALANCED_IF, tmp_path)
        assert _denied(proc), (
            "a shell file with an unclosed `if` was allowed through; this is the exact "
            f"edit that bricked the session. stdout={proc.stdout[:300]!r}"
        )

    def test_the_denial_says_what_is_wrong(self, tmp_path) -> None:
        """A deny that does not name the problem sends the next person hunting."""
        proc = _run(str(tmp_path / "victim.sh"), UNBALANCED_IF, tmp_path)
        payload = json.loads(proc.stdout)
        reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
        assert "syntax" in reason.lower(), f"unhelpful denial reason: {reason!r}"

    def test_a_valid_shell_file_is_not_denied(self, tmp_path) -> None:
        """Anti-vacuity: a guard that denies everything would pass the test above."""
        proc = _run(str(tmp_path / "fine.sh"), GOOD, tmp_path)
        assert not _denied(proc), f"a valid shell file was denied: {proc.stdout[:300]!r}"

    @pytest.mark.parametrize("name", ["common.sh", "writ-rag-inject.sh", "anything.bash"])
    def test_the_guard_covers_the_shell_extensions_that_matter(
        self, tmp_path, name: str
    ) -> None:
        proc = _run(str(tmp_path / name), UNBALANCED_IF, tmp_path)
        assert _denied(proc), f"{name} was not syntax-checked"

    def test_a_non_shell_file_is_unaffected(self, tmp_path) -> None:
        """The guard must not start denying python or markdown for shell reasons."""
        proc = _run(str(tmp_path / "notes.md"), "# just text\n", tmp_path)
        assert not _denied(proc)


class TestCommentedOutGuard:
    """The second failure shape: a file that parses fine and does nothing."""

    def test_commenting_out_an_entire_live_file_is_denied(self, tmp_path) -> None:
        target = tmp_path / "live.sh"
        target.write_text(GOOD)
        commented = "".join(f"#{line}\n" for line in GOOD.splitlines())
        proc = _run(str(target), commented, tmp_path)
        assert _denied(proc), (
            "a file with live code was replaced by pure comments and allowed; that is "
            "how rule injection and auto-routing were silently disabled"
        )

    def test_a_file_that_keeps_some_live_code_is_allowed(self, tmp_path) -> None:
        """Anti-vacuity: commenting out PART of a file is ordinary editing."""
        target = tmp_path / "live.sh"
        target.write_text(GOOD)
        partly = GOOD.replace("foo\n", "#foo\n")
        proc = _run(str(target), partly, tmp_path)
        assert not _denied(proc)

    def test_a_brand_new_comment_only_file_is_allowed(self, tmp_path) -> None:
        """Only the LIVE -> DEAD transition is suspicious. A new notes-only script,
        or a stub, is not, and denying it would be a guard that cries wolf."""
        proc = _run(str(tmp_path / "brand-new.sh"), "# nothing here yet\n", tmp_path)
        assert not _denied(proc)
