"""ENF-GATE-STATE covers the state root AND the legacy directory the migration reads from."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATE_HOOK = REPO / "hooks" / "scripts" / "writ-state-write-gate.sh"
BASH_HOOK = REPO / "hooks" / "scripts" / "writ-bash-write-gate.sh"


def _env(tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "WRIT_CACHE_DIR"}
    env.update({"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg"),
                "WRIT_LOG_ROOT": str(tmp_path / "logs"),
                "WRIT_FRICTION_LOG": str(tmp_path / "friction.log"),
                "WRIT_PORT": "59996", "WRIT_HOST": "localhost", "WRIT_NO_AUTOSTART": "1"})
    return env


def _decision(stdout: str) -> tuple[str, str]:
    for line in stdout.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        out = doc.get("hookSpecificOutput") or {}
        if out.get("permissionDecision"):
            return out["permissionDecision"], out.get("permissionDecisionReason", "")
    return "allow", ""


def _hook(hook: Path, tool: str, tool_input: dict, tmp_path: Path) -> tuple[str, str]:
    envelope = {"session_id": "sr-gate", "hook_event_name": "PreToolUse",
                "tool_name": tool, "tool_input": tool_input}
    r = subprocess.run(["bash", str(hook)], input=json.dumps(envelope), env=_env(tmp_path),
                       capture_output=True, text=True, timeout=60, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    return _decision(r.stdout)


def _write(tmp_path, path) -> tuple[str, str]:
    return _hook(STATE_HOOK, "Write", {"file_path": str(path), "content": "x"}, tmp_path)


def _bash(tmp_path, cmd) -> tuple[str, str]:
    return _hook(BASH_HOOK, "Bash", {"command": cmd}, tmp_path)


class TestWriteEditGate:
    def test_the_state_root_session_dir_is_protected(self, tmp_path):
        decision, why = _write(tmp_path, tmp_path / "xdg" / "writ" / "session" / "anything.json")
        assert decision == "deny" and "ENF-GATE-STATE" in why

    def test_this_installs_legacy_dir_is_protected(self, tmp_path):
        """A name the basename rule does not match, so only the directory rule can deny."""
        decision, why = _write(tmp_path, REPO / "var" / "session" / "notes.txt")
        assert decision == "deny" and "ENF-GATE-STATE" in why

    @pytest.mark.parametrize("name", ["writ-session-forged.json", "writ-grant-forged.json"])
    def test_a_state_file_name_is_protected_in_any_directory(self, tmp_path, name):
        target = tmp_path / "plugins" / "cache" / "writ" / "writ" / "1.7.0" / "var" / "session" / name
        decision, why = _write(tmp_path, target)
        assert decision == "deny" and "ENF-GATE-STATE" in why

    def test_an_unrelated_var_session_file_is_not(self, tmp_path):
        assert _write(tmp_path, tmp_path / "magento" / "var" / "session" / "sess_abc")[0] == "allow"

    def test_the_session_helper_source_is_not_caught_by_the_name_rule(self, tmp_path):
        assert _write(tmp_path, REPO / "bin" / "lib" / "writ-session.py")[0] == "allow"


class TestBashGate:
    @pytest.mark.parametrize("cmd", [
        "touch {xdg}/writ/session/x",
        "rm -f ~/.local/state/writ/session/x",
        "cp /dev/null {repo}/var/session/x",
    ])
    def test_the_state_dirs_are_refused(self, tmp_path, cmd):
        cmd = cmd.format(xdg=tmp_path / "xdg", repo=REPO)
        decision, why = _bash(tmp_path, cmd)
        assert decision == "deny" and "ENF-GATE-STATE" in why, (cmd, why)

    def test_read_only_inspection_of_the_state_root_is_allowed(self, tmp_path):
        assert _bash(tmp_path, "ls ~/.local/state/writ/session")[0] != "deny"

    def test_destroying_the_state_root_logs_is_irreversible(self, tmp_path):
        decision, why = _bash(tmp_path, "rm -rf ~/.local/state/writ/logs")
        assert decision == "deny" and "ENF-IRREVERSIBLE" in why, why
