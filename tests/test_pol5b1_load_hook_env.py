"""POL-5b-1: single-spawn hook field extraction via load_hook_env.

`parse-hook-stdin.py --shell` emits shlex-quoted shell assignments for the scalar
fields (+ HOOK_ENVELOPE); `common.sh load_hook_env` evals them in one python3 spawn.
The 6 scalars-only hooks migrate off the parse_hook_stdin + parsed_field 2-spawn idiom.

load_hook_env no longer "applies the session-id fallback", and this file used to require
that it did. See TestSessionId below.

RED until --shell + load_hook_env exist and the hooks are migrated.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
PARSE_PY = str(SKILL_DIR / "bin" / "lib" / "parse-hook-stdin.py")
COMMON_SH = str(SKILL_DIR / "bin" / "lib" / "common.sh")
HOOKS = SKILL_DIR / "hooks" / "scripts"

# track-failed-writes.sh deferred to 5b-2: it reads the non-normalized `error`
# field and resolves session_id via a custom WRIT_SESSION_ID/-current-session path
# (not detect_session_id), so it is not cleanly scalars-only.
MIGRATED = [
    "writ-worktree-safety.sh",
    "writ-dispatch-discipline.sh",
    "validate-test-file.sh",
    "writ-run-pending-tests.sh",
    "writ-pressure-audit.sh",
]

SAMPLE_ENV = (
    '{"hook_event_name":"PreToolUse","tool_name":"Write","session_id":"sid-123",'
    '"agent_id":"","tool_input":{"file_path":"/tmp/foo/Bar.php","content":"x"}}'
)


def _shell_then_print(envelope: str, var: str, *, extra: str = "") -> subprocess.CompletedProcess:
    """eval the --shell output for `envelope`, then print one HOOK_ var."""
    script = (
        f'{extra}'
        f'eval "$(printf %s {shlex.quote(envelope)} | python3 {shlex.quote(PARSE_PY)} --shell)"; '
        f'printf "%s" "${{{var}}}"'
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)


def _load_hook_env_print(envelope: str, var: str) -> str:
    """Source common.sh, run load_hook_env on the envelope, print one HOOK_ var."""
    script = (
        f'source {shlex.quote(COMMON_SH)}; '
        f'printf %s {shlex.quote(envelope)} | {{ load_hook_env; printf "%s" "${{{var}}}"; }}'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    return r.stdout


# --------------------------------------------------------------------------- #
# 1. --shell emits the fields
# --------------------------------------------------------------------------- #
class TestShellMode:
    @pytest.mark.parametrize("var,expected", [
        ("HOOK_SESSION_ID", "sid-123"),
        ("HOOK_FILE_PATH", "/tmp/foo/Bar.php"),
        ("HOOK_TOOL_NAME", "Write"),
        ("HOOK_EVENT", "PreToolUse"),
    ])
    def test_shell_emits_scalar(self, var: str, expected: str) -> None:
        r = _shell_then_print(SAMPLE_ENV, var)
        assert r.returncode == 0
        assert r.stdout == expected, f"{var}: got {r.stdout!r}, want {expected!r}"

    def test_shell_emits_envelope_json(self) -> None:
        r = _shell_then_print(SAMPLE_ENV, "HOOK_ENVELOPE")
        assert '"file_path"' in r.stdout and "Bar.php" in r.stdout

    def test_json_mode_unchanged(self) -> None:
        """Without --shell the parser still emits normalized JSON."""
        r = subprocess.run(
            ["python3", PARSE_PY], input=SAMPLE_ENV,
            capture_output=True, text=True, timeout=10,
        )
        assert r.stdout.lstrip().startswith("{") and '"file_path"' in r.stdout


# --------------------------------------------------------------------------- #
# 2. injection-safe
# --------------------------------------------------------------------------- #
class TestInjectionSafe:
    def test_hostile_file_path_not_executed(self) -> None:
        marker = Path("/tmp/pol5b1_pwned")
        if marker.exists():
            marker.unlink()
        hostile = '/tmp/a;$(touch /tmp/pol5b1_pwned) b\'c"'
        env = (
            '{"hook_event_name":"PreToolUse","tool_name":"Write","session_id":"s",'
            '"tool_input":{"file_path":' + _json_str(hostile) + ',"content":"x"}}'
        )
        r = _shell_then_print(env, "HOOK_FILE_PATH")
        try:
            assert not marker.exists(), "hostile file_path was executed by eval -- shlex.quote missing"
            assert r.stdout == hostile, f"HOOK_FILE_PATH not literal: {r.stdout!r}"
        finally:
            if marker.exists():
                marker.unlink()


def _json_str(s: str) -> str:
    import json
    return json.dumps(s)


# --------------------------------------------------------------------------- #
# 3. session-id preference + fallback
# --------------------------------------------------------------------------- #
class TestSessionId:
    def test_agent_id_preferred_over_session_id(self) -> None:
        env = '{"agent_id":"agent-9","session_id":"sid-1","tool_input":{}}'
        r = _shell_then_print(env, "HOOK_SESSION_ID")
        assert r.stdout == "agent-9"

    def test_session_id_when_no_agent(self) -> None:
        env = '{"agent_id":"","session_id":"sid-1","tool_input":{}}'
        r = _shell_then_print(env, "HOOK_SESSION_ID")
        assert r.stdout == "sid-1"

    def test_load_hook_env_leaves_the_id_empty_when_the_payload_has_none(self) -> None:
        """This test used to assert the OPPOSITE, and that is why it is worth reading.

        It required load_hook_env to "produce a non-empty fallback session id", which the
        helper did by running `ps -o ppid=` and then md5(cwd:user)-date. Neither can ever
        equal the id Claude Code uses, so the id it produced named a session that does not
        exist: every write under it was lost, and the hook reported success. The test was
        green the whole time, because it only ever checked that SOMETHING came out.

        The contract now is the payload's id or the empty string. A caller that needs a
        session detects empty, calls writ_critical, and no-ops; the rest carry on, because
        plenty of hooks do work that needs no session at all.
        See tests/test_session_identity_no_fallback.py for the full argument.
        """
        env = '{"agent_id":"","session_id":"","tool_input":{}}'
        out = _load_hook_env_print(env, "HOOK_SESSION_ID")
        assert out == "", (
            f"load_hook_env invented a session id the payload never carried: {out!r}"
        )


# --------------------------------------------------------------------------- #
# 4. migration: hooks dropped the 2-spawn idiom
# --------------------------------------------------------------------------- #
class TestMigration:
    @pytest.mark.parametrize("hook", MIGRATED)
    def test_hook_uses_load_hook_env(self, hook: str) -> None:
        src = (HOOKS / hook).read_text()
        assert "load_hook_env" in src, f"{hook} must call load_hook_env"

    @pytest.mark.parametrize("hook", MIGRATED)
    def test_hook_dropped_old_parse_idiom(self, hook: str) -> None:
        src = (HOOKS / hook).read_text()
        for stale in ("parsed_field", "parse_hook_stdin", "detect_session_id"):
            assert stale not in src, f"{hook} must not still use {stale} after migration"


# --------------------------------------------------------------------------- #
# 5. behavior preserved (smoke)
# --------------------------------------------------------------------------- #
class TestBehaviorPreserved:
    @pytest.mark.parametrize("hook", MIGRATED)
    def test_hook_runs_clean_on_sample_envelope(self, hook: str) -> None:
        r = subprocess.run(
            ["bash", str(HOOKS / hook)],
            input=SAMPLE_ENV,
            capture_output=True, text=True,
            cwd=str(SKILL_DIR),
            env={**os.environ, "WRIT_HOST": "localhost"},
            timeout=15,
        )
        assert r.returncode == 0, f"{hook} exited {r.returncode}; stderr={r.stderr[:200]!r}"
