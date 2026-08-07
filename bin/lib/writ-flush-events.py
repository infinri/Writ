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

import json
import os
import sys
import time

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"
FIELDS = 6       # hook_execution: kind, hook, duration_ms, exit_code, mode, truncated
GATE_FIELDS = 7  # gate_decision: kind, gate, decision, reason, target, mode, decided_at


def _buffer_path(session_id: str) -> str:
    cache_dir = os.environ.get("WRIT_CACHE_DIR")
    if not cache_dir:
        skill_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        cache_dir = os.path.join(skill_dir, "var", "session")
    return os.path.join(cache_dir, f"writ-events-{session_id or 'unknown'}.buf")


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
    return rows


CLAIM_SUFFIX = ".inflight."
# A drain that has not finished in this long is dead, not slow. A live drain takes
# milliseconds; 60s is three orders of magnitude of headroom.
ORPHAN_CLAIM_SECONDS = 60


def _claim_name(path: str) -> str:
    """A name no other claim can collide with.

    pid alone is NOT enough: pids are reused, and os.rename REPLACES an existing
    destination, so a new drain reusing a dead drain's pid would silently destroy that
    drain's unswept rows. The nanosecond stamp makes every attempt unique, so a rename
    can only ever create, never overwrite.
    """
    return f"{path}{CLAIM_SUFFIX}{os.getpid()}.{time.time_ns()}"


def _claim(path: str) -> str | None:
    """Take exclusive ownership of a file by renaming it, or return None.

    THE RACE THIS CLOSES: the first version read the file, emitted, then unlinked. Two
    drains of one session (Stop and SessionEnd firing close together) both read before
    either unlinked, so both emitted the same rows. Reproduced: two appended rows came
    out as four emitted, breaking capabilities.md's "nothing is emitted twice".

    os.rename is atomic on a single filesystem, so exactly one caller wins the source
    and the loser sees FileNotFoundError. Used for BOTH the live buffer and orphan
    adoption, which is the fix for the second race: adoption previously selected orphans
    by name and age and read them WITHOUT claiming, so two drains racing one aged orphan
    both emitted it. Reproduced 3/3 before this change.
    """
    try:
        claim = _claim_name(path)
        os.rename(path, claim)
    except OSError:
        return None
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
    bug: the rows sit on disk under a name nothing reads. Only claims older than
    ORPHAN_CLAIM_SECONDS are adopted, so a live drain's claim is never stolen, and each
    adoption goes through the same atomic _claim, so exactly one adopter wins.
    """
    directory = os.path.dirname(path) or "."
    prefix = os.path.basename(path) + CLAIM_SUFFIX
    now = time.time()
    adopted = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return adopted
    for name in names:
        if not name.startswith(prefix):
            continue
        candidate = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(candidate) <= ORPHAN_CLAIM_SECONDS:
                continue
        except OSError:
            continue
        # Re-claim under a FRESH unique name. The winner gets the rows; every loser
        # sees the source gone and moves on.
        claimed = _claim(candidate)
        if claimed:
            adopted.append(claimed)
    return adopted


def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else ""
    path = _buffer_path(session_id)

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

    emit = None
    try:
        skill_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sys.path.insert(0, skill_dir)
        from writ.shared.logging import emit as _emit

        emit = _emit
    except Exception:
        # The rows still get printed, so a broken import degrades to visible output
        # rather than to silence.
        emit = None

    for row in rows:
        if emit is not None:
            try:
                if row["event"] == "hook_execution":
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
