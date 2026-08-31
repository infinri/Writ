"""Run a command under strace, or say loudly that the measurement could not run.

WHY THIS MODULE EXISTS
----------------------
Nine call sites across seven test files each ended their strace invocation with
the same three lines:

    if not trace.exists():
        pytest.skip(...)

Two defects live in that shape, and the second is worse than the first.

The first is the skip itself. Every one of those nine sites holds a ratchet:
the write-path process budget, the prompt-path python budget, the "a non
approval prompt never reads the transcript" guard. A ratchet that skips under
load is a ratchet that stops ratcheting exactly when the machine is busy
enough to regress, and a skip in a 8,780-test run is invisible.

The second is vacuity. `Path.exists()` is true for a zero byte file, so an
EMPTY trace was already treated as a successful measurement. At
`tests/test_approval_evidence.py` the assertion on that text is
`assert "approval_evidence" not in text`, which is trivially true of an empty
string. That is not a skip and not a failure: it is a green result produced by
a measurement that never happened. A skip is at least visible in the summary.

So there are THREE unmeasurable states, not one, and all three raise here:

  1. the trace file was never created,
  2. the trace file exists and is empty,
  3. the trace file exists, is non empty, and holds no `execve(` line.

WHAT STAYS A SKIP
-----------------
`shutil.which("strace") is None`. A machine without the tool is a fact about
the environment, not a measurement that failed, so this module skips for that
one reason and raises for every other. The nine converted sites keep their own
`shutil.which` guards; this one is the backstop for a caller that forgets.

WHY `UnmeasurableTrace` IS A RuntimeError
-----------------------------------------
pytest's `Skipped` is an `OutcomeException`, i.e. a BaseException, and it is
caught and turned into a skip report by machinery a caller cannot see. Making
the unmeasurable case a plain RuntimeError subclass means no part of pytest's
skip path is reachable from here even by accident, and a caller that wraps the
call in `except Exception` sees a failure rather than a silent skip.

WHY TWO ATTEMPTS
----------------
Measured: roughly one "no trace" occurrence per run across about 66 measured
sites, call it 1.5 percent per site. A second INDEPENDENT trial (fresh
subprocess, fresh trace path) takes a transient cause to about 0.02 percent,
roughly one false failure per 4,000 runs. A third attempt would buy 0.0003
percent at triple the worst case cost of a path that runs a real hook under
strace. And if the cause is deterministic (ptrace denied, a sandbox
restriction) then no retry count helps at all, and failing on the second
attempt with the exit status attached is exactly the wanted outcome.

WHY A FRESH PATH PER CALL AND PER ATTEMPT
------------------------------------------
Two of the converted sites wrote to a fixed `/tmp/writ-budget-<hook>.trace`.
A leftover file from a crashed earlier run is then read as THIS run's
measurement, which is a silent wrong answer rather than a skip. Every call
gets its own temp directory and every attempt its own file inside it, so a
stale file can never be mistaken for a fresh one and the retry cannot re-read
the attempt that just failed.

TWO DEVIATIONS FROM THE ONE-FUNCTION PLAN, AND WHY THE CONVERSION IS STILL SAFE
-------------------------------------------------------------------------------
`trace_execve_result` exists because two of the nine sites
(`tests/test_blackbox_record_schema.py`, `tests/test_hook_out_capture.py`)
assert on the TRACED PROGRAM's own returncode and stdout as well as on the
trace, and a text-only return discards them. One of those probes deliberately
exits 2, which is why a non-zero exit is never a failure condition here: the
only question this module answers is whether a measurement exists.

`str_limit` exists because `tests/test_approval_evidence.py` passed `-s 500`
and asserts on a SUBSTRING of an argv element. strace's default 32 character
cut can truncate that substring out of the trace, which is the same vacuity in
the other direction: a negative assertion would then pass because the evidence
was clipped, not because the evidence was absent. THE CLAIM A FUTURE READER
WANTS: only the approval-evidence sites pass `str_limit`. Every other converted
site runs on strace's default flags, exactly as it did before the conversion,
so no site whose number a ratchet is pinned to could have moved. Verified by
running the write-path and prompt-path budget files after the conversion, with
`PYTHON_BUDGET`, `TOTAL_BUDGET` and `REAL_PROCESS_BUDGET` byte-identical.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Sequence

import pytest

# How much of each string strace prints, when a caller asks for a limit. Only
# the approval-evidence sites need one: they assert on a SUBSTRING of an argv
# element, and strace's default 32 character cut can truncate the substring out
# of the trace, which is the same vacuity in the other direction. Sites that
# count lines are left on the default so the conversion cannot move a number a
# ratchet is pinned to.
DEFAULT_STR_LIMIT: int | None = None

# How much of strace's own stderr the failure message carries. Long enough for
# "ptrace: Operation not permitted" plus context, short enough not to bury the
# rest of the message.
STDERR_TAIL_CHARS = 600

_EXECVE_MARKER = "execve("

# The skip shape this module exists to remove. Matched against the REASON text
# of a real `pytest.skip(...)` call, never against raw source, so a docstring
# quoting the old shape (this file, and tests/test_strace_measurability.py) is
# not a finding.
_UNMEASURABLE_REASON = re.compile(r"produced\s+no\s+trace|no\s+trace\s+produced", re.IGNORECASE)


class UnmeasurableTrace(RuntimeError):
    """strace ran and produced nothing usable, so there is no measurement.

    Deliberately not a pytest skip. See the module docstring.
    """


def _describe(trace: Path) -> str | None:
    """Name why this attempt is unusable, or None when it is usable.

    Returns the operator-facing reason string rather than a boolean, because
    "missing", "empty" and "no execve line" are three different defects and the
    failure message has to say which one happened.
    """
    if not trace.exists():
        return "the trace file was never created"
    raw = trace.read_text(errors="replace")
    if not raw.strip():
        return f"the trace file exists but is empty ({trace.stat().st_size} bytes)"
    if _EXECVE_MARKER not in raw:
        head = raw.splitlines()[:2]
        return (
            f"the trace file holds {len(raw.splitlines())} lines but no "
            f"{_EXECVE_MARKER} line; first lines: {head!r}"
        )
    return None


def _attempt_note(n: int, result: Any, reason: str) -> str:
    status = "not launched" if result is None else getattr(result, "returncode", "unknown")
    stderr = "" if result is None else (getattr(result, "stderr", "") or "")
    if not isinstance(stderr, str):
        stderr = stderr.decode("utf-8", errors="replace")
    tail = stderr[-STDERR_TAIL_CHARS:]
    return f"  attempt {n}: exit status {status}, {reason}, stderr tail: {tail!r}"


def trace_execve_result(
    argv: Sequence[str],
    *,
    input: str | None = None,
    cwd: str | None = None,
    env: dict | None = None,
    timeout: int = 180,
    attempts: int = 2,
    str_limit: int | None = DEFAULT_STR_LIMIT,
) -> tuple[Any, str]:
    """Trace `argv`'s execve calls; return (CompletedProcess, trace text).

    The full form, for the two call sites that assert on the traced program's
    own exit status and stdout as well as on the trace. `trace_execve` below is
    the same thing for the seven that only need the text, and it is the
    documented entry point.

    Raises `UnmeasurableTrace` after the final attempt when no attempt produced
    a trace holding at least one `execve(` line. Skips, once, when there is no
    strace binary at all.

    The traced program's own exit status is NEVER a failure condition here: one
    converted site deliberately traces a probe that exits 2. The only question
    this function answers is whether a measurement exists.
    """
    if shutil.which("strace") is None:
        pytest.skip("strace unavailable; cannot trace execve")

    workdir = Path(tempfile.mkdtemp(prefix="writ-strace-"))
    notes: list[str] = []
    try:
        for n in range(1, attempts + 1):
            trace = workdir / f"attempt-{n}.trace"
            strace_argv = ["strace", "-f", "-qq", "-e", "trace=execve"]
            if str_limit is not None:
                strace_argv += ["-s", str(str_limit)]
            strace_argv += ["-o", str(trace), *argv]
            result: Any = None
            try:
                result = subprocess.run(
                    strace_argv,
                    input=input,
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                notes.append(_attempt_note(n, None, f"strace timed out after {timeout}s ({exc})"))
                continue
            except OSError as exc:
                notes.append(_attempt_note(n, None, f"strace could not be launched ({exc})"))
                continue
            reason = _describe(trace)
            if reason is None:
                return result, trace.read_text(errors="replace")
            notes.append(_attempt_note(n, result, reason))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    raise UnmeasurableTrace(
        f"strace ran but produced no usable trace for {list(argv)!r} after "
        f"{attempts} attempts.\n"
        + "\n".join(notes)
        + "\nThis FAILS rather than skipping on purpose: a missing or empty trace "
        "makes a negative assertion such as `assert \"x\" not in text` pass "
        "vacuously, which is a green result from a measurement that never ran."
    )


def trace_execve(argv: Sequence[str], **kwargs: Any) -> str:
    """Trace `argv`'s execve calls and return the trace text.

    Documented signature, one home for the defaults in `trace_execve_result`:

        trace_execve(argv, *, input=None, cwd=None, env=None, timeout=180,
                     attempts=2, str_limit=None) -> str
    """
    return trace_execve_result(argv, **kwargs)[1]


def _static_text(node: ast.AST) -> str:
    """Best effort literal text of a skip reason expression.

    Handles the two shapes the nine sites used, a plain string and an f-string,
    plus implicit and explicit concatenation. Anything dynamic contributes its
    literal parts only, which is the right direction for a ratchet: a reason
    assembled at runtime is not something a source scan can claim to have read.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else ""
    if isinstance(node, ast.JoinedStr):
        return " ".join(_static_text(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        return ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_text(node.left) + _static_text(node.right)
    return ""


def _skip_reason(node: ast.Call) -> str | None:
    """The reason text of a `pytest.skip(...)` / `skip(...)` call, else None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return None
    if name != "skip":
        return None
    parts = [_static_text(a) for a in node.args[:1]]
    parts += [_static_text(kw.value) for kw in node.keywords if kw.arg == "reason"]
    return " ".join(p for p in parts if p)


def unmeasurable_skip_sites(*, tests_dir: Path) -> list[str]:
    """Every remaining `pytest.skip` whose reason is the "produced no trace" shape.

    Pinned EMPTY by tests/test_strace_measurability.py, which is what stops the
    pattern from being reintroduced by the next person who sees a flake and
    reaches for the three lines that made it quiet.

    AST based, not a grep, and that is load bearing rather than fastidious:
    this file's own docstring and the pinning test's docstring both quote the
    old shape verbatim, and a text scan would report them as live sites and
    then be "fixed" by deleting the explanation. Only a real call node counts.
    """
    hits: list[str] = []
    for path in sorted(Path(tests_dir).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            reason = _skip_reason(node)
            if reason and _UNMEASURABLE_REASON.search(reason):
                hits.append(f"{path}:{node.lineno}")
    return hits
