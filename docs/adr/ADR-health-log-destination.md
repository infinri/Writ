# ADR: `/health` names the file this daemon's rows actually land in

Status: accepted
Date: 2026-09-05
Plan: `.claude/plans/dfacff61-23d5-474e-846c-2e2f0f0ea482/plan.md`
Touches: `writ/shared/logging.py`, `writ/server/routes/query.py`,
`tests/test_health_log_destination.py`

## Context

MEASURED before the change. `writ/server/routes/query.py` reported
`"friction_log": str(resolve_log_path())`. `writ.analysis.friction.resolve_log_path`
resolves an explicit argument, else `WRIT_FRICTION_LOG`, else the bare cwd-relative
literal `Path("workflow-friction.log")`. `WRIT_FRICTION_LOG` is not set in the daemon's
environment (`systemctl --user show-environment` has zero matches; the only setter under
`hooks/` or `bin/` is `bin/lib/common.sh`, for dev and test use), so a live
`GET /health` answered `workflow-friction.log`.

The rows were somewhere else. `writ.shared.logging.emit` classifies by `STREAM_MAP` and
appends to `log_root() / project / f"{stream}.jsonl"`, which for this repo is
`var/logs/github.com/infinri/Writ/`. Counts taken the same day: `audit.jsonl` held 780
`write_attempt` rows, `friction.jsonl` held 0 of them, and the repo-root
`workflow-friction.log` was 23,814,070 bytes with its newest row dated 2026-07-01.

Reading the reported path, finding no `write_attempt` rows there, and concluding that
write telemetry was dead is a misdiagnosis that actually happened. That is the whole
reason this decision exists. `write_attempt` being an audit-stream event is not the bug;
it is the design (`STREAM_MAP`), and it is correct. The bug was a reporter answering a
different question from the one its name implied.

This is a one-copy-fixed divergence, the shape this program has closed several times.
The fix already existed in this repo for a different caller:
`writ/session/doctor.py::_observed_hook_names` hit the same confusion and rejected the
resolver BY NAME, noting that it "answers with whatever WRIT_FRICTION_LOG names (on this
machine a bare `workflow-friction.log` at the repo root), so its parent is not the
per-project stream directory and the metrics file was never found". `/health` never got
that fix.

INFERRED, and labelled as such: everything about how an operator uses `/health`. The
claim that an operator reads it first and is therefore pointed at a July file is
inference from one instance (the misdiagnosis above) plus the fact that the field was the
only log path the endpoint published. No usage telemetry exists for the endpoint.

## Decision

The destination rule is extracted ONCE, as `writ.shared.logging.emit_destination(stream,
project=None)`, and BOTH the writer and the reporter go through it.

    emit_destination(stream, project=None) -> Path
        WRIT_FRICTION_LOG set    -> Path(that value)          # every stream collapses
        otherwise                -> stream_path(project, stream)

`emit` reads the collapse and then calls `emit_destination` in both of its branches,
passing its own already-resolved `project` on the per-project path so the git identity is
derived once per event and not twice. `/health` reports two fields, `audit_log` and
`friction_log`, each produced by that same function through a single module-level helper
`_log_destinations()` that is spread into BOTH return branches.

The collapse branch lives INSIDE `emit_destination`, not in the caller. That is what makes
the reported value mean the same thing in both conditions: "where a row of this stream
lands". With `WRIT_FRICTION_LOG` set both fields equal that one file, which is the truth
rather than a coincidence to paper over, because every stream really does collapse into
it. With it unset they are the two real per-project stream files.

Copying `stream_path(resolve_project(), ...)` into `query.py` was rejected for the same
reason: it would be a THIRD copy of the destination rule, and it would be wrong in the
collapse condition.

The `not_ready` return carries both fields too. The precedent is the comment already on
that branch for `tcp_readonly`: a doctor asking a not-ready daemon still needs a true
answer. Where a daemon's rows land does not depend on its graph being up, and "where are
the gate decisions" is exactly the question asked of a daemon that is not answering.

## Alternatives considered

1. **Report the per-project stream DIRECTORY, one field.** Rejected. The operator's
   question is "where are the gate decisions", and the answer is one file in a directory
   of four; handing back a directory makes the reader guess between `audit.jsonl` and
   `friction.jsonl`, which is precisely the guess that went wrong. It also has no honest
   value under the collapse, where there is no per-project directory at all.

2. **Rename the single field so it cannot be read as "where write decisions are logged"**
   (for example `friction_log_collapse_target`). Rejected. Three live consumers read the
   key `friction_log` by that exact name and compare it against their own
   `WRIT_FRICTION_LOG` to detect a daemon whose telemetry is pinned elsewhere:
   `tests/_daemon.py::ensure_daemon_aligned`, `scripts/lib/writ-server-lib.sh`, and
   `tests/test_fix2_cache_alignment.py`, which asserts the shell lib reads that key by
   name. A rename buys clarity in one place and spends it on drift risk in three.

3. **Keep one field and simply make it honest.** Rejected. The honest single answer for a
   gate decision is `audit.jsonl`, and calling that "the friction log" would be a second
   wrong name for the same reason as the first.

The chosen shape keeps `friction_log` under its exact name and, with `WRIT_FRICTION_LOG`
set, its exact value, so all three alignment readers are untouched, and adds `audit_log`
so the operator can reach the file that actually holds gate decisions.

## Consequences

What flips. With `WRIT_FRICTION_LOG` unset, `friction_log` goes from the bare
cwd-relative `workflow-friction.log` to the absolute per-project `friction.jsonl`, and
`audit_log` appears, naming the file every gate decision really lands in. The `not_ready`
payload gains both fields, which also stops the alignment readers seeing a not-ready
daemon as PERMANENTLY diverged: they compared `health.get("friction_log")`, the missing
key read as `None`, `None` never equals a set variable, so such a daemon was restarted on
every probe. The shell lib's fail-safe (empty treated as aligned) means it moves from
"never restart a not-ready daemon" to "restart it only on a genuine mismatch", which is
what that code is for.

What deliberately does not flip. `resolve_log_path` and its resolution order are
untouched: it is the correct answer for its real callers, which pass an explicit path or
read the env var deliberately, namely the CLI's `--log` default (`writ/cli.py`) and the
dashboard (`writ/dashboard.py`). `writ/server/__init__.py` keeps its own import of it.
`STREAM_MAP` is untouched; `write_attempt` belongs in the audit stream. The READY
branch's `friction_log` is byte-identical to the old value in the only condition the test
suite runs in, because the suite always sets `WRIT_FRICTION_LOG` and with it set
`emit_destination` returns exactly that path.

The test gap is closed in the same cycle, because it is the other half of the defect.
`tests/conftest.py::_isolate_friction_log` is autouse and sets `WRIT_FRICTION_LOG` for
every test, and `emit` takes an early-return collapse branch when that variable is set,
so the classification split was never exercised: those tests would have passed
identically if `write_attempt` were classified to any stream at all.
`tests/test_logging_router.py` does disable the collapse but writes under
`WRIT_LOG_PROJECT="proj-a"`, a name it invents, so it could not catch a wrong destination
for a repo whose scope comes from git. And `tests/_daemon.py` restarts the daemon whenever
`/health` disagrees with the variable it always sets, forcing agreement a second way.
`tests/test_health_log_destination.py` uses the repo's existing `no_friction_isolation`
marker to delete the variable (no new mechanism), asserts on the RENDERED file rather
than on the helper's return value, and gives every classification assertion an ABSENCE
half so reclassifying an event reddens the module instead of moving a row a
presence-only assertion would still accept.

Declared unprovable by test (ENF-SYS-005). Those tests drive the ASGI app with
`writ.server._db` patched to `None`, so they prove what the ROUTE returns and cannot
prove what the systemd daemon reports. That behavior is verified operationally instead,
by restarting the service and reading the live field, which is why a live check is part
of this cycle rather than optional.

## Out of scope, named so it is not rediscovered as new

A THIRD reader of the destination rule exists and is deliberately not changed:
`hooks/scripts/friction-logger.sh` computes `stream_path(resolve_project(), 'audit')` and
`...'metrics'` for its dedup reads while its appends go through
`bin/lib/friction-append.py`, which routes via `emit`. Under the collapse the appends land
in the single file and the dedup reads look at the per-project stream, so the dedup finds
nothing and can re-append. That is a real divergence of the same family, left alone for
two reasons: in production (variable unset) `emit_destination` returns exactly what that
hook already computes, so nothing there is wrong today; and pointing it at the collapsed
file would widen its dedup read population from one stream to every stream, which needs
its own measurement. INFERRED from reading the hook, not measured.

`writ/server/routes/query.py` carries a comment justifying client-side friction logging on
the grounds that the resolver "is cwd-relative when WRIT_FRICTION_LOG is unset". It is
left alone: it is about which PROCESS logs, not about what `/health` reports, and the
claim it makes about the resolver is still true of the resolver.

The 811 `subagent_type_fallback` rows in `friction.jsonl` are untouched; that condition is
documented in `writ/session/subagent_role.py`'s own docstring and is a separate question.
