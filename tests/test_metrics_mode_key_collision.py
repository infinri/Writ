"""`mode` means two different things in the event schema; the report must count one.

The metrics report's mode_distribution reads the top-level `mode` field off EVERY event
to answer "which governance mode was this session in". `retrieval_result` (metrics
stream) reuses the same key for the retrieval DELIVERY mode -- "standard", "summary",
"full", "abstained" -- and the reader concatenates the audit, friction and metrics
streams with metrics LAST, so those values overwrote each session's real mode. Live
output carried `{'work': 145, 'standard': 30, 'abstained': 3, ...}`, where two of those
three are not session modes at all and the sessions they came from were misreported.

The JSON key is deliberately NOT renamed here: `mode` is a published field on a live
event that other consumers read. The report filters instead.

Per TEST-REGRESSION-001 these assert the corrected behavior: they fail against the
unfiltered reader and pass once it filters.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "bin" / "lib" / "writ-session.py"


def _report(tmp_path: Path, events: list[dict]) -> dict:
    """Run the real metrics command over a log this test owns."""
    log = tmp_path / "workflow-friction.log"
    log.write_text("".join(json.dumps(e) + "\n" for e in events))
    res = subprocess.run(
        [sys.executable, str(HELPER), "metrics", "--log", str(log)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


class TestRetrievalDeliveryModeIsNotASessionMode:
    def test_delivery_modes_do_not_appear_in_the_histogram(self, tmp_path) -> None:
        """The three values the live report was polluted with, plus "full"."""
        events = [
            {"session": "s1", "mode": "work", "event": "gate_decision",
             "gate": "phase-a", "decision": "allow"},
            {"session": "s2", "mode": "standard", "event": "retrieval_result",
             "rule_count": 5},
            {"session": "s3", "mode": "summary", "event": "retrieval_result",
             "rule_count": 7},
            {"session": "s4", "mode": "abstained", "event": "retrieval_result",
             "rule_count": 0},
            {"session": "s5", "mode": "full", "event": "retrieval_result",
             "rule_count": 3},
        ]
        dist = _report(tmp_path, events)["mode_distribution"]
        strays = {k: v for k, v in dist.items()
                  if k in {"standard", "summary", "abstained", "full"}}
        assert strays == {}, (
            f"retrieval DELIVERY modes were counted as session modes: {strays}"
        )

    def test_a_retrieval_row_cannot_overwrite_the_sessions_real_mode(self, tmp_path) -> None:
        """The damaging case: one session, both event kinds. The report counts a session
        at its LAST mode, and metrics events sort after audit ones, so the delivery mode
        won -- the session was reported in a mode that does not exist."""
        events = [
            {"session": "s1", "mode": "work", "event": "gate_decision",
             "gate": "phase-a", "decision": "allow"},
            {"session": "s1", "mode": "abstained", "event": "retrieval_result",
             "rule_count": 0},
        ]
        dist = _report(tmp_path, events)["mode_distribution"]
        assert dist.get("work") == 1, (
            f"the session's real mode was lost to the retrieval row: {dist}"
        )
        assert "abstained" not in dist, f"delivery mode entered the histogram: {dist}"

    def test_retrieval_rows_are_still_counted_as_events(self, tmp_path) -> None:
        """Filtering is scoped to the mode histogram. The rows are real telemetry and
        must keep appearing everywhere else in the report."""
        events = [
            {"session": "s1", "mode": "abstained", "event": "retrieval_result",
             "rule_count": 0},
        ]
        report = _report(tmp_path, events)
        assert report["total_events"] == 1
        assert report["event_frequency"].get("retrieval_result") == 1


class TestGovernanceModesStillCount:
    def test_the_real_mode_is_still_counted(self, tmp_path) -> None:
        """Anti-vacuity: a filter that dropped everything would pass the tests above."""
        events = [
            {"session": "s1", "mode": "work", "event": "mode_change"},
            {"session": "s2", "mode": "investigate", "event": "gate_decision",
             "gate": "phase-a", "decision": "allow"},
            {"session": "s3", "mode": "work", "event": "hook_execution",
             "hook": "pre-validate-file", "duration_ms": 4},
        ]
        dist = _report(tmp_path, events)["mode_distribution"]
        assert dist.get("work") == 2, f"governance modes were dropped: {dist}"
        assert dist.get("investigate") == 1, f"governance modes were dropped: {dist}"

    def test_legacy_tier_events_are_still_mapped(self, tmp_path) -> None:
        """The legacy tier -> mode mapping is untouched by the filter."""
        events = [
            {"session": "s1", "tier": 2, "event": "phase_transition"},
            {"session": "s2", "tier": 0, "event": "approval_pattern_miss"},
        ]
        dist = _report(tmp_path, events)["mode_distribution"]
        assert dist.get("work") == 1
        assert dist.get("conversation") == 1
