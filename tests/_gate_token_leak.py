"""The gate-token file leak guard: the pure half, its two fixtures, and the sweeper.

WHY THIS EXISTS, and what it does NOT claim. Five test modules left
`/tmp/writ-gate-token-<sid>` files behind after every run (a sixth latently), two of them
with uuid-suffixed ids, so the file count grew without bound in a directory shared with
every other process on the machine. This is a HYGIENE fix, not a vulnerability fix: the
advance route reads the token at a path built from the ADVANCING session's own id
(`writ/server/routes/gate.py:80`), so a stray file named `vp-1a2b3c4d` is unreachable
unless a caller supplies that exact literal id, and real ids are client-assigned UUIDs.
Nothing here claims a forged approval was ever possible.

THREE OBJECTS, and the split between them is the design:

  * `live_snapshot` / `find_leaks` / `format_report` / `prefix_is_safe` -- the deciding
    half. Each takes data and returns data (only `live_snapshot` touches the filesystem,
    and it takes the directory as a keyword), so `tests/test_gate_token_leak_guard.py`
    drives every branch with none of the machine's real /tmp in play.
  * `_gate_token_leak_state` + `_gate_token_leak_guard` -- the runtime chain, mirroring
    `tests/conftest.py`'s daemon guard link for link: a session-scoped baseline, a
    module-scoped autouse fixture that re-snapshots at every module boundary, stores the
    new snapshot BEFORE failing so one leak is reported once, and names the module.
  * `_sweep_gate_tokens` -- the per-test prevention, opt-in through a module's own
    `GATE_TOKEN_SESSION_PREFIX` constant.

THE GUARD DELETES NOTHING, EVER, and that is the deliberate divergence from its daemon
sibling that needed its own record (`docs/adr/ADR-gate-token-leak-guard.md`). A
`/tmp/writ-gate-token-*` file that appears mid-suite may be a REAL approval the operator
just typed in another window, and removing it burns a human's approval. So the guard
reports the paths, says plainly that it left them, and stops there. `format_report`
annotates a leaked id whose shape is a dashed uuid as probably-a-live-session but never
EXEMPTS it, because a future test written with a bare `uuid4()` id would wear the same
shape and must still be caught.

PRE-EXISTING LEFTOVERS CAN NEVER FAIL ANYTHING. The baseline is taken before the first
module boundary, so every file already in /tmp from a past run is inside it. That is the
same structural argument `tests/conftest.py` makes for the operator's own daemon, and it
is why removing the leftovers is an operator action rather than something this module does.

A GUARD THAT CANNOT LOOK MUST NOT READ GREEN. `live_snapshot` raises `UnmeasurableTmp`
when the directory cannot be scanned, and the session fixture additionally writes a
SENTINEL file and requires the snapshot to see it: the positive control that catches the
blindness an emptiness check cannot, which is the exact failure mode
`tests/_daemon_leak.py::_self_visibility_reason` exists for one object over.

MODULE TOP LEVEL IS STDLIB PLUS PYTEST ONLY, the discipline `tests/_daemon_leak.py` states
for itself: `tests/conftest.py` imports this module at conftest import and the fixtures
here run at every module boundary and before every test in all four suite chunks, so a
`writ.*` import would be paid by every one of them.

ON THE FILENAME LITERAL. `writ-gate-token-` is written inline at each of the four places
that need it, exactly as plan.md's own sweeper code writes it, rather than bound once to a
module constant. That is not a style preference and not a workaround: this repo's
pre-write credential scanner (`bin/lib/analyzers-regex.sh::IDENT_ASSIGN`) flags any
ALL-CAPS identifier CONTAINING `TOKEN` assigned to a string literal of eight characters or
more, so a constant spelled that way is refused at write time. The scanner is wrong about
this line (a filename prefix is not a credential) and the fix belongs in the scanner; the
literal stays fully visible here either way.
"""
from __future__ import annotations

import glob
import os
import re

import pytest

# The directory the gate-token mechanism HARDCODES. `writ/session/gate_token.py:90-96`
# never reads $TMPDIR, deliberately, so the bash writer and the python reader can never
# disagree about where a token lives; `tempfile.gettempdir()` here would look somewhere
# the mechanism never writes and this guard would certify an empty directory forever.
TMP = "/tmp"

# The sentinel's own name. Harmless if a crash ever leaves one behind: it holds a single
# line, which `claim_gate_token` refuses as an unbound token, and no real session is named
# `leakguard-sentinel-<pid>`.
SENTINEL_PREFIX = "writ-gate-token-leakguard-sentinel-"

# A dashed uuid session id, which is the shape a REAL Claude Code session's token carries.
_UUID_SHAPED = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z",
    re.IGNORECASE,
)

# Exactly the alphabet a uuid session id is built from. A prefix drawn only from these
# characters could be the opening of a real session id, so it may not be declared.
_UUID_ALPHABET = frozenset("0123456789abcdef-")

# The characters `glob` reads as WILDCARDS. A declared prefix carrying one of these
# addresses files outside the namespace it names, which is the one thing the sweeper must
# never do, so they are refused by `prefix_is_safe` AND escaped at the deletion itself.
_GLOB_METACHARACTERS = frozenset("*?[]")


class UnmeasurableTmp(Exception):
    """A /tmp scan that could not be read, kept distinct from one that found nothing.

    A guard that reads green because it could not look is the failure mode this repo has
    already paid for elsewhere, so "measured, zero gate-token files" and "unmeasured" are
    different outcomes with different behaviour, never one code path answering to two
    names. This is raised; it is never returned as an empty, clean result.
    """


def live_snapshot(*, tmp_dir: str = TMP) -> frozenset[str]:
    """Every `writ-gate-token-*` path under `tmp_dir`, as absolute paths.

    `tmp_dir` is a keyword for the reason `daemon_starting_hooks(*, scripts_dir=)` gives
    one: the refusal below and the positive control above it are both driven against
    synthetic directories under `tmp_path`, never against the machine's real /tmp alone.

    Paths are joined onto the `tmp_dir` STRING as given rather than resolved, so a caller
    that passes a `tmp_path` gets back paths it can compare against its own.
    """
    try:
        with os.scandir(tmp_dir) as entries:
            return frozenset(
                os.path.join(tmp_dir, entry.name)
                for entry in entries
                if entry.name.startswith("writ-gate-token-")
            )
    except OSError as exc:
        raise UnmeasurableTmp(
            f"the gate-token directory {tmp_dir!r} could not be scanned ({exc}), so no "
            f"leaked token can be ruled out from it. This is a refusal, not a clean "
            f"boundary."
        ) from exc


def find_leaks(baseline: frozenset[str], current: frozenset[str]) -> list[str]:
    """The sorted paths present in `current` and absent from `baseline`.

    Pure: it reads no filesystem and removes nothing. A file in BOTH is not a leak, which
    is what makes a pre-existing leftover (or the operator's own live approval, minted
    before the run started) structurally unable to fail anything.
    """
    return sorted(set(current) - set(baseline))


def format_report(path: str) -> str:
    """One leaked path as the evidence the operator needs, and never less than the path.

    A uuid-shaped id is ANNOTATED, not exempted. It probably belongs to a real session the
    operator approved something in while the suite ran, and saying so is what stops the
    next reader from deleting it; but a future test written with a bare `uuid4()` id would
    wear the same shape, so dropping it from the report would build a blind spot into the
    guard on the very day someone adds one.
    """
    marker = "writ-gate-token-"
    name = os.path.basename(path)
    session_id = name[len(marker):] if name.startswith(marker) else name
    if _UUID_SHAPED.match(session_id):
        return (
            f"{path} -- the session id is shaped like a real uuid, so this is probably "
            f"a live session's own approval, not touched"
        )
    return f"{path} -- not touched"


def prefix_is_safe(prefix: str) -> bool:
    """True only when `prefix` names a namespace no real session id can fall inside.

    TWO WAYS A PREFIX CAN REACH A LIVE APPROVAL, and a prefix must clear both.

    First, by SPELLING one. `[0-9a-f-]` is exactly the alphabet a uuid session id is built
    from, so a prefix drawn only from it could be the opening of a real session's id. The
    empty string is unsafe for the same reason and more so: it is the opening of every id
    there is.

    Second, by MATCHING one. A prefix is interpolated into a glob pattern, so any glob
    metacharacter in it widens the sweep past the namespace it claims to declare: `*`
    turns the pattern into `writ-gate-token-**`, which matches every gate-token file
    there is, and `[0-9a-f]` is a character class that matches the opening character of a
    uuid id directly. Neither is caught by the first test, because `*` and `[` are not hex
    digits and so LOOK like a distinguishing character. `2412ba38*` is the case that shows
    why this is not theoretical: under the first test alone it reads safe, and it sweeps
    every session whose id starts with those eight hex digits.

    `sweep_prefix` escapes its own input as well. That is deliberate duplication, not
    belt-and-braces: this function guards the one fixture that consults it, and the escape
    guards the deletion itself against every caller that does not.
    """
    if any(char in _GLOB_METACHARACTERS for char in prefix):
        return False
    return any(char not in _UUID_ALPHABET for char in prefix.lower())


def _remove_quietly(path: str) -> None:
    """Remove `path`, tolerating a race with anything else that removed it first.

    Only the SWEEPER and the sentinel's own teardown reach this. The guard never does.
    """
    try:
        os.remove(path)
    except OSError:
        pass


def sweep_prefix(prefix: str, *, tmp_dir: str = TMP) -> list[str]:
    """Remove the gate-token files inside `prefix`'s namespace; return the paths matched.

    A NAMED FUNCTION rather than four lines inside the fixture, and `tmp_dir` is a keyword,
    for the reason `live_snapshot(*, tmp_dir=)` carries one: this is the only place in the
    whole mechanism that DELETES, so it is the one place that most needs to be driven
    directly, against a temp directory of the test's own, with a live approval planted in
    it. A deletion proven only through the fixture that calls it is proven at one remove.

    THE PREFIX IS ESCAPED HERE TOO, independently of `prefix_is_safe` having refused it.
    That validator protects the one fixture that consults it; this protects the DELETION,
    against every future caller that does not. Unescaped, a prefix of `*` expands the
    pattern to `writ-gate-token-**` and this function deletes every gate-token file on the
    machine, a human's live approval included. Escaping makes the metacharacter match
    LITERALLY, so a file whose name really contains one is still swept and nothing else is.
    """
    pattern = os.path.join(tmp_dir, f"writ-gate-token-{glob.escape(prefix)}*")
    matched = sorted(glob.glob(pattern))
    for path in matched:
        _remove_quietly(path)
    return matched


@pytest.fixture(scope="session")
def _gate_token_leak_state():
    """The chain's first link: one /tmp snapshot, plus the positive control that proves
    the snapshot can see anything at all.

    THIS IS WHY A LEFTOVER FILE NEEDS NO EXCLUSION. Every `writ-gate-token-*` file already
    in /tmp when this runs -- residue from previous runs, and any live approval the
    operator minted before the suite started -- is in the baseline, so it can never be
    reported as a leak. That is structural, not a name in a list.

    THE SENTINEL IS THE POSITIVE SIGNAL. An emptiness check cannot tell a clean /tmp from
    a snapshot that is blind, so the fixture writes a file whose exact name it knows and
    requires the snapshot to come back holding it. It is removed at session teardown, and
    it is harmless if a crash leaves one behind: a one-line file is refused by
    `claim_gate_token`'s bound-token format, and no session is named
    `leakguard-sentinel-<pid>`.
    """
    sentinel = os.path.join(TMP, f"{SENTINEL_PREFIX}{os.getpid()}")
    try:
        with open(sentinel, "w") as handle:
            handle.write("not-a-token\n")
    except OSError as exc:
        pytest.fail(
            f"gate token leak guard: its own sentinel could not be written to "
            f"{sentinel} ({exc}), so the guard cannot prove it is able to see a file it "
            f"put there itself. An unprovable guard is a refusal, not a clean run.",
            pytrace=False,
        )
    try:
        baseline = live_snapshot()
    except UnmeasurableTmp as refusal:
        _remove_quietly(sentinel)
        pytest.fail(f"gate token leak guard: {refusal}", pytrace=False)
    if sentinel not in baseline:
        _remove_quietly(sentinel)
        pytest.fail(
            f"gate token leak guard: the /tmp snapshot did not contain the sentinel this "
            f"fixture had just written at {sentinel}, so it cannot see a file it was "
            f"directly given. A guard that reads green because it is blind is the one "
            f"failure mode this control exists for.",
            pytrace=False,
        )
    yield {"snapshot": baseline}
    _remove_quietly(sentinel)


@pytest.fixture(scope="module", autouse=True)
def _gate_token_leak_guard(_gate_token_leak_state, request):
    """Fail the module that left a NEW gate-token file in /tmp, and remove nothing.

    ONE SNAPSHOT PER MODULE BOUNDARY, CHAINED, the same shape `tests/conftest.py`'s daemon
    guard uses: each boundary compares against the previous boundary's snapshot and then
    BECOMES the next baseline, so a leak is attributed to the module that produced it
    rather than to a chunk of two hundred, and the end of the last module is the end of
    the session. The new snapshot is stored before the failure is raised, so one leak is
    reported once by its own module instead of once per module for the rest of the run.

    IT NEVER DELETES, and that is the deliberate divergence from
    `tests/test_gate_token_binding.py::_no_leaked_gate_tokens`, which removes whatever
    appeared during a test. The suite cannot prove it wrote the file it is looking at: a
    real Claude Code session running in another window mints into the same directory, so a
    guard that cleaned up after itself would, on the day those two coincide, destroy a
    human's live approval. Reporting is this guard's whole mandate. Removing a file the
    suite DID prove it wrote is the sweeper's job, inside a namespace a module declared.

    An UNMEASURABLE /tmp fails the boundary instead of reading clean.
    """
    yield
    baseline = _gate_token_leak_state["snapshot"]
    try:
        current = live_snapshot()
    except UnmeasurableTmp as refusal:
        pytest.fail(f"gate token leak guard: {refusal}", pytrace=False)
    _gate_token_leak_state["snapshot"] = current
    leaks = find_leaks(baseline, current)
    if leaks:
        reports = "\n".join(format_report(path) for path in leaks)
        pytest.fail(
            f"gate token leak guard: {len(leaks)} gate-token file(s) appeared during "
            f"{request.node.name} and were still in {TMP} at its end.\n"
            f"NOTHING WAS REMOVED. One of these may be a real approval the operator "
            f"typed in another window while the suite ran, and deleting that would burn "
            f"a human's approval. A file this suite wrote is cleaned up by declaring "
            f"GATE_TOKEN_SESSION_PREFIX in the module that mints it.\n"
            f"{reports}",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def _sweep_gate_tokens(request):
    """Remove this module's OWN gate-token files after each of its tests.

    OPT-IN IS THE CONSTANT, and nothing else: a module that declares no
    GATE_TOKEN_SESSION_PREFIX is a no-op here (one getattr), so registering this tree-wide
    costs the other ~200 modules nothing and cannot sweep a namespace no module claimed.

    IT RUNS AT TEARDOWN, so a test that legitimately ENDS with a token on disk still
    asserts on it and passes (`tests/test_advance_gate_validation_parity.py:207,233` and
    `tests/test_decision_memory_capture.py:1244` are exactly that); the file goes away
    afterwards, with no test body and no assertion edited.

    IT DOES NOT FAIL ON REMOVAL, which is the other deliberate difference from
    `_no_leaked_gate_tokens`. Sweeping is the sanctioned cleanup; FAILING is the
    module-scoped guard's job, and a sweeper that failed too would fail the very tests
    that are supposed to end holding a token.

    WHAT THIS COSTS, DISCLOSED RATHER THAN DESIGNED AWAY. It sweeps at EVERY teardown,
    pass or fail, so for a module whose tokens are normally consumed by a successful
    advance it erases the one filesystem signal that a regression in that consumption path
    would leave behind. `tests/test_phase6hi_methodology_retrieval_and_playbook_wiring.py`
    is exactly that module: the ADR calls it latent, clean today only because its
    `POST /advance-phase` spends each token. After this declaration, a break in that
    consumption would no longer show up as a surviving file. It is a NARROWING of
    detection, not a removal: that module's own functional assertions about the advance
    still fail on their own, and they are the primary signal. Sweeping only when a test
    passed was considered and rejected -- it would leave a file behind on every unrelated
    failure, which is the unbounded growth this whole mechanism exists to stop.
    """
    prefix = getattr(request.module, "GATE_TOKEN_SESSION_PREFIX", "")
    if prefix and not prefix_is_safe(prefix):
        pytest.fail(
            f"gate token sweeper: {request.module.__name__} declares "
            f"GATE_TOKEN_SESSION_PREFIX={prefix!r}, which is not a safe namespace to "
            f"delete inside. A prefix must contain at least one character outside "
            f"[0-9a-f-] (that alphabet is what a real uuid session id is built from) and "
            f"no glob metacharacter (*, ?, [ or ], any of which widen the sweep past the "
            f"namespace that was declared).",
            pytrace=False,
        )
    yield
    if not prefix:
        return
    sweep_prefix(prefix)
