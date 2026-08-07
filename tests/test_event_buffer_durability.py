"""Buffered telemetry must lose nothing that the per-hook spawn would have kept.

Pins the capabilities.md sections "Every event that reached the buffer reaches the log",
"Concurrent hooks do not corrupt each other's rows", and "A failed append never changes
what a hook does".

Why this exists: 8 of the 15 write-path hooks spawn python purely to append one
telemetry row, ~96ms per write (measured 2026-08-07). The row moves to a bash append
(0.018ms, no process) and one flush per turn emits the batch.

THE ARGUMENT THIS FILE HAS TO MAKE. Deferring a write is only acceptable if it loses
less than what it replaces. Today the row exists solely as arguments to a process that
may never start: a failed spawn drops it with no trace. After, the row is on disk before
any process runs. So these tests are written against the failure modes, not the happy
path: a turn that dies before Stop, two hooks appending at once, a row too big to append
atomically, and a disk that refuses the write.

Per ENF-PROC-TDD-001: skeletons approved before implementation.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON_SH = REPO / "bin" / "lib" / "common.sh"
FLUSH = REPO / "bin" / "lib" / "writ-flush-events.py"

# Linux does not interleave O_APPEND writes at or below this size. The whole
# concurrency guarantee rests on rows staying under it, so the number is named here and
# asserted, not assumed.
PIPE_BUF = 4096

# The buffer reuses the record/field separators the existing _WRIT_EVENT_BUF already
# uses, so there is one on-disk convention rather than two.
RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"


def _bash(script: str, env: dict[str, str] | None = None, timeout: int = 60):
    return subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nsource {COMMON_SH} >/dev/null 2>&1\n{script}"],
        capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})},
    )


@pytest.fixture
def buf_env(tmp_path: Path) -> dict[str, str]:
    """A private cache dir and log root, so no test touches real session state."""
    return {"WRIT_CACHE_DIR": str(tmp_path / "session"),
            "WRIT_LOG_DIR": str(tmp_path / "logs")}


def _append(session: str, hook: str, env: dict[str, str], duration: int = 5,
            rc: int = 0, mode: str = "work"):
    return _bash(
        f'writ_event_buffer_append "{session}" "{hook}" "{duration}" "{rc}" "{mode}"', env
    )


def _buffer_path(session: str, env: dict[str, str]) -> Path:
    out = _bash(f'writ_event_buffer_path "{session}"', env).stdout.strip()
    return Path(out)


def _rows(session: str, env: dict[str, str]) -> list[str]:
    p = _buffer_path(session, env)
    if not p.exists():
        return []
    return [r for r in p.read_text().split(RECORD_SEP) if r.strip()]


class TestNothingIsLost:
    def test_an_appended_row_lands_in_the_buffer(self, buf_env) -> None:
        _append("s-1", "validate-file", buf_env)
        assert len(_rows("s-1", buf_env)) == 1

    def test_every_appended_row_survives_to_the_flush(self, buf_env) -> None:
        """The core promise: 8 hooks append, 8 events come out."""
        for i in range(8):
            _append("s-2", f"hook-{i}", buf_env)
        assert len(_rows("s-2", buf_env)) == 8
        proc = subprocess.run(
            ["python3", str(FLUSH), "s-2"], capture_output=True, text=True,
            timeout=60, env={**os.environ, **buf_env},
        )
        assert proc.returncode == 0, proc.stderr[:400]
        emitted = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(emitted) == 8, f"appended 8 rows, flushed {len(emitted)}"
        assert {e["hook_name"] for e in emitted} == {f"hook-{i}" for i in range(8)}

    def test_the_flush_removes_the_buffer_it_drained(self, buf_env) -> None:
        _append("s-3", "validate-file", buf_env)
        subprocess.run(["python3", str(FLUSH), "s-3"], capture_output=True,
                       timeout=60, env={**os.environ, **buf_env})
        assert not _buffer_path("s-3", buf_env).exists()

    def test_a_second_flush_emits_nothing_rather_than_duplicating(self, buf_env) -> None:
        """Requeue semantics must not become replay semantics: the rows are emitted
        once, then the buffer is gone."""
        _append("s-4", "validate-file", buf_env)
        first = subprocess.run(["python3", str(FLUSH), "s-4"], capture_output=True,
                               text=True, timeout=60, env={**os.environ, **buf_env})
        second = subprocess.run(["python3", str(FLUSH), "s-4"], capture_output=True,
                                text=True, timeout=60, env={**os.environ, **buf_env})
        assert first.stdout.strip() != ""
        assert second.stdout.strip() == ""
        assert second.returncode == 0

    def test_an_orphan_buffer_from_a_turn_that_never_stopped_is_drained(self, buf_env) -> None:
        """The crash path (ERR-GRACEFUL-002). A turn that dies before Stop leaves rows
        on disk; the drain must still find and emit them."""
        _append("s-5", "validate-file", buf_env)
        # No Stop hook runs. SessionEnd drains instead.
        proc = subprocess.run(
            ["bash", str(REPO / "hooks" / "scripts" / "writ-session-end.sh")],
            input=json.dumps({"session_id": "s-5"}), capture_output=True, text=True,
            timeout=120, env={**os.environ, **buf_env},
        )
        assert proc.returncode == 0
        assert not _buffer_path("s-5", buf_env).exists(), (
            "SessionEnd left the orphan buffer on disk, so those rows are stranded"
        )


class TestConcurrency:
    def test_parallel_appends_produce_whole_separate_rows(self, buf_env) -> None:
        """20 hooks appending at once must yield 20 intact rows.

        This is the test that would catch interleaving, which is the failure mode that
        makes a shared append buffer dangerous. It asserts the COUNT and that every row
        has the right field count, because a torn write shows up as a row with too few
        or too many fields rather than as a missing one.
        """
        script = " & ".join(
            f'writ_event_buffer_append "s-6" "hook-{i}" "5" "0" "work"' for i in range(20)
        ) + " & wait"
        _bash(script, buf_env, timeout=120)
        rows = _rows("s-6", buf_env)
        assert len(rows) == 20, f"expected 20 rows, got {len(rows)}"
        for r in rows:
            assert len(r.split(FIELD_SEP)) == 6, f"torn row: {r!r}"

    def test_every_row_stays_under_the_atomic_append_limit(self, buf_env) -> None:
        """The concurrency guarantee is only real while rows fit in PIPE_BUF."""
        _append("s-7", "validate-file", buf_env)
        assert _buffer_path("s-7", buf_env).stat().st_size < PIPE_BUF

    def test_an_oversized_field_is_truncated_rather_than_risking_a_torn_row(
        self, buf_env
    ) -> None:
        """A hook name (or any field) long enough to push the row past PIPE_BUF must be
        cut down, and the row must say so, so the size bound holds by construction
        instead of by hoping fields stay short."""
        _append("s-8", "x" * (PIPE_BUF * 2), buf_env)
        raw = _buffer_path("s-8", buf_env).read_text()
        assert len(raw) < PIPE_BUF, "row exceeded the atomic-append limit"

        # Asserts the FLUSHED event, not a substring of the raw row. The skeleton
        # looked for the literal word "truncated" in the buffer; the row records it as
        # a positional flag instead, which the flush maps to this field. Checking the
        # emitted event is the stronger test: it proves the signal survives the whole
        # path to the log, rather than proving a particular byte is present on disk.
        proc = subprocess.run(
            ["python3", str(FLUSH), "s-8"], capture_output=True, text=True,
            timeout=60, env={**os.environ, **buf_env},
        )
        events = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]
        assert len(events) == 1, f"expected one event, got {len(events)}"
        assert events[0]["truncated"] is True, (
            "the row was cut but the emitted event does not say so, so a truncated "
            "hook name would silently read as the real one"
        )


class TestFailureIsolation:
    def test_a_failed_append_does_not_change_the_hook_exit_status(self, tmp_path) -> None:
        """Telemetry failure must never become enforcement failure. An unwritable
        buffer directory must leave the hook's own exit code untouched."""
        ro = tmp_path / "readonly"
        ro.mkdir()
        ro.chmod(0o500)
        env = {"WRIT_CACHE_DIR": str(ro), "WRIT_LOG_DIR": str(tmp_path / "logs")}
        try:
            proc = _bash('writ_event_buffer_append "s-9" "h" "5" "0" "work"; exit 7', env)
            assert proc.returncode == 7, (
                f"a failed append changed the exit status to {proc.returncode}"
            )
        finally:
            ro.chmod(0o700)

    def test_a_corrupt_buffer_does_not_abort_the_flush(self, buf_env) -> None:
        """A partially written or garbled buffer must not strand the rows around it."""
        _append("s-10", "good-hook", buf_env)
        p = _buffer_path("s-10", buf_env)
        p.write_text(p.read_text() + RECORD_SEP + "garbage-without-separators")
        proc = subprocess.run(["python3", str(FLUSH), "s-10"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert proc.returncode == 0
        assert "good-hook" in proc.stdout, "a bad row took a good one down with it"


class TestTheSpawnIsActuallyGone:
    """Anti-vacuity for the whole change: the tests above would still pass if the trap
    kept spawning python AND also buffered."""

    def test_the_exit_trap_buffers_and_does_not_double_emit(self) -> None:
        """Asserts the design the plan approved, which the skeleton overshot.

        The skeleton demanded python be ABSENT from the trap. The plan keeps it for
        `gate_decision` on purpose: those are governance records with 365-day retention
        and they are rare, so a different risk calculus applies than to a metrics row.
        What actually has to hold is narrower and more important:
          1. the hook_execution row is appended by bash, and
          2. the python block no longer ALSO emits hook_execution, or every hook would
             be logged twice on every write, and
          3. that block is skipped entirely when no gate decision was buffered, which
             is what makes the common path spawn-free.
        """
        src = COMMON_SH.read_text()
        start = src.index("_writ_hook_exit_trap()")
        body = src[start:src.index("\n}\n", start)]

        assert "writ_event_buffer_append" in body, (
            "the exit trap does not use the buffer, so no spawn was removed"
        )
        assert 'emit(None, "hook_execution"' not in body, (
            "the trap still emits hook_execution directly as well as buffering it, "
            "so every hook would be counted twice"
        )
        guard = body.index('if [ -s "${_WRIT_EVENT_BUF:-/nonexistent}" ]')
        assert guard < body.index("python3 -c"), (
            "the python spawn is not behind the gate-decision guard, so it still runs "
            "on every hook rather than only when a gate decided"
        )

    @pytest.mark.skipif(shutil.which("strace") is None, reason="strace unavailable")
    def test_a_hook_run_spawns_no_python_for_telemetry(self, tmp_path) -> None:
        """Measured, not inferred: run one instrumented hook and count python starts."""
        trace = tmp_path / "t.trace"
        subprocess.run(
            ["strace", "-f", "-qq", "-e", "trace=execve", "-o", str(trace),
             "bash", str(REPO / "hooks" / "scripts" / "validate-handoff.sh")],
            input=json.dumps({"session_id": "s-11", "tool_name": "Write",
                              "tool_input": {"file_path": "/tmp/x.py", "content": "x"}}),
            capture_output=True, text=True, timeout=180,
        )
        text = trace.read_text(errors="replace")
        assert sum(1 for ln in text.splitlines() if 'python3"' in ln) == 0, (
            "this hook still starts a python interpreter on a plain write"
        )
