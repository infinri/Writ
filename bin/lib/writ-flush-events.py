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

RECORD_SEP = "\x1e"
FIELD_SEP = "\x1f"
FIELDS = 6  # kind, hook, duration_ms, exit_code, mode, truncated


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
        if len(parts) != FIELDS or parts[0] != "hook_execution":
            continue
        _, hook, duration, exit_code, mode, truncated = parts
        try:
            duration_ms = int(duration or 0)
            rc = int(exit_code or 0)
        except ValueError:
            continue
        rows.append({
            "hook_name": hook or "unknown",
            "duration_ms": duration_ms,
            "exit_code": rc,
            "mode": mode or None,
            "truncated": truncated == "1",
        })
    return rows


def main() -> int:
    session_id = sys.argv[1] if len(sys.argv) > 1 else ""
    path = _buffer_path(session_id)
    try:
        with open(path) as handle:
            raw = handle.read()
    except OSError:
        return 0

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
                emit(None, "hook_execution", session_id, row["mode"],
                     hook_name=row["hook_name"], duration_ms=row["duration_ms"],
                     exit_code=row["exit_code"])
            except Exception:
                pass
        print(json.dumps(row))

    # Unlink LAST. See the module docstring: this ordering is what makes a crash
    # replay instead of drop.
    try:
        os.unlink(path)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
