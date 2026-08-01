"""Phase 1.2: single env-aware friction writer + test-log isolation.

Verifies that bin/lib/friction-append.py is the one writer all hooks/modules
route through, and that it honors WRIT_FRICTION_LOG so the autouse conftest
fixture can keep the repo's workflow-friction.log clean during the suite.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FRICTION_APPEND = REPO / "bin" / "lib" / "friction-append.py"
COMMON_SH = REPO / "bin" / "lib" / "common.sh"


def _read_log(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_friction_append_exists() -> None:
    assert FRICTION_APPEND.is_file(), "bin/lib/friction-append.py must exist"


def test_positional_event_honors_env(tmp_path, monkeypatch) -> None:
    log = tmp_path / "elsewhere" / "friction.jsonl"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
    res = subprocess.run(
        [sys.executable, str(FRICTION_APPEND), "sid-x", "work", "write_attempt",
         json.dumps({"file_path": "/a/b.py", "result": "deny", "gate_status": "phase-a"})],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    events = _read_log(log)
    assert len(events) == 1
    e = events[0]
    assert e["session"] == "sid-x"
    assert e["mode"] == "work"
    assert e["event"] == "write_attempt"
    assert e["file_path"] == "/a/b.py"
    assert e["gate_status"] == "phase-a"
    assert "ts" in e


def test_stdin_json_mode_adds_ts(tmp_path, monkeypatch) -> None:
    log = tmp_path / "friction.jsonl"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
    entry = {"session": "s2", "mode": None, "event": "subagent_complete", "agent_type": "Explore"}
    res = subprocess.run(
        [sys.executable, str(FRICTION_APPEND), "--stdin-json"],
        input=json.dumps(entry), capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stderr
    events = _read_log(log)
    assert len(events) == 1
    assert events[0]["agent_type"] == "Explore"
    assert "ts" in events[0]


def test_no_env_routes_to_central_router_not_repo_root(tmp_path, monkeypatch) -> None:
    """CONTRACT CHANGE (logging P1, per the approved logging blueprint, now in git history only): with no
    WRIT_FRICTION_LOG, friction-append routes through the central router at
    ~/.claude/writ/logs/<project>/<stream>.jsonl. The old marker-walk that wrote
    <repo>/workflow-friction.log is superseded (centralization was the design
    goal); the repo-root file must NOT be created."""
    from writ.shared.logging import stream_path

    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))  # reader (stream_path) must match the subprocess root
    (tmp_path / ".git").mkdir()
    env = {**os.environ, "WRIT_LOG_ROOT": str(tmp_path / "logs"), "WRIT_LOG_PROJECT": "iso-proj"}
    env.pop("WRIT_FRICTION_LOG", None)
    res = subprocess.run(
        [sys.executable, str(FRICTION_APPEND), "sid-z", "", "phase_advance"],
        cwd=str(tmp_path), capture_output=True, text=True, env=env,
    )
    assert res.returncode == 0, res.stderr
    # phase_advance is STREAM_MAP-classified as audit, under the central root.
    events = _read_log(stream_path("iso-proj", "audit"))
    assert len(events) == 1
    assert events[0]["mode"] is None
    assert not (tmp_path / "workflow-friction.log").exists()


def test_common_sh_log_friction_event_honors_env(tmp_path, monkeypatch) -> None:
    """common.sh's log_friction_event must route through the env-aware writer."""
    log = tmp_path / "common.jsonl"
    env = {**os.environ, "WRIT_FRICTION_LOG": str(log)}
    cmd = f'source "{COMMON_SH}" && log_friction_event "sid-c" "debug" "mode_change" \'{{"to":"work"}}\''
    res = subprocess.run(["bash", "-c", cmd], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    events = _read_log(log)
    assert len(events) == 1
    assert events[0]["event"] == "mode_change"
    assert events[0]["to"] == "work"


def test_common_sh_hook_timer_end_honors_env(tmp_path, monkeypatch) -> None:
    log = tmp_path / "timer.jsonl"
    env = {**os.environ, "WRIT_FRICTION_LOG": str(log)}
    cmd = (f'source "{COMMON_SH}" && S=$(hook_timer_start) && '
           f'hook_timer_end "$S" "my-hook" "sid-t" "work"')
    res = subprocess.run(["bash", "-c", cmd], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    events = _read_log(log)
    assert len(events) == 1
    assert events[0]["event"] == "hook_execution"
    assert events[0]["hook_name"] == "my-hook"
    assert isinstance(events[0]["duration_ms"], int)


def test_common_sh_detect_project_root_resolves_root_dir(tmp_path) -> None:
    """E-PROJROOT-BUG: detect_project_root must resolve the project root even when passed the
    root directory itself (the `$(pwd)` callers -- check-gates.sh / writ-session-end.sh /
    inject-tier-workflow.sh -- run from the repo root). The pre-fix `dirname`-before-check
    returned "" there, silently skipping gate checks. Sub-dir and file-path callers unchanged."""
    (tmp_path / ".git").write_text("")  # project-root marker
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)

    def _resolve(path) -> str:
        res = subprocess.run(
            ["bash", "-c", f'source "{COMMON_SH}" && detect_project_root "{path}"'],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, res.stderr
        return res.stdout.strip()

    assert _resolve(tmp_path) == str(tmp_path)            # the bug case: root dir itself
    assert _resolve(sub) == str(tmp_path)                 # nested dir resolves to root
    assert _resolve(sub / "file.php") == str(tmp_path)    # file path unchanged


def test_session_friction_module_honors_env(tmp_path, monkeypatch) -> None:
    log = tmp_path / "session.jsonl"
    monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
    from writ.session.friction import _log_friction_event

    _log_friction_event("sid-m", "work", "phase_transition", to_phase="testing")
    events = _read_log(log)
    assert len(events) == 1
    assert events[0]["event"] == "phase_transition"
    assert events[0]["to_phase"] == "testing"


def test_no_inline_marker_walk_writers_remain() -> None:
    """Option B: no hook/module may still open workflow-friction.log directly via
    the copy-pasted marker-walk idiom. All writes route through friction-append.py
    (or, in-package, resolve_log_path). Guards against regression of the sprawl."""
    offenders: list[str] = []
    search_dirs = [REPO / "hooks" / "scripts", REPO / "bin" / "lib"]
    allow = {"friction-append.py"}
    for base in search_dirs:
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".sh", ".py"}:
                continue
            if path.name in allow:
                continue
            text = path.read_text(errors="ignore")
            if "workflow-friction.log" in text and "open(" in text:
                offenders.append(str(path.relative_to(REPO)))
    assert not offenders, f"inline marker-walk friction writers remain: {offenders}"
