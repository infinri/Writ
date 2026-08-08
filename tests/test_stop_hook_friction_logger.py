"""Hermetic tests for the friction-logger.sh Stop hook (P1 regression + loop guard).

Regression under test: the P1 logging-router migration removed
`friction-append.resolve_log_path`, but friction-logger.sh still imported the
module and called `mod.resolve_log_path()` to resolve FRICTION_LOG for its dedup
reads. Under the hook's `set -euo pipefail`, that AttributeError (swallowed by
`2>/dev/null`) collapsed the `FRICTION_LOG=$(...)` command substitution to a
non-zero status and aborted the ENTIRE Stop hook with exit 1 -- silently, on
EVERY Stop -- before it logged any of its three events.

The fix resolves FRICTION_LOG from the P1 router's AUDIT stream
(`writ.shared.logging.stream_path(resolve_project(), 'audit')`) and makes it
NON-FATAL (`|| echo ""`), and adds a stop_hook_active loop-breaker at the top.

Hermetic: WRIT_LOG_ROOT (router root) and WRIT_CACHE_DIR (session cache) point
at tmp; WRIT_LOG_PROJECT pins a deterministic project scope so events land at a
known path independent of the tmp repo's git identity; WRIT_FRICTION_LOG is
unset so the router splits into typed streams (audit) rather than collapsing to
one file. No live Neo4j, no daemon (WRIT_NO_AUTOSTART=1).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FRICTION_LOGGER = REPO / "hooks" / "scripts" / "friction-logger.sh"

sys.path.insert(0, str(REPO))
from writ.shared.logging import read_streams  # noqa: E402

# This hook drives the P1 router's split typed streams under WRIT_LOG_ROOT, so
# opt out of the autouse WRIT_FRICTION_LOG single-file redirect (which would
# collapse audit + friction + metrics into one file and defeat the audit-stream
# assertion).
pytestmark = pytest.mark.no_friction_isolation

SID = "test-friction-logger"
PROJECT = "test-friction-logger-project"


def _seed_cache(cache_dir: Path, *, gate_name: str) -> None:
    """Write a session cache with mode=work and an invalidation_history record
    for `gate_name`. Paired with a `<gate_name>.approved` gate file, this is the
    minimal state the hook needs to emit a gate_denied_then_approved event."""
    cache = {
        "mode": "work",
        "invalidation_history": {
            gate_name: [
                {"ts": "2026-07-01T00:00:00Z", "reason": "test denial"},
            ],
        },
        "phase_transitions": [],
        "phase_transitions_logged": 0,
    }
    (cache_dir / f"writ-session-{SID}.json").write_text(json.dumps(cache))


def _make_project(tmp_path: Path) -> Path:
    """A tmp project root with a .git marker (so the hook's marker-walk resolves
    PROJECT_ROOT) and a .claude/gates dir holding an approved gate file."""
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    (root / ".claude" / "gates").mkdir(parents=True)
    return root


def _run_hook(
    *,
    cache_dir: Path,
    log_root: Path,
    project_root: Path,
    stop_hook_active: bool,
) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "WRIT_NO_AUTOSTART": "1",
        "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_LOG_ROOT": str(log_root),
        "WRIT_LOG_PROJECT": PROJECT,
    }
    # Unset the single-file collapse so the router splits into typed streams.
    env.pop("WRIT_FRICTION_LOG", None)
    # Session identity comes ONLY from the hook's stdin payload (`session_id`,
    # or `agent_id` when present) -- there is no env var or pointer-file
    # fallback. /tmp/writ-current-session used to be read as a fallback, but it
    # is ONE global file rewritten by writ-rag-inject.sh on every
    # UserPromptSubmit in EVERY Claude Code session on the machine, so it named
    # whichever session took a turn most recently rather than the one this hook
    # was invoked for. That silently drained/attributed state to the wrong
    # session, so the fallback was removed; a payload without session_id now
    # makes the hook record a critical error and do nothing.
    payload = json.dumps({"stop_hook_active": stop_hook_active, "session_id": SID})
    return subprocess.run(
        ["bash", str(FRICTION_LOGGER)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project_root),
        timeout=30,
    )


def _audit_events(log_root: Path) -> list[dict]:
    # read_streams uses WRIT_LOG_ROOT via stream_path; it is already set in-process
    # by the autouse fixture to the same tmp `logs` dir the hook wrote to.
    return read_streams(PROJECT, ["audit"])


def _metrics_events(log_root: Path) -> list[dict]:
    # phase_transition_time is METRICS-classified in STREAM_MAP, so its rows land
    # in the metrics stream (NOT audit). read_streams resolves WRIT_LOG_ROOT via
    # stream_path, set in-process by the test's monkeypatch to the tmp logs dir.
    return read_streams(PROJECT, ["metrics"])


def _seed_two_gates_at_fixed_mtimes(project_root: Path) -> None:
    """Seed the two Work-mode gates (phase-a, test-skeletons) at fixed, distinct
    mtimes so the hook's Event-2 mtime diff produces a single phase_transition_time
    (phase-a -> test-skeletons). Fixed mtimes make the gate state identical across
    repeated Stop fires, so the second fire's dedup read must suppress a duplicate."""
    gates = project_root / ".claude" / "gates"
    phase_a = gates / "phase-a.approved"
    test_skel = gates / "test-skeletons.approved"
    phase_a.write_text("ok")
    test_skel.write_text("ok")
    # Distinct, fixed mtimes: test-skeletons approved 60s after phase-a.
    os.utime(phase_a, (1_700_000_000, 1_700_000_000))
    os.utime(test_skel, (1_700_000_060, 1_700_000_060))


def test_gate_denied_then_approved_state_appends_audit_event_and_exits_zero(
    tmp_path, monkeypatch
):
    """With mode=work, an invalidation_history record and a matching .approved
    gate file, the hook exits 0 (NOT the silent set -e abort) and a
    gate_denied_then_approved event lands in the router's audit stream."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WRIT_LOG_PROJECT", PROJECT)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_root = _make_project(tmp_path)
    gate_name = "phase-a"
    _seed_cache(cache_dir, gate_name=gate_name)
    (project_root / ".claude" / "gates" / f"{gate_name}.approved").write_text("ok")

    proc = _run_hook(
        cache_dir=cache_dir,
        log_root=tmp_path / "logs",
        project_root=project_root,
        stop_hook_active=False,
    )

    assert proc.returncode == 0, (
        f"hook exited {proc.returncode} (silent set -e abort regression?); "
        f"stderr={proc.stderr!r}"
    )

    events = _audit_events(tmp_path / "logs")
    ours = [
        e
        for e in events
        if e.get("session") == SID
        and e.get("event") == "gate_denied_then_approved"
        and e.get("gate") == gate_name
    ]
    assert ours, (
        "expected a gate_denied_then_approved audit event; "
        f"audit stream held: {events!r}"
    )


def test_stop_hook_active_true_exits_zero_and_appends_no_events(
    tmp_path, monkeypatch
):
    """When CC re-invokes the Stop hook (stop_hook_active=true), the loop-breaker
    exits 0 immediately and appends NO events, even though the seeded state would
    otherwise produce a gate_denied_then_approved event."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WRIT_LOG_PROJECT", PROJECT)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_root = _make_project(tmp_path)
    gate_name = "phase-a"
    _seed_cache(cache_dir, gate_name=gate_name)
    (project_root / ".claude" / "gates" / f"{gate_name}.approved").write_text("ok")

    proc = _run_hook(
        cache_dir=cache_dir,
        log_root=tmp_path / "logs",
        project_root=project_root,
        stop_hook_active=True,
    )

    assert proc.returncode == 0, f"stderr={proc.stderr!r}"

    events = _audit_events(tmp_path / "logs")
    ours = [e for e in events if e.get("session") == SID]
    assert not ours, (
        "stop_hook_active=true must append no events; "
        f"found: {ours!r}"
    )


def test_phase_transition_time_deduped_against_metrics_stream_across_fires(
    tmp_path, monkeypatch
):
    """Regression: phase_transition_time is METRICS-classified in STREAM_MAP, so
    its dedup read must target the metrics stream, not audit.

    Before the fix the hook resolved a single FRICTION_LOG to the AUDIT stream and
    reused it for every event's dedup read. Event 2's row is appended to metrics
    (via friction-append.py -> the router), but its dedup read checked audit and
    never found the prior row, so each Stop fire re-appended the SAME
    phase_transition_time -> duplicate metrics rows.

    Fire the hook TWICE with identical gate state (two approved gates at fixed,
    distinct mtimes) and assert EXACTLY ONE phase_transition_time row lands in the
    metrics stream. FAILS against the pre-fix hook (2 rows), PASSES after."""
    monkeypatch.setenv("WRIT_LOG_ROOT", str(tmp_path / "logs"))
    monkeypatch.setenv("WRIT_LOG_PROJECT", PROJECT)
    monkeypatch.delenv("WRIT_FRICTION_LOG", raising=False)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_root = _make_project(tmp_path)
    # mode=work is required for Event 2; no invalidation_history needed here.
    (cache_dir / f"writ-session-{SID}.json").write_text(
        json.dumps(
            {
                "mode": "work",
                "invalidation_history": {},
                "phase_transitions": [],
                "phase_transitions_logged": 0,
            }
        )
    )
    _seed_two_gates_at_fixed_mtimes(project_root)

    def _fire() -> subprocess.CompletedProcess:
        # Re-assert the fixed mtimes before each fire: the gate state (and thus the
        # from_gate/to_gate/elapsed the hook derives) must be byte-identical across
        # both fires so only the dedup read can prevent the duplicate.
        _seed_two_gates_at_fixed_mtimes(project_root)
        return _run_hook(
            cache_dir=cache_dir,
            log_root=tmp_path / "logs",
            project_root=project_root,
            stop_hook_active=False,
        )

    first = _fire()
    assert first.returncode == 0, f"first fire stderr={first.stderr!r}"
    second = _fire()
    assert second.returncode == 0, f"second fire stderr={second.stderr!r}"

    metrics = _metrics_events(tmp_path / "logs")
    ptt = [
        e
        for e in metrics
        if e.get("session") == SID and e.get("event") == "phase_transition_time"
    ]
    assert len(ptt) == 1, (
        "expected EXACTLY ONE phase_transition_time row in the metrics stream "
        f"after two identical fires (dedup must read the metrics stream); "
        f"found {len(ptt)}: {ptt!r}"
    )

    # And it must NOT have leaked into the audit stream (proves correct routing).
    audit = _audit_events(tmp_path / "logs")
    audit_ptt = [
        e for e in audit if e.get("event") == "phase_transition_time"
    ]
    assert not audit_ptt, (
        "phase_transition_time must land in metrics, not audit; "
        f"found in audit: {audit_ptt!r}"
    )
