"""Plan f7fc2b37-9a53-4011-a69f-e6b97f5e45fe, item 1: `writ_critical` must not crash
under `set -u` when jq is absent or `WRIT_NO_JQ` is set.

RED AT HEAD (WRIT_NO_JQ=1 mode): `writ_critical` (bin/lib/common.sh:1487-1507) declares
`local extra` at line 1490 with no assignment. The no-jq branch (1497) reads `$extra`
before the fallback assigns it, so under `set -euo pipefail` bash aborts with
`extra: unbound variable` -- the calling hook dies at the exact moment it was reporting
a critical event, and the command after it never runs.

Isolation: every subprocess gets its own WRIT_CACHE_DIR, WRIT_FRICTION_LOG and HOME
under tmp_path; none of this reaches a real cache or the live daemon (writ_critical
makes no network call at all).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"

SESSION = "wc-nounset-session"
COMPONENT = "test-component"
MESSAGE = "something writ_critical needs to report"


def _run(tmp_path: Path, *, no_jq: bool) -> subprocess.CompletedProcess:
    cache_dir = tmp_path / "cache"
    friction_log = tmp_path / "friction.log"
    home = tmp_path / "home"
    cache_dir.mkdir(parents=True, exist_ok=True)
    home.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "WRIT_CACHE_DIR": str(cache_dir),
        "WRIT_FRICTION_LOG": str(friction_log),
        "HOME": str(home),
    }
    if no_jq:
        env["WRIT_NO_JQ"] = "1"
    else:
        env.pop("WRIT_NO_JQ", None)

    script = (
        "set -euo pipefail\n"
        f"source '{COMMON_SH}'\n"
        f"writ_critical '{COMPONENT}' '{MESSAGE}' '{SESSION}'\n"
        "echo AFTER\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30,
    )


def _critical_rows(tmp_path: Path) -> list[dict]:
    friction_log = tmp_path / "friction.log"
    if not friction_log.exists():
        return []
    rows = []
    for line in friction_log.read_text().splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "critical_error":
            rows.append(row)
    return rows


class TestNoJqFallbackDoesNotCrashUnderSetU:
    """The defect, directly: WRIT_NO_JQ=1 is the faithful stand-in for a machine
    with no jq on PATH (the no-jq branch is the one `command -v jq` failing also
    takes), and it is deterministic on any runner."""

    def test_exits_zero(self, tmp_path):
        result = _run(tmp_path, no_jq=True)
        assert result.returncode == 0, (
            f"writ_critical aborted the caller under set -u: {result.stderr!r}"
        )

    def test_the_next_command_still_runs(self, tmp_path):
        result = _run(tmp_path, no_jq=True)
        assert "AFTER" in result.stdout.splitlines(), (
            "the command after writ_critical never ran; the caller's own hook "
            f"work after it would be lost too: {result.stdout!r}"
        )

    def test_the_critical_line_reaches_stderr(self, tmp_path):
        result = _run(tmp_path, no_jq=True)
        assert f"[WRIT CRITICAL] {COMPONENT}: {MESSAGE}" in result.stderr, result.stderr

    def test_one_critical_error_row_lands_in_the_errors_stream(self, tmp_path):
        _run(tmp_path, no_jq=True)
        rows = _critical_rows(tmp_path)
        assert len(rows) == 1, (
            f"expected exactly one critical_error row, got {rows}"
        )
        row = rows[0]
        assert row.get("severity") == "critical", row
        assert row.get("component") == COMPONENT, row


class TestJqPresentBehavesIdentically:
    """Regression guard (TEST-REGRESSION-001): the jq branch is not the one the
    defect lives in, and this fix must not change its behavior."""

    def _require_jq(self):
        if subprocess.run(["bash", "-c", "command -v jq"],
                          capture_output=True).returncode != 0:
            pytest.skip("jq is not installed on this machine")

    def test_exits_zero(self, tmp_path):
        self._require_jq()
        result = _run(tmp_path, no_jq=False)
        assert result.returncode == 0, result.stderr

    def test_the_next_command_still_runs(self, tmp_path):
        self._require_jq()
        result = _run(tmp_path, no_jq=False)
        assert "AFTER" in result.stdout.splitlines(), result.stdout

    def test_the_critical_line_reaches_stderr(self, tmp_path):
        self._require_jq()
        result = _run(tmp_path, no_jq=False)
        assert f"[WRIT CRITICAL] {COMPONENT}: {MESSAGE}" in result.stderr, result.stderr

    def test_one_critical_error_row_lands_in_the_errors_stream(self, tmp_path):
        self._require_jq()
        _run(tmp_path, no_jq=False)
        rows = _critical_rows(tmp_path)
        assert len(rows) == 1, rows
        row = rows[0]
        assert row.get("severity") == "critical", row
        assert row.get("component") == COMPONENT, row
