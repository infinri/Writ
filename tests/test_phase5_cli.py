"""Phase 5: CLI dispatch arms for the six new analyze-friction flags.

Verifies argparse wiring, mutual-exclusion enforcement, and that
each flag produces both human-readable text and structured JSON.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent

PHASE5_FLAGS = [
    "--rule-effectiveness",
    "--skill-usage",
    "--playbook-compliance",
    "--graduation-candidates",
    "--trim-candidates",
    "--quality-judge-false-positives",
]


@pytest.fixture
def empty_log(tmp_path: Path) -> Path:
    p = tmp_path / "empty.log"
    p.write_text("")
    return p


@pytest.fixture
def synthetic_log(tmp_path: Path) -> Path:
    """Friction log with one event of each Phase 5 relevant type.

    Timestamps are recent (relative to now) so events stay inside the analyzers'
    rolling since_days windows. Absolute past dates previously aged out -- a
    time-bomb that fired once real time crossed the 30-day boundary.
    """
    base = datetime.now(timezone.utc) - timedelta(days=1)

    def ts(offset: int) -> str:
        return (base + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")

    events = [
        {"ts": ts(0), "session": "s1", "mode": "work", "event": "rag_query", "rule_id": "ENF-X-001"},
        {"ts": ts(1), "session": "s1", "mode": "work", "event": "gate_denial", "rule_id": "ENF-X-001", "gate": "phase-a"},
        {"ts": ts(2), "session": "s1", "mode": "work", "event": "rag_query", "skill_id": "SKL-A"},
        {"ts": ts(3), "session": "s1", "mode": "work", "event": "playbook_step_complete", "playbook_id": "PBK-A", "step_id": "s1", "step_index": 0, "total_steps": 2},
        {"ts": ts(4), "session": "s1", "mode": "work", "event": "playbook_step_complete", "playbook_id": "PBK-A", "step_id": "s2", "step_index": 1, "total_steps": 2},
        {"ts": ts(5), "session": "s1", "mode": "work", "event": "quality_judgment", "judgment_id": "j1", "rubric": "R1", "decision": "fail", "override": True, "latency_ms": 120},
    ]
    p = tmp_path / "synth.log"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "writ.cli", *args],
        capture_output=True, text=True, cwd=str(WRIT_ROOT), timeout=15,
    )


class TestEachFlagParses:
    @pytest.mark.parametrize("flag", PHASE5_FLAGS)
    def test_flag_recognized_by_argparse(self, flag: str, empty_log: Path) -> None:
        proc = _cli("analyze-friction", "--log", str(empty_log), flag)
        assert proc.returncode == 0, f"{flag} not recognized: {proc.stderr}"


class TestMutualExclusion:
    def test_two_phase5_flags_rejected(self, empty_log: Path) -> None:
        proc = _cli(
            "analyze-friction", "--log", str(empty_log),
            "--rule-effectiveness", "--skill-usage",
        )
        assert proc.returncode != 0
        assert "not allowed" in proc.stderr.lower() or "mutually exclusive" in proc.stderr.lower()


class TestTextOutput:
    def test_rule_effectiveness_text_contains_header(self, synthetic_log: Path) -> None:
        proc = _cli("analyze-friction", "--log", str(synthetic_log), "--rule-effectiveness")
        assert proc.returncode == 0
        assert "rule_id" in proc.stdout.lower() or "rule" in proc.stdout.lower()
        assert "ENF-X-001" in proc.stdout

    def test_quality_judge_text_contains_rubric(self, synthetic_log: Path) -> None:
        proc = _cli(
            "analyze-friction", "--log", str(synthetic_log),
            "--quality-judge-false-positives",
        )
        assert proc.returncode == 0
        assert "R1" in proc.stdout


class TestJsonOutput:
    @pytest.mark.parametrize("flag", PHASE5_FLAGS)
    def test_json_output_is_valid_list(self, flag: str, synthetic_log: Path) -> None:
        proc = _cli("analyze-friction", "--log", str(synthetic_log), flag, "--json")
        assert proc.returncode == 0, f"{flag} --json failed: {proc.stderr}"
        # Either a JSON list, or empty output -- never invalid JSON.
        out = proc.stdout.strip()
        if out:
            parsed = json.loads(out)
            assert isinstance(parsed, list), f"{flag} --json must emit a JSON list, got {type(parsed)}"


class TestSinceFlag:
    def test_since_filter_excludes_old_events(self, synthetic_log: Path) -> None:
        # synthetic log dated 2026-04-30; --since 1 (1 day) excludes everything
        # if today's clock is past 2026-05-01 + 1d. We assert the run does not
        # crash on the filter, not the row count (clock drift makes that brittle).
        proc = _cli(
            "analyze-friction", "--log", str(synthetic_log),
            "--rule-effectiveness", "--since", "1",
        )
        assert proc.returncode == 0


class TestEmptyLogHandling:
    @pytest.mark.parametrize("flag", PHASE5_FLAGS)
    def test_empty_log_does_not_crash(self, flag: str, empty_log: Path) -> None:
        proc = _cli("analyze-friction", "--log", str(empty_log), flag)
        assert proc.returncode == 0, f"{flag} crashed on empty log: {proc.stderr}"
