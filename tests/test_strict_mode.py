"""WRIT_STRICT=1: fail closed when the write gate cannot be evaluated.

Default posture is fail-open on daemon outage (documented as the availability
decision). Strict mode flips the NO-ANSWER residue to deny. Two layers pinned:

1. The daemon-down Bash-write path now gets a real LOCAL answer (the same
   subprocess fallback the Write gate uses), so an outage no longer ungates
   Bash writes at all -- the fallback is the security win, strict is the
   policy switch for whatever cannot answer even locally.
2. The pre-write-check degenerate arm (daemon down AND no session id in the
   body, so no local fallback is possible) is the reachable no-answer case:
   allow by default, ENF-STRICT-001 deny under WRIT_STRICT=1.

No live daemon needed: WRIT_PORT points at a dead port throughout.
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

SKILL_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
COMMON_SH = os.path.join(SKILL_ROOT, "bin", "lib", "common.sh")
BASH_GATE = os.path.join(SKILL_ROOT, "hooks", "scripts", "writ-bash-write-gate.sh")
DEAD_PORT = "59999"


def _run_pre_write_check(body: str, strict: bool) -> dict:
    env = {**os.environ, "WRIT_PORT": DEAD_PORT, "WRIT_NO_AUTOSTART": "1"}
    if strict:
        env["WRIT_STRICT"] = "1"
    else:
        env.pop("WRIT_STRICT", None)
    script = f'source "{COMMON_SH}"; _writ_session pre-write-check "$1"'
    p = subprocess.run(["bash", "-c", script, "bash", body],
                       capture_output=True, text=True, env=env, timeout=60)
    return json.loads(p.stdout.strip().splitlines()[-1])


def _run_bash_gate(command: str, sid: str, cwd: str, cache: Path) -> dict | None:
    envelope = json.dumps({"session_id": sid, "tool_name": "Bash",
                           "tool_input": {"command": command}})
    env = {**os.environ, "WRIT_PORT": DEAD_PORT, "WRIT_NO_AUTOSTART": "1",
           "WRIT_CACHE_DIR": str(cache)}
    env.pop("WRIT_STRICT", None)
    p = subprocess.run(["bash", BASH_GATE], input=envelope, cwd=cwd,
                       capture_output=True, text=True, env=env, timeout=120)
    out = p.stdout.strip()
    if not out:
        return None
    return json.loads(out).get("hookSpecificOutput", {})


class TestPreWriteCheckStrictPolicy:
    _NO_SID_BODY = '{"tool_input": {"file_path": "/proj/src/foo.py"}}'

    def test_no_answer_defaults_to_allow(self) -> None:
        result = _run_pre_write_check(self._NO_SID_BODY, strict=False)
        assert result["decision"] == "allow"

    def test_no_answer_fails_closed_under_strict(self) -> None:
        result = _run_pre_write_check(self._NO_SID_BODY, strict=True)
        assert result["decision"] == "deny"
        assert "ENF-STRICT-001" in (result.get("reason") or "")

    def test_strict_deny_names_the_recovery_action(self) -> None:
        result = _run_pre_write_check(self._NO_SID_BODY, strict=True)
        assert "writ-server" in (result.get("reason") or "")


class TestBashGateDaemonDownLocalFallback:
    def test_work_mode_bash_write_denied_with_daemon_down(self, tmp_path: Path) -> None:
        # The pre-fallback behavior was a silent allow on daemon outage; the
        # local subprocess now answers, so the plan gate holds through an outage.
        cache = tmp_path / "cache"
        cache.mkdir()
        sid = f"strict-{uuid.uuid4().hex[:8]}"
        (cache / f"writ-session-{sid}.json").write_text(
            json.dumps({"mode": "work", "gates_approved": [], "current_phase": None}))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        (repo / ".git").mkdir()  # project-root marker so the target reads as project-local
        out = _run_bash_gate("echo x > src/foo.py", sid, str(repo), cache)
        assert out is not None and out.get("permissionDecision") == "deny", (
            "a daemon outage must not ungate Bash writes; the local fallback "
            f"should deny the pre-plan write, got {out!r}"
        )
        assert "ENF-GATE-PLAN" in out.get("permissionDecisionReason", "")

    def test_conversation_mode_bash_write_still_allowed_daemon_down(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir()
        sid = f"strict-{uuid.uuid4().hex[:8]}"
        (cache / f"writ-session-{sid}.json").write_text(json.dumps({"mode": "conversation"}))
        repo = tmp_path / "repo"
        (repo / "src").mkdir(parents=True)
        out = _run_bash_gate("echo x > src/foo.py", sid, str(repo), cache)
        assert out is None, f"conversation mode must stay ungated, got {out!r}"
