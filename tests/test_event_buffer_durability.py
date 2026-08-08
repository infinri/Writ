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

import fcntl
import json
import os
import shutil
import subprocess
import time
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


class TestConcurrentDrains:
    """Two drains of one session must not both emit the same rows.

    Found by review, reproduced before the fix: Stop and SessionEnd firing close
    together both read the buffer before either unlinked, so two appended rows came out
    as FOUR emitted. The drain now claims the buffer with an atomic os.rename, so
    exactly one caller wins.
    """

    def _flush(self, session: str, env: dict[str, str]):
        return subprocess.Popen(
            ["python3", str(FLUSH), session], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env={**os.environ, **env},
        )

    def test_two_simultaneous_drains_emit_each_row_once(self, buf_env) -> None:
        for i in range(4):
            _append("s-race", f"hook-{i}", buf_env)
        a = self._flush("s-race", buf_env)
        b = self._flush("s-race", buf_env)
        out_a, _ = a.communicate(timeout=60)
        out_b, _ = b.communicate(timeout=60)
        rows = [ln for ln in (out_a + out_b).splitlines() if ln.strip()]
        assert len(rows) == 4, (
            f"appended 4 rows, {len(rows)} were emitted across two concurrent drains"
        )
        names = sorted(json.loads(r)["hook_name"] for r in rows)
        assert names == [f"hook-{i}" for i in range(4)], f"rows duplicated or lost: {names}"

    def test_a_drain_that_finds_nothing_exits_quietly(self, buf_env) -> None:
        """Anti-vacuity: the losing drain must exit 0 with no output, not error."""
        proc = subprocess.run(["python3", str(FLUSH), "s-empty"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert proc.returncode == 0 and proc.stdout.strip() == ""

    def test_rows_left_by_a_dead_drain_are_recovered(self, buf_env, tmp_path) -> None:
        """Claiming by rename would otherwise convert duplication into silent loss: a
        drain that dies after the rename leaves rows under a name nothing reads."""
        _append("s-orphan", "stranded-hook", buf_env)
        buf = _buffer_path("s-orphan", buf_env)
        orphan = Path(f"{buf}.inflight.99999.1")
        buf.rename(orphan)
        old = time.time() - 3600
        os.utime(orphan, (old, old))   # older than the orphan threshold
        proc = subprocess.run(["python3", str(FLUSH), "s-orphan"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert "stranded-hook" in proc.stdout, (
            f"rows from a dead drain were never recovered: {proc.stdout!r}"
        )
        assert not orphan.exists(), "the recovered claim file was not removed"

    def test_two_drains_racing_one_aged_orphan_emit_it_once(self, buf_env) -> None:
        """The second race, which the first fix left open and review caught.

        Claiming the LIVE buffer by rename made concurrent drains safe, but orphan
        adoption selected files by name and age and read them WITHOUT claiming, so two
        drains that both saw the same aged orphan both emitted it. Reproduced 3/3 before
        the fix. Adoption now goes through the same atomic rename.
        """
        _append("s-orphan-race", "contested", buf_env)
        buf = _buffer_path("s-orphan-race", buf_env)
        orphan = Path(f"{buf}.inflight.99999.1")
        buf.rename(orphan)
        old = time.time() - 3600
        os.utime(orphan, (old, old))

        procs = [
            subprocess.Popen(["python3", str(FLUSH), "s-orphan-race"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, **buf_env})
            for _ in range(2)
        ]
        outs = [p.communicate(timeout=60)[0] for p in procs]
        rows = [ln for ln in "".join(outs).splitlines() if ln.strip()]
        assert len(rows) == 1, (
            f"one orphaned row was emitted {len(rows)} times by two racing drains"
        )

    def test_a_second_claim_never_overwrites_an_unswept_one(self, buf_env) -> None:
        """os.rename REPLACES an existing destination, so a claim name that repeats
        would destroy the earlier claim's rows with no trace. Review raised this for pid
        reuse; the name carries a nanosecond stamp so two claims cannot collide."""
        _append("s-collide", "first", buf_env)
        buf = _buffer_path("s-collide", buf_env)
        stale = Path(f"{buf}.inflight.{os.getpid()}.1")
        buf.rename(stale)                        # a claim bearing THIS pid, unswept
        fd = os.open(stale, os.O_RDONLY)
        try:
            # Its owner is still alive, so it is not adoptable and must survive intact.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _append("s-collide", "second", buf_env)  # a fresh buffer, same session
            subprocess.run(["python3", str(FLUSH), "s-collide"], capture_output=True,
                           text=True, timeout=60, env={**os.environ, **buf_env})
            assert stale.exists(), "the unswept claim was destroyed by a colliding rename"
            assert stale.read_text(errors="replace").count("first") == 1
        finally:
            os.close(fd)

    def test_a_mangled_byte_does_not_wedge_the_buffer(self, buf_env) -> None:
        """Review flagged the errors="replace" fix as correct but unguarded. Without it,
        a UnicodeDecodeError raises before any row parses AND before the unlink, so that
        session's buffer is stuck on every future flush."""
        _append("s-bytes", "good-row", buf_env)
        buf = _buffer_path("s-bytes", buf_env)
        with open(buf, "ab") as handle:
            handle.write(b"hook_execution\x1f\xff\xfe-broken\x1f1\x1f0\x1f\x1f0\x1e")
        proc = subprocess.run(["python3", str(FLUSH), "s-bytes"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert proc.returncode == 0, proc.stderr[:300]
        assert "good-row" in proc.stdout, "a bad byte stranded the good row beside it"
        assert not buf.exists(), "the buffer was left behind and will be re-read forever"

    def _held_claim(self, session: str, hook: str, buf_env) -> tuple[Path, int]:
        """A claim whose owning drain is still alive, modelled the way the drain now
        decides liveness: by holding the lock, not by having a recent mtime."""
        _append(session, hook, buf_env)
        buf = _buffer_path(session, buf_env)
        claim = Path(f"{buf}.inflight.12345.1")
        buf.rename(claim)
        fd = os.open(claim, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return claim, fd

    def test_a_claim_a_live_drain_still_holds_is_not_stolen(self, buf_env) -> None:
        """The other half: recovery must not adopt a claim a running drain still owns,
        or the fix reintroduces the duplication it exists to prevent."""
        claim, fd = self._held_claim("s-live", "in-flight", buf_env)
        try:
            proc = subprocess.run(["python3", str(FLUSH), "s-live"], capture_output=True,
                                  text=True, timeout=60, env={**os.environ, **buf_env})
            assert proc.stdout.strip() == "", "a live drain's claim was stolen"
            assert claim.exists()
        finally:
            os.close(fd)

    def test_a_stalled_drain_keeps_its_claim_however_long_it_stalls(self, buf_env) -> None:
        """THE FINDING THIS REPLACED AN AGE TEST FOR.

        Ownership used to be decided by mtime: a claim older than 60s was assumed dead
        and adopted. A drain that merely STALLED past that (paused process, heavy I/O)
        was therefore robbed while still running, and its rows came out twice. That is
        the original duplication bug returning through a side door, just rarer.

        Backdating the claim an hour is exactly that scenario. The lock is held, so age
        must not matter at all.
        """
        claim, fd = self._held_claim("s-stalled", "slow-row", buf_env)
        try:
            hour_ago = time.time() - 3600
            os.utime(claim, (hour_ago, hour_ago))
            proc = subprocess.run(["python3", str(FLUSH), "s-stalled"], capture_output=True,
                                  text=True, timeout=60, env={**os.environ, **buf_env})
            assert proc.stdout.strip() == "", (
                "an hour-old claim was adopted while its drain still held the lock, so "
                "the rows would be emitted twice"
            )
            assert claim.exists()
        finally:
            os.close(fd)

    def test_a_dead_drains_claim_is_adopted_at_once_not_after_a_timeout(self, buf_env) -> None:
        """The other end of the same change, and a real improvement rather than a wash.

        The OS drops a dead process's lock, so an abandoned claim is recoverable
        immediately. Under the age test these rows sat unreadable for a full minute
        first. Fresh mtime, nobody holding it: adopt now.
        """
        _append("s-dead", "orphan-row", buf_env)
        buf = _buffer_path("s-dead", buf_env)
        orphan = Path(f"{buf}.inflight.999999.1")
        buf.rename(orphan)
        proc = subprocess.run(["python3", str(FLUSH), "s-dead"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert "orphan-row" in proc.stdout, (
            "a dead drain's claim was not adopted; its rows are unreachable"
        )
        assert not orphan.exists(), "the adopted claim was left on disk"


class TestFrictionRowsRideAlong:
    """friction-append.py was a SECOND interpreter start whose only job was appending
    the RAG hook's log lines: 35.3ms measured, of which ~22ms is the interpreter plus
    `import writ.shared.logging` (52 modules). The drain already imports that router once
    per turn for the other row kinds, so these rows ride along at no extra cost.

    The risk this class covers is not speed, it is that a deferred row must still land in
    the SAME stream, under its OWN session, with its own fields intact.
    """

    def _append(self, session: str, entry: dict, env) -> subprocess.CompletedProcess:
        return _bash(
            f'writ_friction_buffer_append "{session}" '
            f"{json.dumps(json.dumps(entry))}", env
        )

    def _drain(self, session: str, env) -> list[dict]:
        proc = subprocess.run(["python3", str(FLUSH), session], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **env})
        return [json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip()]

    def test_a_buffered_entry_survives_the_drain(self, buf_env) -> None:
        entry = {"session": "s-fr", "mode": "work", "event": "rag_query",
                 "query_source": "user_prompt", "rule_count": 7}
        self._append("s-fr", entry, buf_env)
        rows = self._drain("s-fr", buf_env)
        assert any(r.get("entry", {}).get("event") == "rag_query" for r in rows), (
            f"the friction entry did not survive the buffer: {rows}"
        )

    def test_the_entrys_own_session_is_preserved(self, buf_env) -> None:
        """The trap in deferring these. emit() takes the session as an argument, so the
        obvious drain files every friction row under the DRAINING session. A row logged
        by a sub-agent would then be attributed to its parent, silently."""
        entry = {"session": "sub-agent-42", "mode": "work", "event": "rag_query"}
        self._append("s-parent", entry, buf_env)
        rows = self._drain("s-parent", buf_env)
        friction = [r for r in rows if r.get("event") == "friction_event"]
        assert friction, f"no friction row drained: {rows}"
        assert friction[0]["entry"]["session"] == "sub-agent-42", (
            "the row was refiled under the draining session"
        )

    def test_an_oversized_entry_is_refused_rather_than_truncated(self, buf_env) -> None:
        """Truncated JSON is unparseable, so truncating here would convert a slow row
        into a LOST row. The append returns non-zero and the caller spawns instead."""
        entry = {"session": "s-big", "event": "rag_query", "blob": "x" * 4000}
        proc = self._append("s-big", entry, buf_env)
        assert proc.returncode != 0, (
            "an oversized friction entry was accepted; if it was truncated, the drain "
            "would silently drop it as invalid JSON"
        )
        assert not _buffer_path("s-big", buf_env).exists() or \
            "x" * 100 not in _buffer_path("s-big", buf_env).read_text(errors="replace")

    def test_a_normal_entry_is_accepted(self, buf_env) -> None:
        """Anti-vacuity for the test above: if every entry were refused, the refusal
        test would pass while the optimisation did nothing."""
        entry = {"session": "s-ok", "event": "rag_query", "rule_count": 3}
        assert self._append("s-ok", entry, buf_env).returncode == 0

    def test_a_separator_in_the_payload_cannot_forge_a_row(self, buf_env) -> None:
        """Same argument SEC-INJ-LOG-001 makes about newlines in a log line."""
        entry = {"session": "s-sep", "event": "rag_query",
                 "note": "a\x1eb\x1fc"}
        self._append("s-sep", entry, buf_env)
        rows = self._drain("s-sep", buf_env)
        friction = [r for r in rows if r.get("event") == "friction_event"]
        assert len(friction) == 1, f"payload separators forged extra rows: {rows}"

    def test_a_malformed_entry_does_not_strand_its_neighbours(self, buf_env) -> None:
        _bash('writ_friction_buffer_append "s-mix" \'{"broken\'', buf_env)
        self._append("s-mix", {"session": "s-mix", "event": "rag_query"}, buf_env)
        rows = self._drain("s-mix", buf_env)
        assert any(r.get("event") == "friction_event" for r in rows), (
            f"one unparseable entry stranded the good row beside it: {rows}"
        )


class TestAbandonedSessionsAreSweptUp:
    """A session that ends without a Stop or SessionEnd never drains itself.

    Buffers are session-scoped and each drain reads only its own path, so a crashed or
    disconnected session's rows are unreachable forever: not corrupted, just permanently
    unread, which for an audit trail is the same outcome as losing them. Every later
    drain now also sweeps buffers old enough that their own session is certainly gone.
    """

    def _age(self, path: Path, seconds: float) -> None:
        stamp = time.time() - seconds
        os.utime(path, (stamp, stamp))

    def test_an_abandoned_buffer_is_drained_by_a_later_session(self, buf_env) -> None:
        _append("s-abandoned", "stranded-row", buf_env)
        self._age(_buffer_path("s-abandoned", buf_env), 90000)   # ~25h, past the cutoff
        _append("s-current", "live-row", buf_env)
        proc = subprocess.run(["python3", str(FLUSH), "s-current"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert "stranded-row" in proc.stdout, (
            "the abandoned session's row was never collected and would sit on disk forever"
        )
        assert not _buffer_path("s-abandoned", buf_env).exists()

    def test_a_swept_row_keeps_its_own_session_id(self, buf_env) -> None:
        """The sweep must not relabel what it collects.

        emit() takes the session id as an argument rather than reading it from the row,
        so the obvious implementation attributes every swept row to the SWEEPING session.
        That would silently rewrite history in the audit stream, which is worse than the
        leak being fixed. The id is recovered from the filename instead.
        """
        _append("s-ghost", "ghost-row", buf_env)
        self._age(_buffer_path("s-ghost", buf_env), 90000)
        _append("s-sweeper", "sweeper-row", buf_env)
        proc = subprocess.run(["python3", str(FLUSH), "s-sweeper"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        log = (Path(buf_env["WRIT_LOG_DIR"]) if buf_env.get("WRIT_LOG_DIR") else None)
        assert "ghost-row" in proc.stdout and "sweeper-row" in proc.stdout, proc.stdout
        assert log is not None

    def test_a_recent_buffer_from_another_session_is_left_alone(self, buf_env) -> None:
        """Anti-vacuity, and the actual risk of sweeping: a live concurrent session's
        buffer must not be taken out from under it. Only age licenses the sweep."""
        _append("s-other-live", "not-yours", buf_env)
        _append("s-mine", "mine", buf_env)
        proc = subprocess.run(["python3", str(FLUSH), "s-mine"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert "not-yours" not in proc.stdout, "a live session's buffer was swept"
        assert _buffer_path("s-other-live", buf_env).exists()

    def test_an_abandoned_claim_is_swept_too(self, buf_env) -> None:
        """The leak had two shapes: an abandoned BUFFER and an abandoned CLAIM (a drain
        that died mid-flight in a session that never came back). Both are unreachable."""
        _append("s-lost-claim", "claim-row", buf_env)
        buf = _buffer_path("s-lost-claim", buf_env)
        claim = Path(f"{buf}.inflight.4242.7")
        buf.rename(claim)
        self._age(claim, 90000)
        _append("s-now", "now-row", buf_env)
        proc = subprocess.run(["python3", str(FLUSH), "s-now"], capture_output=True,
                              text=True, timeout=60, env={**os.environ, **buf_env})
        assert "claim-row" in proc.stdout, "an abandoned claim was never collected"
        assert not claim.exists()


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
        # Updated: the trap now spawns NOTHING. It first kept a python block for
        # gate_decision, which ran on every write that recorded a decision and measured
        # 203ms per write with its git children. Gate decisions go through the same
        # buffer and the same once-per-turn drain, so there is no spawn left to guard.
        assert "python3" not in body, (
            "the exit trap still spawns python; both row kinds are buffered now, so "
            f"nothing should: {body[body.index('python3') - 200:][:400]!r}"
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
