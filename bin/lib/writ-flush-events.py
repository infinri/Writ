#!/usr/bin/env python3
"""Drain one session's buffered hook-telemetry rows into the log streams.

Called once per turn (Stop) instead of once per hook. 8 of the 15 hooks a file write
fires used to spawn python purely to append a `hook_execution` row, ~96ms per write
(measured 2026-08-07); they now append with bash and this drains the batch in a single
interpreter start.

ORDER OF OPERATIONS IS THE DURABILITY ARGUMENT: read, emit, THEN unlink. A crash before
the unlink replays the rows on the next drain rather than losing them, which is the
trade this design is built on. The reverse order would turn a crash into silent data
loss, which is exactly what the per-hook spawn already did whenever the spawn failed.

Usage: writ-flush-events.py <session_id>
Stdout: one JSON object per emitted row, so callers and tests can see what landed.
Never fails the caller: telemetry must not become an enforcement failure.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"
FIELDS = 6       # hook_execution: kind, hook, duration_ms, exit_code, mode, truncated
GATE_FIELDS = 7  # gate_decision: kind, gate, decision, reason, target, mode, decided_at
FRICTION_FIELDS = 2  # friction_event: kind, entry (one complete JSON object)


BUF_PREFIX = "writ-events-"
BUF_SUFFIX = ".buf"


def _buffer_path(session_id: str) -> str:
    cache_dir = os.environ.get("WRIT_CACHE_DIR")
    if not cache_dir:
        skill_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cache_dir = os.path.join(skill_dir, "var", "session")
    return os.path.join(cache_dir, f"{BUF_PREFIX}{session_id or 'unknown'}{BUF_SUFFIX}")


def _parse(raw: str) -> list[dict]:
    """Rows that survive parsing are kept; a garbled row is skipped, not fatal.

    One corrupt record must not strand the good records around it. That is the whole
    reason this is a per-record loop rather than one strict decode of the file.
    """
    rows = []
    for record in raw.split(RECORD_SEP):
        if not record.strip():
            continue
        parts = record.split(FIELD_SEP)
        if parts[0] == "hook_execution" and len(parts) == FIELDS:
            _, hook, duration, exit_code, mode, truncated = parts
            try:
                duration_ms = int(duration or 0)
                rc = int(exit_code or 0)
            except ValueError:
                continue
            rows.append({
                "event": "hook_execution",
                "hook_name": hook or "unknown",
                "duration_ms": duration_ms,
                "exit_code": rc,
                "mode": mode or None,
                "truncated": truncated == "1",
            })
        elif parts[0] == "gate_decision" and len(parts) == GATE_FIELDS:
            # Governance record, 365-day retention. It reaches the buffer before any
            # process runs, where the old spawn-at-exit form held it only as process
            # arguments and lost it outright if the spawn failed.
            _, gate, decision, reason, target, mode, decided_at = parts
            rows.append({
                "event": "gate_decision",
                "gate": gate,
                "decision": decision,
                "reason": reason,
                "target": target,
                "mode": mode or None,
                # When the gate DECIDED. emit() stamps `ts` when it runs, which for a
                # drained row is turn end, so without this the record would claim a time
                # several seconds after the fact and several decisions in one turn would
                # collapse onto the same instant.
                # FORMAT: epoch seconds as TEXT, unlike `ts` beside it, which is ISO-8601.
                # Two timestamp conventions in one record is a trap, so anything sorting
                # or diffing on this needs int() first.
                "decided_at": decided_at,
            })
        elif parts[0] == "friction_event" and len(parts) == FRICTION_FIELDS:
            # A complete entry dict, buffered by writ_friction_buffer_append instead of
            # spawning friction-append.py (35.3ms measured) to append one line. Carried
            # as JSON rather than as fields because the entry's shape varies by event.
            try:
                entry = json.loads(parts[1])
            except ValueError:
                # Same policy as a garbled row above: skip this one, keep its neighbours.
                continue
            if isinstance(entry, dict):
                rows.append({"event": "friction_event", "entry": entry})
    return rows


CLAIM_SUFFIX = ".inflight."
# FALLBACK ONLY, no longer the liveness test. Ownership is decided by the lock below:
# "is the owning process still holding this file", which is the actual question. The age
# test was the previous answer, and it was a heuristic wearing a fact's clothes -- a drain
# that stalled past the timeout (paused process, heavy I/O) had its LIVE claim adopted and
# its rows emitted twice. Three orders of magnitude of headroom is not a closed race, it is
# a narrow one. This constant now applies only where flock is unsupported by the
# filesystem, where a heuristic is the best available answer.
ORPHAN_CLAIM_SECONDS = 60

# A session whose buffer has not been touched in this long is never going to drain itself:
# its Stop and SessionEnd already happened, or the client died without them. Nothing else
# would ever look at that file, because buffers are session-scoped and each drain reads
# only its own. Without the sweep those rows sit on disk forever, which is the quiet
# version of the loss this whole design exists to prevent.
ABANDONED_SESSION_SECONDS = 86400

# Locked fds, held for the process lifetime. The lock lives on the OPEN FILE DESCRIPTION,
# so letting one close would release ownership while the drain is still working.
_HELD: list[int] = []

_LOCK_HELD = "held"
_LOCK_TAKEN = "taken"
_LOCK_UNSUPPORTED = "unsupported"


def _lock(fd: int) -> str:
    """Try to take the file's advisory lock without blocking.

    The OS releases it when the holder dies, so a crashed drain's claim becomes adoptable
    IMMEDIATELY rather than after a timeout, and a live drain's claim never becomes
    adoptable at all. That is strictly better than the age test on both ends.
    """
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _LOCK_HELD
    except BlockingIOError:
        return _LOCK_TAKEN
    except OSError:
        # ENOLCK / ENOTSUP on an exotic filesystem. Degrade to the age heuristic rather
        # than refusing to drain.
        return _LOCK_UNSUPPORTED


def _claim_name(path: str) -> str:
    """A name no other claim can collide with.

    pid alone is NOT enough: pids are reused, and os.rename REPLACES an existing
    destination, so a new drain reusing a dead drain's pid would silently destroy that
    drain's unswept rows. The nanosecond stamp makes every attempt unique, so a rename
    can only ever create, never overwrite.
    """
    return f"{path}{CLAIM_SUFFIX}{os.getpid()}.{time.time_ns()}"


def _claim(path: str, age_fallback: float | None = None) -> str | None:
    """Take exclusive ownership of a file by locking then renaming it, or return None.

    THE RACE THIS CLOSES: the first version read the file, emitted, then unlinked. Two
    drains of one session (Stop and SessionEnd firing close together) both read before
    either unlinked, so both emitted the same rows. Reproduced: two appended rows came
    out as four emitted, breaking capabilities.md's "nothing is emitted twice".

    os.rename is atomic on a single filesystem, so exactly one caller wins the source
    and the loser sees FileNotFoundError. Used for BOTH the live buffer and orphan
    adoption, which is the fix for the second race: adoption previously selected orphans
    by name and age and read them WITHOUT claiming, so two drains racing one aged orphan
    both emitted it. Reproduced 3/3 before this change.

    LOCK BEFORE RENAME, deliberately. Locking after would leave a window in which the file
    sits claimed-but-unlocked and a concurrent adopter could take it. Rename preserves the
    inode, so a lock taken on the source is still held on the destination.

    `age_fallback` is consulted ONLY when the filesystem cannot lock: None means "this is
    the live buffer, claim it unconditionally", a number means "treat it as abandoned only
    if it is at least this old".
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None

    state = _lock(fd)
    if state == _LOCK_TAKEN:
        # Another drain holds it and is alive, however slow. Never steal it.
        os.close(fd)
        return None
    if state == _LOCK_UNSUPPORTED and age_fallback is not None:
        try:
            if time.time() - os.path.getmtime(path) <= age_fallback:
                os.close(fd)
                return None
        except OSError:
            os.close(fd)
            return None

    try:
        claim = _claim_name(path)
        os.rename(path, claim)
    except OSError:
        os.close(fd)
        return None
    _HELD.append(fd)
    # STAMP IT AS ACTIVE. os.rename PRESERVES mtime, so a freshly claimed orphan still
    # looked an hour old to the next drain, which adopted it and emitted the same rows
    # again. That defeated the claim entirely: measured, two drains racing one aged
    # orphan both emitted it even with the rename in place. Touching the file makes the
    # age test mean "nobody is working on this" rather than "this row is old".
    try:
        os.utime(claim, None)
    except OSError:
        pass
    return claim


def _adopt_orphans(path: str) -> list[str]:
    """Claim files left by a drain that died between rename and unlink.

    Without this, claiming-by-rename converts the duplication bug into a silent loss
    bug: the rows sit on disk under a name nothing reads. Each adoption goes through the
    same atomic _claim, so exactly one adopter wins.

    THE LIVENESS TEST IS THE LOCK, NOT THE CLOCK. _claim tries the lock, so a claim whose
    owner is still running is skipped no matter how long it has been running, and a claim
    whose owner died is taken at once rather than after a timeout. The age constant is
    passed only as the degraded answer for filesystems that cannot lock.
    """
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + CLAIM_SUFFIX
    adopted = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return adopted
    for name in names:
        if not name.startswith(prefix):
            continue
        # Re-claim under a FRESH unique name. The winner gets the rows; every loser
        # sees the source gone or the lock held, and moves on.
        claimed = _claim(os.path.join(directory, name), age_fallback=ORPHAN_CLAIM_SECONDS)
        if claimed:
            adopted.append(claimed)
    return adopted


def _load_emit():
    try:
        skill_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, skill_dir)
        from writ.shared.logging import emit as _emit

        return _emit
    except Exception:
        # The rows still get printed, so a broken import degrades to visible output
        # rather than to silence.
        return None


def _session_of(name: str) -> str | None:
    """Recover the session id from a buffer or claim filename.

    Needed because the sweep drains OTHER sessions' buffers, and emit() takes the session
    id as an argument rather than reading it from the row. Attributing a swept row to the
    sweeping session would silently corrupt the audit trail, which is worse than the leak
    the sweep exists to fix.
    """
    if not name.startswith(BUF_PREFIX):
        return None
    rest = name[len(BUF_PREFIX):]
    idx = rest.find(BUF_SUFFIX)
    if idx < 0:
        return None
    return rest[:idx]


def _sweep_abandoned(directory: str, skip_session: str, emit) -> int:
    """Drain buffers whose own session will never drain them.

    A session that ends without a Stop or SessionEnd (crash, disconnect, kill) leaves its
    buffer and any claim files on disk, and nothing ever reads them again: every drain
    looks only at its own session's path. Those rows are real telemetry and real gate
    decisions, so this collects them rather than deleting them.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return 0

    newest: dict[str, float] = {}
    for name in names:
        session = _session_of(name)
        if session is None or session == skip_session:
            continue
        try:
            mtime = os.path.getmtime(os.path.join(directory, name))
        except OSError:
            continue
        newest[session] = max(newest.get(session, 0.0), mtime)

    now = time.time()
    drained = 0
    for session, mtime in sorted(newest.items()):
        # The NEWEST file decides. A session with one old buffer and one fresh claim is
        # still active, and taking its rows early would race its own drain.
        if now - mtime <= ABANDONED_SESSION_SECONDS:
            continue
        drained += _drain(session, _buffer_path(session), emit)
    return drained


def _drain(session_id: str, path: str, emit) -> int:
    sources = _adopt_orphans(path)
    claimed = _claim(path)
    if claimed:
        sources.append(claimed)
    if not sources:
        return 0

    raw = ""
    for source in sources:
        try:
            # errors="replace" because byte-truncation of a multi-byte character in the
            # append path would otherwise raise UnicodeDecodeError here, before any row
            # is parsed and before the file is removed, wedging that session's buffer on
            # every future flush. One mangled character must not strand every good row.
            with open(source, encoding="utf-8", errors="replace") as handle:
                raw += handle.read()
        except OSError:
            continue

    rows = _parse(raw)

    for row in rows:
        if emit is not None:
            try:
                if row["event"] == "friction_event":
                    # Mirrors friction-append.py's _emit_entry: session and mode come
                    # from the ENTRY, not from this drain, or a row buffered by one
                    # session would be filed under whichever session drained it. `ts` is
                    # dropped because the router stamps its own.
                    entry = row["entry"]
                    emit(None, entry.get("event", ""), entry.get("session", ""),
                         entry.get("mode"),
                         **{k: v for k, v in entry.items()
                            if k not in ("session", "mode", "event", "ts")})
                elif row["event"] == "hook_execution":
                    emit(None, "hook_execution", session_id, row["mode"],
                         hook_name=row["hook_name"], duration_ms=row["duration_ms"],
                         exit_code=row["exit_code"])
                else:
                    # Routed to the audit stream by STREAM_MAP, exactly as the per-hook
                    # spawn routed it. Only the moment it lands has changed.
                    emit(None, "gate_decision", session_id, row["mode"],
                         gate=row["gate"], decision=row["decision"],
                         reason=row["reason"], target=row["target"],
                         decided_at=row["decided_at"])
            except Exception:
                pass
        print(json.dumps(row))

    # Unlink LAST, and only what this process claimed. See the module docstring: this
    # ordering is what makes a crash replay instead of drop.
    for source in sources:
        try:
            os.unlink(source)
        except OSError:
            pass
    return len(rows)


def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else ""
    path = _buffer_path(session_id)
    emit = _load_emit()

    _drain(session_id, path, emit)
    # Own session first: the sweep is housekeeping for sessions that are already gone, and
    # must never delay or displace the drain this process was actually called to do.
    _sweep_abandoned(os.path.dirname(path) or ".", session_id, emit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
