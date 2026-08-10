"""Phase 2 enforcement-layer correctness fixes (WRIT-BLUEPRINT Phase 2).

Each class is a failing-test-first (RED -> GREEN) gate for one verified
finding. The findings were re-verified live against HEAD 6cc9fe6 before any
fix; 2.1 (config import guard) was dropped as already-guarded.

  2.2  validate-design-doc.sh / writ-worktree-safety.sh heredoc-argv bug:
       `python3 <<'PY' ... PY "$VAR"` never passes "$VAR" to python (it lands
       after the heredoc terminator), so sys.argv[1] IndexErrors and the gate
       emits an empty DENY -> the PreToolUse gate is inert (always allows).
  2.3  writ-session-end.sh / writ-precompact.sh derive session_id from PPID
       instead of the stdin envelope -> end-of-session telemetry reads the
       wrong cache.
  2.6  analyze_rule_effectiveness branches on gate_denial.rule_id which the
       emitter never sets -> rule-effectiveness analysis is dead.
  2.7  delete_rule counts after DETACH DELETE (always 0); detect_stale is
       Rule-only and never checks methodology nodes.
  2.8  metrics.cmd_metrics ignores WRIT_FRICTION_LOG; inject-tier-workflow.sh
       has no `investigate` branch.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox so `mode set` cannot delete THIS repo's gate artifacts.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOKS = WRIT_ROOT / "hooks" / "scripts"
SESSION_PY = WRIT_ROOT / "bin" / "lib" / "writ-session.py"


def _set_work_mode(cache_dir: Path, session_id: str) -> None:
    """Put a session into work mode in an isolated cache dir."""
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    # NO cwd=WRIT_ROOT here, unlike _run_hook below. `mode set` stamps the process cwd as
    # the session's project_root and then deletes that project's .claude/gates/*.approved,
    # so pinning it to the repo root made this helper wipe THIS repo's approval artifacts
    # on every run. Inheriting the sandbox_cwd fixture's cwd keeps the damage in tmp_path.
    proc = subprocess.run(
        ["python3", str(SESSION_PY), "mode", "set", "work", session_id],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"mode set work failed: {proc.stderr}"


def _run_hook(hook: str, envelope: dict, cache_dir: Path, cwd: Path | None = None) -> tuple[str, int]:
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(cache_dir)
    proc = subprocess.run(
        [str(HOOKS / hook)],
        input=json.dumps(envelope),
        capture_output=True, text=True, env=env,
        cwd=str(cwd or WRIT_ROOT),
    )
    return proc.stdout, proc.returncode


class TestDesignDocGateFires:
    """2.2: in work mode, a bad design doc must be DENIED. The heredoc-argv
    bug made DENY empty, so the gate never denied. Fix passes CONTENT via env."""

    def test_bad_design_doc_is_denied_in_work_mode(self, tmp_path: Path) -> None:
        sid = "p2-design-deny"
        _set_work_mode(tmp_path, sid)
        envelope = {
            "session_id": sid,
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/proj/docs/specs/feature-design.md",
                # Missing every REQUIRED subsection -> must trigger DENY.
                "content": "# Feature\n\nshort note\n",
            },
        }
        stdout, code = _run_hook("validate-design-doc.sh", envelope, tmp_path)
        assert code == 0, f"hook crashed: {stdout}"
        assert '"permissionDecision": "deny"' in stdout, (
            f"design-doc gate did not deny a bad doc in work mode (inert gate): {stdout!r}"
        )
        assert "validate-design-doc" in stdout


class TestWorktreeSafetyGateFires:
    """2.2: in work mode, a project-local worktree target with no .gitignore
    entry must be DENIED. Same heredoc-argv bug rendered it inert."""

    def test_unignored_worktree_target_denied_in_work_mode(self, tmp_path: Path) -> None:
        sid = "p2-wt-deny"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        repo = tmp_path / "repo"  # fresh dir, no .gitignore
        repo.mkdir()
        _set_work_mode(cache_dir, sid)
        envelope = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": "git worktree add wt-target feat"},
        }
        stdout, code = _run_hook("writ-worktree-safety.sh", envelope, cache_dir, cwd=repo)
        assert code == 0, f"hook crashed: {stdout}"
        assert '"permissionDecision": "deny"' in stdout, (
            f"worktree-safety gate did not deny an unignored project-local target "
            f"in work mode (inert gate): {stdout!r}"
        )
        assert "ENF-PROC-WORKTREE-001" in stdout


def _read_friction(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _run_hook_with_friction(hook: str, envelope: dict, tmp: Path) -> Path:
    """Run a hook with isolated cache + friction log; return the friction path."""
    friction = tmp / "friction.log"
    env = os.environ.copy()
    env["WRIT_CACHE_DIR"] = str(tmp)
    env["WRIT_FRICTION_LOG"] = str(friction)
    subprocess.run(
        [str(HOOKS / hook)],
        input=json.dumps(envelope),
        capture_output=True, text=True, env=env, cwd=str(WRIT_ROOT),
    )
    return friction


class TestSessionEndUsesEnvelopeSessionId:
    """2.3: writ-session-end.sh must derive session_id from the stdin envelope,
    not PPID. The session_end rollup entry's `session` field is the SESSION_ID
    the hook computed (daemon/cache-independent), so it is the exact signal."""

    def test_session_end_rollup_tags_envelope_session(self, tmp_path: Path) -> None:
        sid = "p2-sessionend-env-9c1f"
        envelope = {"session_id": sid, "hook_event_name": "SessionEnd"}
        friction = _run_hook_with_friction("writ-session-end.sh", envelope, tmp_path)
        entries = _read_friction(friction)
        rollups = [e for e in entries if e.get("event") == "session_end"]
        assert rollups, f"no session_end rollup written: {entries!r}"
        assert any(e.get("session") == sid for e in rollups), (
            f"session_end rollup used a non-envelope session id (PPID bug): "
            f"{[e.get('session') for e in rollups]!r}"
        )


class TestPrecompactUsesEnvelopeSessionId:
    """2.3: writ-precompact.sh must derive session_id from the stdin envelope,
    not PPID. The hook_execution telemetry carries the computed SESSION_ID."""

    def test_precompact_telemetry_tags_envelope_session(self, tmp_path: Path) -> None:
        sid = "p2-precompact-env-7b4a"
        envelope = {"session_id": sid, "hook_event_name": "PreCompact"}
        friction = _run_hook_with_friction("writ-precompact.sh", envelope, tmp_path)
        entries = _read_friction(friction)
        execs = [
            e for e in entries
            if e.get("event") == "hook_execution" and e.get("hook_name") == "writ-precompact"
        ]
        assert execs, f"no writ-precompact hook_execution telemetry written: {entries!r}"
        assert any(e.get("session") == sid for e in execs), (
            f"precompact telemetry used a non-envelope session id (PPID bug): "
            f"{[e.get('session') for e in execs]!r}"
        )


class TestMetricsHonorsFrictionLogEnv:
    """2.8a: cmd_metrics must consult WRIT_FRICTION_LOG when no explicit path is
    given, instead of going straight to the project-marker walk."""

    def test_metrics_reads_env_log_when_no_marker(self, tmp_path: Path) -> None:
        log = tmp_path / "friction.log"
        log.write_text(
            json.dumps({"ts": "2026-06-12T00:00:00Z", "session": "s1", "event": "hook_execution"}) + "\n"
            + json.dumps({"ts": "2026-06-12T00:00:01Z", "session": "s2", "event": "hook_execution"}) + "\n"
        )
        empty = tmp_path / "no_markers"  # cwd with no project marker -> marker-walk finds nothing
        empty.mkdir()
        env = os.environ.copy()
        env["WRIT_FRICTION_LOG"] = str(log)
        proc = subprocess.run(
            ["python3", str(SESSION_PY), "metrics"],
            capture_output=True, text=True, env=env, cwd=str(empty),
        )
        report = json.loads(proc.stdout)
        assert "error" not in report, (
            f"cmd_metrics ignored WRIT_FRICTION_LOG and found no log via marker-walk: {report!r}"
        )
        assert report.get("total_events") == 2, (
            f"cmd_metrics did not read the env-pointed friction log: {report!r}"
        )


class TestInjectTierWorkflowHandlesInvestigate:
    """2.8b: inject-tier-workflow.sh must emit a directive for `mode set
    investigate`, not silently no-op as it does for the four older modes."""

    def test_investigate_mode_injects_directive(self, tmp_path: Path) -> None:
        sid = "p2-inject-investigate"
        envelope = {
            "session_id": sid,
            "tool_name": "Bash",
            "tool_input": {"command": f"python3 writ-session.py mode set investigate {sid}"},
            "tool_output": "Mode set: investigate",
            "hook_event_name": "PostToolUse",
        }
        env = os.environ.copy()
        env["WRIT_CACHE_DIR"] = str(tmp_path)
        proc = subprocess.run(
            [str(HOOKS / "inject-tier-workflow.sh")],
            input=json.dumps(envelope),
            capture_output=True, text=True, env=env, cwd=str(WRIT_ROOT),
        )
        assert proc.returncode == 0, f"hook crashed: {proc.stderr}"
        assert "Investigate mode" in proc.stdout, (
            f"inject-tier-workflow.sh did not inject an investigate directive: {proc.stdout!r}"
        )


class TestRuleEffectivenessNotDead:
    """2.6: gate_denial events must carry the enforcement rule_id (extracted from
    the [RULE-ID] reason prefix) so analyze_rule_effectiveness counts them.
    Before the fix, gate_denial had no rule_id -> every one was skipped and
    rule-effectiveness was dead."""

    def test_gate_denial_emits_rule_id_and_analyzer_counts_it(self, tmp_path: Path, monkeypatch) -> None:
        from writ.analysis.friction import analyze_rule_effectiveness, parse_log
        from writ.session.gates import _log_gate_denial

        log = tmp_path / "friction.log"
        monkeypatch.setenv("WRIT_FRICTION_LOG", str(log))
        monkeypatch.setenv("WRIT_CACHE_DIR", str(tmp_path))

        cache = {"mode": "work", "current_phase": "planning"}
        _log_gate_denial(
            "s-2-6", cache, "phase-a", "x.py",
            "[ENF-GATE-PLAN] ALL writes blocked -- plan not yet approved.",
        )

        events = parse_log(log)
        denials = [e for e in events if e.event == "gate_denial"]
        assert denials, f"no gate_denial emitted: {[e.event for e in events]}"
        assert denials[0].rule_id == "ENF-GATE-PLAN", (
            f"gate_denial did not carry the enforcement rule_id: {denials[0].model_dump()}"
        )

        rows = {r.rule_id: r for r in analyze_rule_effectiveness(events)}
        assert "ENF-GATE-PLAN" in rows, (
            f"analyze_rule_effectiveness produced no row for the denied rule (dead analysis): "
            f"{list(rows)}"
        )
        assert rows["ENF-GATE-PLAN"].stuck_denials >= 1
