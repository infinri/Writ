"""Calibration logging and escalation decision logic.

Tracks calibration state across requests and server restarts via
line count of the JSONL log file. Provides escalation decisions
based on pattern match confidence and retrieval scores.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from writ.analysis import Finding

CALIBRATION_THRESHOLD = 100
RELEVANCE_SCORE_THRESHOLD = 0.6
DEFAULT_LOG_PATH = "/tmp/writ-calibration.jsonl"


class Instrumentation:
    """Calibration and escalation tracking for /analyze."""

    def __init__(self, log_path: str | Path = DEFAULT_LOG_PATH) -> None:
        self._log_path = Path(log_path)
        self._counter = self._read_counter()

    def _read_counter(self) -> int:
        """Count lines in JSONL file to resume calibration state."""
        if not self._log_path.exists():
            return 0
        try:
            with open(self._log_path) as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def get_mode(self) -> str:
        """Current mode: 'calibration' or 'production'."""
        return "calibration" if self._counter < CALIBRATION_THRESHOLD else "production"

    def should_escalate(
        self,
        matches: list[Finding],
        retrieval_scores: dict[str, float],
    ) -> bool:
        """Decide whether to escalate from pattern matching to LLM.

        Calibration mode: always escalate (log both for paired comparison).
        Production mode: escalate on ambiguous matches or high-relevance rules.
        """
        if self.get_mode() == "calibration":
            return True

        # Any medium or low confidence match: escalate
        for m in matches:
            if m.confidence in ("medium", "low"):
                return True

        # No matches but high-relevance rules exist: escalate
        if not matches:
            has_relevant = any(
                score > RELEVANCE_SCORE_THRESHOLD
                for score in retrieval_scores.values()
            )
            if has_relevant:
                return True

        return False

    def log_calibration(
        self,
        file_path: str,
        phase: str,
        pattern_findings: list[Finding],
        llm_findings: list[Finding],
        rules_checked: list[str],
        retrieval_scores: dict[str, float],
    ) -> None:
        """Append paired comparison to calibration log."""
        pattern_verdict = _derive_verdict(pattern_findings)
        llm_verdict = _derive_verdict(llm_findings)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "file_path": file_path,
            "phase": phase,
            "pattern_findings": [f.model_dump() for f in pattern_findings],
            "llm_findings": [f.model_dump() for f in llm_findings],
            "pattern_verdict": pattern_verdict,
            "llm_verdict": llm_verdict,
            "agreed": pattern_verdict == llm_verdict,
            "rules_checked": rules_checked,
            "retrieval_scores": retrieval_scores,
        }

        try:
            with open(self._log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
            self._counter += 1
        except OSError:
            pass  # Best-effort logging


def _derive_verdict(findings: list[Finding]) -> str:
    """Derive verdict from a list of findings."""
    if not findings:
        return "pass"
    statuses = {f.status for f in findings}
    if "violated" in statuses:
        return "fail"
    if "uncertain" in statuses:
        return "warn"
    return "pass"
