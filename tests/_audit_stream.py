"""Single owner of the PRODUCTION audit-stream path derivation, row reader,
attribution predicate and counting helper, so a later triage cycle over this
hazard has one place to call instead of a per-module re-derivation.

WHY THIS EXISTS. tests/test_advance_phase_token_gate.py was measured writing
one `agent_self_approval_blocked` row per run into the operator's REAL
`<skill>/var/logs/<project>/audit.jsonl` (1518 rows before a run, 1519
after). That row is security-shaped: an auditor reading the trail sees what
looks like an agent trying to approve its own gate. This module is the guard
that measures the hazard and the guard that later verifies it stays fixed.

NO ROW COUNT IS QUOTED HERE ON PURPOSE. An earlier draft said the live file
held eleven such rows as of 2026-09-09, and that number was stale within the
hour: proving the defect required RUNNING it, and the mutation proof appended
four more. A count in a docstring is a claim that decays on its own, so the
count belongs to `count_attributable()` at the moment it is called, and the
tests assert a DELTA around a run rather than any absolute figure.

THE PATH IS DELIBERATELY NOT `writ.shared.logging.emit_destination`, and
does NOT read `WRIT_LOG_ROOT` or `WRIT_FRICTION_LOG`. Both are exactly the
variables the test suite's own isolation sets (tests/conftest.py's autouse
`_isolate_friction_log` sets `WRIT_LOG_ROOT` for every test); a guard that
reads the same env var the isolation fixture sets can be redirected into
passing by the very isolation it exists to verify -- see
tests/test_production_audit_stream_isolation.py::TestProductionAuditPathIsEnvironmentIndependent.
Mirrors the derivation tests/test_logging_router.py already pins for
`log_root()`'s default (`Path(writ.shared.logging.__file__).resolve().parents[2]`),
plus `resolve_project` scoped explicitly to the repo root rather than the
process cwd, which pytest may run from anywhere.

Every function here is READ-ONLY against the production file by
construction: nothing opens it for writing, and nothing creates a missing
parent directory. A counting helper for a file this hazard exists to
protect must never itself become a second writer.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# THIS repo's root, so the `resolve_project` call below is scoped to this
# checkout explicitly rather than to `os.getcwd()`, which pytest may be
# invoked from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent

# The session-id prefix every test in tests/test_advance_phase_token_gate.py
# mints (f"selfapproval-{uuid.uuid4().hex[:8]}", f"selfapproval-ok-{...}"),
# and the event writ/server/routes/gate.py emits on a refused advance. Either
# one alone attributes a row to this test suite: an OR, not an AND, so a row
# carrying the event under an unrelated session (an interactive developer
# hitting the same route by hand) is still the same class of false security
# signal this cycle exists to stop producing, and a row carrying the prefix
# under a different event (were one ever added) is still ours to claim.
ATTRIBUTION_SESSION_PREFIX = "selfapproval"
ATTRIBUTION_EVENT = "agent_self_approval_blocked"


def production_audit_path() -> Path:
    """The real `<skill>/var/logs/<project>/audit.jsonl`, environment independent.

    `<skill>` is derived from `writ.shared.logging`'s own `__file__`
    (`parents[2]`: [0]=writ/shared, [1]=writ, [2]=the skill install dir),
    never from `WRIT_LOG_ROOT`. `<project>` is `resolve_project` scoped to
    THIS repo's root explicitly (not `os.getcwd()`), so the answer does not
    depend on where pytest happens to be invoked from.
    """
    # Imported inside the function so this module's top level stays stdlib-only.
    import writ.shared.logging as writ_logging

    skill_root = Path(writ_logging.__file__).resolve().parents[2]
    project = writ_logging.resolve_project(str(_REPO_ROOT))
    return skill_root / "var" / "logs" / project / "audit.jsonl"


def read_rows(path: Path) -> list[dict[str, Any]]:
    """Every JSON row in `path`, in file order. `[]` when `path` is absent.

    Read-only: never opens `path` for writing and never creates a missing
    parent directory. A malformed line is skipped rather than raising, so one
    corrupt row (e.g. a torn write from a concurrent writer) cannot hide every
    row after it.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def count_rows(path: Path) -> int:
    """`len(read_rows(path))`, named separately so a caller measuring the RAW
    count (for a failure message) does not have to hold the parsed rows."""
    return len(read_rows(path))


def is_attributable(row: dict[str, Any]) -> bool:
    """True when `row` is one this test suite caused.

    Keyed on `session` (the field these rows actually carry -- verified
    against the live audit stream, e.g. line 343 of the current generation:
    `{"session": "selfapproval-d1786c3f", ...}`) OR `event`. NOT `session_id`,
    a field name these rows do not carry: keying on it would make the
    predicate match nothing, and a predicate that matches nothing lets the
    module-scoped zero-delta guard in test_advance_phase_token_gate.py pass
    on every run whether or not the leak this cycle fixes is actually fixed.
    """
    session = str(row.get("session") or "")
    return (
        session.startswith(f"{ATTRIBUTION_SESSION_PREFIX}-")
        or row.get("event") == ATTRIBUTION_EVENT
    )


def count_attributable(rows: list[dict[str, Any]]) -> int:
    """How many of `rows` are attributable to this test suite. Pure; no I/O."""
    return sum(1 for row in rows if is_attributable(row))


def attributable_rows(path: Path) -> list[dict[str, Any]]:
    """The attributable rows in `path`, so a failure message can name them
    (the artifact and the exact value, not a bare boolean)."""
    return [row for row in read_rows(path) if is_attributable(row)]
