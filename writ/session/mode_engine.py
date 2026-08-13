"""Mode engine for the session helper: per-mode config, phase/gate resolvers, and the
mode get/set/switch commands (including the debug->work root-cause handoff).

POL-6d extracts this out of bin/lib/writ-session.py. It depends only on lower layers
(cache, friction, locators) and the stdlib -- never on the facade -- so the dependency
graph stays acyclic. The facade re-exports this surface, so the gate / approval /
investigation callers resolve the names unchanged.
"""

import glob
import os
import sys

from writ.session.cache import _read_cache, _write_cache, mutate_cache, record_transition
from writ.session.friction import _log_friction_event
from writ.session.locators import (
    PLAN_HASH_UNREADABLE,
    PROJECT_ROOT_MARKERS,
    _find_debug_md,
    _find_plan_md,
    is_valid_session_component,
    plan_md_hash,
)


# MODE_CONFIG is the single source of truth for per-mode gate behavior. A new
# mode (incident, review-with-gates, ...) is one entry here, not new branches:
# Work and Debug are two configs of one engine. The legacy names below are
# derived aliases, kept so existing imports/tests are unaffected.
MODE_CONFIG: dict[str, dict] = {
    "work": {
        "initial_phase": "planning",
        "gate_sequence": ["phase-a", "test-skeletons"],
        "phase_after_gate": {"phase-a": "testing", "test-skeletons": "implementation"},
    },
    # INV-8: debug is the runtime lens of the investigation engine -- its command
    # citations are runtime evidence. gate_sequence stays [] (its gate is the
    # root-cause source-edit gate, not a phase sequence).
    "debug": {"initial_phase": None, "gate_sequence": [], "phase_after_gate": {}, "source_type": "runtime"},
    "review": {"initial_phase": None, "gate_sequence": [], "phase_after_gate": {}},
    "conversation": {"initial_phase": None, "gate_sequence": [], "phase_after_gate": {}},
    # INV-1: unified investigation mode (audit / explore / research are one
    # evidence-grounded process). The lens (source_type: code-vs-rules |
    # codebase | web | runtime) is set per-invocation in later increments;
    # gate_strictness is per-source-type -- research overrides to "hard" at INV-7,
    # audit/explore stay "advisory" (audit has an oracle; explore has no done-state).
    "investigate": {
        "initial_phase": None,
        "gate_sequence": [],
        "phase_after_gate": {},
        "source_type": None,
        "gate_strictness": "advisory",
    },
}

VALID_MODES = set(MODE_CONFIG)


# WHO chose the mode, stamped into cache["mode_source"]. Two values and no third:
#   MODE_SOURCE_EXPLICIT -- a human named the mode (`mode set`, and so `writ mode set`)
#   MODE_SOURCE_AUTO     -- the writ-rag-inject.sh classifier chose it (`mode init`)
#
# WHY THE FIELD EXISTS AT ALL: both paths land in _apply_mode_set and both emit a
# mode_change row with change_type "set" and from_mode null (_mode_init's old_mode is
# always None by construction, guarded by its own already-routed check), so after the fact
# NOTHING could tell a user's stated mode from the classifier's guess. The mid-session
# re-route has to answer exactly that question before it may touch a live session's mode,
# and one incident session is on record whose single mode-establishing row could not say.
#
# A session whose source is unknown (None: a cache written before this field, or a mode
# carried forward from one) must be treated as EXPLICIT by consumers -- left alone. There
# is no truth to recover there, and guessing "auto" would hand the classifier a session
# whose mode a human may well have typed.
MODE_SOURCE_EXPLICIT = "explicit"
MODE_SOURCE_AUTO = "auto"


# Mode auto-routing classifier. Defined in the standalone stdlib-only module
# bin/lib/writ_mode_hint.py (so the UserPromptSubmit hook can import it without the
# writ-package chain, which was load-flaky inside the hook's prompt-parse block); re-exported
# here as the package-facing name so callers and tests resolve a single definition.
_BIN_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bin", "lib"
)
if _BIN_LIB not in sys.path:
    sys.path.insert(0, _BIN_LIB)
from writ_mode_hint import classify_mode_hint  # noqa: E402  (re-export; single source)

# Legacy aliases -- the same objects MODE_CONFIG["work"] holds.
GATE_SEQUENCE_WORK = MODE_CONFIG["work"]["gate_sequence"]
_PHASE_AFTER_GATE_WORK = MODE_CONFIG["work"]["phase_after_gate"]


def _initial_phase_for_mode(mode: str | None) -> str | None:
    return MODE_CONFIG.get(mode or "", {}).get("initial_phase")


def _gate_sequence_for_mode(mode: str | None) -> list[str]:
    return MODE_CONFIG.get(mode or "", {}).get("gate_sequence", [])


# INV-8: the valid investigation source types (the lens vocabulary). Relocated here in
# POL-6g-1 -- it is source-type vocabulary, not budget state; cmd_update validates
# --set-source-type against it. The lens->gate table (_LENS_TABLE) stays with cmd_lens.
_VALID_SOURCE_TYPES = {"code", "web", "runtime"}


def _source_type_for_mode(mode: str | None):
    """The investigation lens for a mode (code-vs-rules | codebase | web |
    runtime), or None when the mode has no lens. Reads MODE_CONFIG (INV-1)."""
    return MODE_CONFIG.get(mode or "", {}).get("source_type")


def _gate_strictness_for_mode(mode: str | None) -> str:
    """Per-source-type synthesis-gate strictness ("advisory" | "hard"). Safe
    default "advisory" for modes without the field. Reads MODE_CONFIG (INV-1)."""
    return MODE_CONFIG.get(mode or "", {}).get("gate_strictness", "advisory")


def _next_pending_gate(cache: dict) -> str | None:
    """Return the first gate in the mode's sequence not yet approved."""
    mode = cache.get("mode")
    if mode != "work":
        return None
    # An approval counts only for the plan it was granted against. A bare gate
    # name cannot say which plan that was, so rewriting plan.md used to leave
    # every prior approval standing: a finished cycle's gates carried into the
    # next one and the user's approval of the NEW plan advanced nothing.
    #
    # A gate with NO recorded hash re-arms. On a governance gate the safe default
    # when we cannot prove what an approval covered is to ask again, and that
    # costs one re-approval for a session whose state predates this binding.
    # Honoring unfingerprinted entries instead would leave the hole open forever.
    current_plan = plan_md_hash(cache.get("project_root"))
    bound = cache.get("gates_approved_plan", {})
    approved = {
        gate for gate in cache.get("gates_approved", [])
        if bound.get(gate) == current_plan
    }
    for gate in _gate_sequence_for_mode(mode):
        if gate not in approved:
            return gate
    return None


def _extract_root_cause(debug_md_path: str) -> str | None:
    """Return the body text of debug.md's '## Root cause' section, or None when
    the section is absent or empty. Reuses gates._section_body (the same parser
    as _validate_root_cause); returns the text for the Debug -> Work handoff."""
    # Deferred import: gates imports mode_engine at module level, so a top-level
    # import here would be circular; at call time there is no cycle.
    from writ.session.gates import _section_body
    try:
        with open(debug_md_path) as f:
            content = f.read()
    except FileNotFoundError:
        # No debug.md yet: the normal state before a debug cycle starts.
        return None
    except OSError as exc:
        # Present but unreadable silently reads as "no root cause recorded", which
        # blocks the Debug -> Work handoff for a reason the agent cannot see.
        from writ.shared.logging import emit_exception

        emit_exception("session.mode_engine.read_debug_md", exc, "", None,
                       debug_md_path=debug_md_path)
        return None
    body = _section_body(content, r'^##\s+Root\s+[Cc]ause.*$')
    return (body.strip() or None) if body else None


def _promote_root_cause_to_plan(session_id: str, mode: str) -> None:
    """Debug -> Work handoff: seed plan.md's '## Root Cause Evidence' from
    debug.md's '## Root cause'.

    Best-effort and idempotent: it never raises into the caller, so a failure can
    never break the mode transition (the cache is already written by the time
    this runs). No-op when there is no project / debug.md / populated root cause.
    """
    try:
        project_root = os.getcwd()
        markers = PROJECT_ROOT_MARKERS
        path = project_root
        while path != '/':
            if any(os.path.exists(os.path.join(path, m)) for m in markers):
                project_root = path
                break
            path = os.path.dirname(path)

        debug_md = _find_debug_md(os.path.join(project_root, "_"))
        root_cause = _extract_root_cause(debug_md) if debug_md else None

        if not root_cause:
            _log_friction_event(session_id, mode, "debug_to_work_handoff", evidence_present=False)
            return

        plan_path = _find_plan_md(project_root) or os.path.join(project_root, "plan.md")
        existing = ""
        if os.path.isfile(plan_path):
            with open(plan_path) as f:
                existing = f.read()

        if "## Root Cause Evidence" in existing:
            _log_friction_event(session_id, mode, "debug_to_work_handoff",
                                evidence_present=True, seeded=False)
            return

        section = (
            "## Root Cause Evidence\n\n"
            + root_cause.strip()
            + "\n\n_(promoted from debug.md on debug -> work)_\n"
        )
        if existing:
            if not existing.endswith("\n"):
                existing += "\n"
            content_out = existing + "\n" + section
        else:
            content_out = section
        with open(plan_path, "w") as f:
            f.write(content_out)

        _log_friction_event(session_id, mode, "debug_to_work_handoff",
                            evidence_present=True, seeded=True)
    except Exception as exc:  # graceful: a handoff failure must not break the transition
        try:
            _log_friction_event(session_id, mode, "debug_to_work_handoff",
                                evidence_present=False, error=type(exc).__name__)
        except Exception:
            pass


def _clear_gate_artifacts(project_root: str | None, session_id: str | None) -> None:
    """Delete ONE SESSION's on-disk `*.approved` artifacts after its gate state was cleared.

    An approval writes <project_root>/.claude/gates/<session_id>/<gate>.approved as an
    ARTIFACT (approval_workflow.py); enforcement itself reads gates_approved from the cache.
    Clearing only the cache therefore left the files behind, and the work-mode reminder in
    writ-rag-inject.sh reads that directory straight off disk: in this repo the artifacts sat
    approved for eighteen days, so the "plan gate pending" reminder could not fire for any
    session in that window. Writes were still blocked correctly; what was lost was the
    message telling the user which gate they were stuck behind.

    `session_id` is REQUIRED, not defaulted, because a default would make "clear my own
    approvals" indistinguishable from "clear everybody's" at the call site -- the shape that
    turned one session's `mode set` into a deletion of another session's approvals. An
    unresolvable session id clears nothing under the session directory.

    TWO THINGS ARE REMOVED, and only these two:
      1. `<gate_dir>/<session_id>/*.approved` -- this session's own artifacts.
      2. `<gate_dir>/*.approved` -- LEGACY flat artifacts from before the path carried a
         session. They are swept at every re-arm rather than migrated: approvals no longer
         survive a session end by decision, and a surviving flat file would keep reading as
         "approved" for every session in the repo. Sweeping over-blocks (a reader this
         change missed reports gate-pending), which is the safe direction to fail.
    A SIBLING SESSION's directory is never touched. Neither is the directory itself, nor a
    neighbouring non-artifact file. Never raises -- it runs after the durable cache write,
    so a failure here must not surface as a failed mode change.

    BOTH DIRECTORIES ARE CHECKED ON THEIR RESOLVED PATHS, not on the joined strings. glob
    and unlink resolve intermediate components, so a `.claude/gates` symlinked elsewhere
    made this delete files at the link TARGET, anywhere on the filesystem, which is the
    exact opposite of what the paragraph above promises. A symlinked `<session_id>`
    directory is that same escape one level down, so it gets the same check: an escaping
    session directory is skipped while the contained legacy sweep still runs, because
    refusing the whole call would leave a flat artifact readable-as-truth on the strength of
    a symlink an attacker planted.

    Individual entries need no such check, because os.unlink never follows the FINAL path
    component: removing a `*.approved` symlink removes the entry and never the file it
    points at. Skipping an escaping entry instead would be the worse failure. Six non-test
    call sites read these files as truth and every one of them tests with a symlink-
    FOLLOWING call, so a surviving `phase-a.approved` link keeps asserting an approval the
    cache no longer holds: validate-rules-helper.py `_derive_phase` would still report the
    session past the plan gate. That cache-versus-disk divergence is the whole defect this
    cleanup exists to close.
    """
    if not project_root:
        return
    try:
        root = os.path.realpath(project_root)
        contained = root.rstrip(os.sep) + os.sep
        gate_dir = os.path.realpath(os.path.join(root, ".claude", "gates"))
        if not gate_dir.startswith(contained):
            return
        session_dir = ""
        if session_id and is_valid_session_component(session_id):
            candidate = os.path.realpath(os.path.join(gate_dir, session_id))
            if candidate.startswith(contained):
                session_dir = candidate
        for directory in ([session_dir] if session_dir else []) + [gate_dir]:
            for path in glob.glob(os.path.join(directory, "*.approved")):
                os.unlink(path)
    except OSError:
        pass


def _apply_mode_set(
    cache: dict, mode: str, is_orchestrator: bool = False, *, mode_source: str | None
) -> tuple:
    """Mutate `cache` in place for a fresh mode-set; return (old_mode, new_phase).

    Pure cache mutation -- no lock, no I/O, no friction event -- so both _mode_set
    (standalone) and _mode_init (already holding the lock) apply it and let the
    OUTERMOST mutate_cache own the single write. The friction event / debug->work
    promote fire in the callers AFTER that durable write, preserving the
    write-before-log ordering the pre-lock code had.

    `mode_source` is MODE_SOURCE_EXPLICIT or MODE_SOURCE_AUTO (or None only where the
    provenance is genuinely unknown, e.g. a rotation carrying a pre-field cache forward).
    It is keyword-only and has NO DEFAULT on purpose: every path that sets a mode passes
    through here, so a new caller must state who chose the mode rather than inherit a
    default that would quietly label a guess as a human's word.
    """
    old_mode = cache.get("mode")
    old_phase = cache.get("current_phase")

    cache["mode"] = mode
    # Provenance is stamped WITH the mode, in the one function both set paths share,
    # rather than at the call sites -- the two can then never disagree about who chose it.
    cache["mode_source"] = mode_source
    if is_orchestrator:
        cache["is_orchestrator"] = True

    # Stamp the project where the mode was declared (the process cwd). Every
    # mode-bearing cache records its project so the rotation carry's same-project
    # guard has a positive per-cache identity check (safe under parallel jobs).
    cache["project_root"] = os.getcwd()

    # Fresh workflow state
    new_phase = _initial_phase_for_mode(mode)
    cache["current_phase"] = new_phase
    cache["gates_approved"] = []
    cache["gates_approved_plan"] = {}
    cache["paused_work_state"] = None
    cache["denial_counts"] = {}

    # Audit trail -- skip no-op transitions (e.g. repeated mode set work)
    if old_phase != new_phase:
        record_transition(
            cache, from_phase=old_phase, to_phase=new_phase, trigger="mode-set", mode=mode
        )
    return old_mode, new_phase


def _mode_set(session_id: str, mode: str, is_orchestrator: bool = False) -> None:
    """Set mode with fresh state. Internal -- called by cmd_mode.

    This is the EXPLICIT new-task command: it always resets current_phase to the
    mode's initial phase and clears gates_approved, so a new task never inherits
    the prior task's stale phase (test_phase_machine_reset). The AUTO-classifier
    must NOT use this -- it uses `_mode_init`, which never resets a live session.

    Being the explicit command is exactly what it stamps: MODE_SOURCE_EXPLICIT, which is
    what later tells the mid-session re-route to leave this session's mode alone.
    """
    with mutate_cache(session_id) as cache:
        old_mode, new_phase = _apply_mode_set(
            cache, mode, is_orchestrator=is_orchestrator, mode_source=MODE_SOURCE_EXPLICIT
        )
        project_root = cache.get("project_root")

    # _apply_mode_set emptied gates_approved, so the .approved artifacts have to go with
    # it or the disk keeps claiming an approval the session no longer has. Runs AFTER the
    # durable write (same write-before-log ordering as the event below), which is what
    # keeps _apply_mode_set the pure no-lock, no-I/O mutation its docstring promises.
    #
    # ASSUMES the mutate_cache block above is the OUTERMOST one for this session_id.
    # mutate_cache is reentrant per session_id and defers the write to the outermost
    # caller, so a future nested call would run this deletion while the new empty
    # gates_approved is still only in memory: the files would be gone and a crash before
    # the outer exit would leave the cache still claiming them approved. No call site
    # nests today; a new one must move the cleanup out to its own outermost caller.
    _clear_gate_artifacts(project_root, session_id)

    _log_friction_event(
        session_id, mode, "mode_change",
        change_type="set", from_mode=old_mode, to_mode=mode,
        mode_source=MODE_SOURCE_EXPLICIT,
    )

    # Debug -> Work handoff: set-work always lands in planning, so promote a
    # populated debug.md root cause into plan.md. Best-effort (never raises).
    if old_mode == "debug" and mode == "work":
        _promote_root_cause_to_plan(session_id, mode)


def _mode_init(
    session_id: str,
    mode: str,
    is_orchestrator: bool = False,
    mode_source: str | None = MODE_SOURCE_AUTO,
) -> None:
    """Auto-route helper: set mode ONLY if the session has no mode yet.

    Used by the writ-rag-inject.sh classifier to route an UNSET session to a mode
    without ever resetting an in-progress one. Unlike `_mode_set` (the explicit
    new-task command, which resets to fresh state), this is a NO-OP when a mode is
    already set, so the classifier re-firing on a transient empty cache read can
    never wipe a live gate cycle back to planning. Idempotent. The authoritative
    "already set?" check is under the lock.

    `mode_source` defaults to MODE_SOURCE_AUTO because the classifier is this function's
    reason to exist. The ONE caller that overrides it is rotation.carry_forward_mode,
    which is not choosing a mode at all -- it is moving one that was already chosen -- and
    so passes the pre-rotation session's own source through (including None, when that
    session predates the field). Labelling a carried explicit mode "auto" would hand a
    human's stated mode to the classifier; labelling a carried auto mode "explicit" would
    freeze a guess in place for the rest of the rotated session.
    """
    # Fast path: an unlocked read avoids a per-turn locked rewrite in the common
    # already-routed case (the classifier fires mode-init every turn).
    if _read_cache(session_id).get("mode"):
        return
    with mutate_cache(session_id) as cache:
        if cache.get("mode"):
            return  # already routed; never reset a live session
        # Atomic check-then-set under the lock; apply the mutation directly (not via
        # _mode_set) so the durable write happens at THIS block's exit, before the
        # friction event below (matching direct _mode_set's write-before-log order).
        old_mode, _ = _apply_mode_set(
            cache, mode, is_orchestrator=is_orchestrator, mode_source=mode_source
        )
        project_root = cache.get("project_root")

    # Only the routing path reaches here (the already-routed check above returns), so this
    # clears artifacts exactly when _apply_mode_set emptied gates_approved. After the
    # durable write, for the same reason as in _mode_set, and on the same assumption: the
    # mutate_cache block above must be the OUTERMOST one for this session_id, or the files
    # go before the emptied cache is durable.
    #
    # NARROWER THAN IT LOOKS since the artifacts became session-scoped: a freshly routed
    # session has an empty directory of its own, so this is a no-op in the common case. It
    # stays load-bearing for the documented mode=None wipe class, where a session that HAD a
    # mode and approvals lost the mode and is then re-routed: its own directory still holds
    # phase-a.approved while its cache says no mode. Do not delete this as dead code.
    _clear_gate_artifacts(project_root, session_id)

    # old_mode is always None here (guarded above), so the debug->work promote never
    # applies via this path; emit the mode_change event after the durable write.
    #
    # mode_source is on the row because from_mode CANNOT carry this: it is null on this
    # path by construction AND null on the first explicit `mode set` of a session, so the
    # row that establishes a mode was the one row in the stream that could not say who
    # established it. One incident session's whole history was a single such row.
    _log_friction_event(
        session_id, mode, "mode_change",
        change_type="set", from_mode=old_mode, to_mode=mode,
        mode_source=mode_source,
    )


def _mode_switch(session_id: str, mode: str) -> None:
    """Switch mode, preserving Work state if leaving/returning.

    It also preserves cache["mode_source"], by not touching it. A switch changes WHICH
    mode the session is in; it does not change the story of who chose to put this session
    under Writ's workflow in the first place. Overwriting the source here would erase the
    only signal the mid-session re-route has: an auto-routed session that took one
    classifier-driven detour would come back looking hand-set, and could never be routed
    again; an explicitly-set session that a user switched by hand would look auto-set, and
    the classifier would then feel free to move it.
    """
    with mutate_cache(session_id) as cache:
        old_mode = cache.get("mode")
        old_phase = cache.get("current_phase")
        project_root = cache.get("project_root")
        # Read, never written: this is the preservation. Carried onto the telemetry row so
        # a switch row states the provenance it inherited instead of leaving a reader to
        # walk back to whichever earlier row established the mode.
        mode_source = cache.get("mode_source")
        restored = False
        pivoted = False

        # Save Work state when leaving Work
        if old_mode == "work" and mode != "work":
            cache["paused_work_state"] = {
                "phase": cache.get("current_phase"),
                "gates_approved": cache.get("gates_approved", []),
                "loaded_rule_ids_by_phase": cache.get("loaded_rule_ids_by_phase", {}),
                # Fingerprint the plan these approvals were granted against. The key is
                # ALWAYS written (None when there is no plan) so the return path can tell
                # "no plan at pause" from "this state predates the fingerprint".
                "plan_hash": plan_md_hash(project_root),
            }

        # Restore Work state when returning to Work
        if mode == "work" and cache.get("paused_work_state"):
            paused = cache["paused_work_state"]
            # Restore-or-re-arm is decided by the file, not by the model's word for it.
            # Same bytes means the same work, so the detour costs nothing. Different bytes
            # (which includes absent at exactly one end) means the plan pivoted while the
            # session was away, and approvals granted against the old plan do not cover it.
            #
            # An UNREADABLE plan is never "the same": the fingerprint could not be taken,
            # so equality here would be equality of two failures, not of two files, and the
            # bytes may well have changed while the session was paused. Re-arming costs one
            # re-approval; restoring on an unchecked file hands back approvals that may no
            # longer cover the plan. Absent at both ends still restores: that is genuinely
            # equal, because no plan means nothing pivoted.
            current_hash = plan_md_hash(project_root)
            if current_hash == paused.get("plan_hash") and current_hash != PLAN_HASH_UNREADABLE:
                cache["current_phase"] = paused["phase"]
                cache["gates_approved"] = paused["gates_approved"]
                cache["loaded_rule_ids_by_phase"] = paused.get("loaded_rule_ids_by_phase", {})
                cache["paused_work_state"] = None
                new_phase = paused["phase"]
                restored = True
            else:
                cache["current_phase"] = "planning"
                cache["gates_approved"] = []
                cache["gates_approved_plan"] = {}
                cache["paused_work_state"] = None
                new_phase = "planning"
                pivoted = True
        elif mode == "work":
            # No paused state -- fresh start
            cache["current_phase"] = "planning"
            cache["gates_approved"] = []
            cache["gates_approved_plan"] = {}
            new_phase = "planning"
        else:
            cache["current_phase"] = None
            new_phase = None

        cache["mode"] = mode

        # Audit trail -- collapse restore into single event, skip no-ops
        if restored:
            record_transition(
                cache, from_phase=old_phase, to_phase=new_phase, trigger="mode-switch-restore", mode=mode
            )
        elif old_phase != new_phase:
            record_transition(
                cache, from_phase=old_phase, to_phase=new_phase, trigger="mode-switch", mode=mode
            )

    # A pivot re-armed the cache to planning with nothing approved, so the artifacts go
    # too. A RESTORE deliberately keeps them: those approvals were just restored and are
    # still valid. After the durable write, as in _mode_set, and on the same assumption:
    # the mutate_cache block above must be the OUTERMOST one for this session_id, or the
    # files go before the re-armed cache is durable.
    if pivoted:
        _clear_gate_artifacts(project_root, session_id)

    _log_friction_event(
        session_id, mode, "mode_change",
        change_type="switch", from_mode=old_mode, to_mode=mode,
        mode_source=mode_source,
    )

    # Debug -> Work handoff: only when landing in a fresh planning phase (not
    # when restoring a paused Work state). Best-effort (never raises).
    if old_mode == "debug" and mode == "work" and new_phase == "planning":
        _promote_root_cause_to_plan(session_id, mode)


def cmd_mode(session_id: str, subcmd: str, value: str | None = None, is_orchestrator: bool = False) -> None:
    """Get, set, or switch the session mode."""
    if subcmd == "get":
        cache = _read_cache(session_id)
        mode = cache.get("mode")
        if mode:
            sys.stdout.write(mode)
        sys.stdout.write("\n")
        return

    if subcmd not in ("set", "switch", "init"):
        print(f"Unknown mode subcommand: {subcmd}", file=sys.stderr)
        sys.exit(2)

    if value is None:
        print(f"Usage: writ-session.py mode {subcmd} <conversation|debug|investigate|review|work> <session_id>", file=sys.stderr)
        sys.exit(2)

    mode = value.lower()
    if mode not in VALID_MODES:
        print(f"Invalid mode: {value} (must be one of: {', '.join(sorted(VALID_MODES))})", file=sys.stderr)
        sys.exit(1)

    if subcmd == "set":
        _mode_set(session_id, mode, is_orchestrator=is_orchestrator)
        sys.stdout.write(f"set: {mode}\n")
    elif subcmd == "init":
        _mode_init(session_id, mode, is_orchestrator=is_orchestrator)
        sys.stdout.write(f"init: {mode}\n")
    else:
        _mode_switch(session_id, mode)
        sys.stdout.write(f"switch: {mode}\n")


# POL-6e: relocated here (shared by the read gate and cmd_lens; wraps _source_type_for_mode).
def _effective_source_type(cache: dict):
    """INV-8: the active investigation lens for a session.

    The per-invocation session `source_type` (set via --set-source-type) wins; otherwise
    the mode's static default from MODE_CONFIG -- so a debug session reports "runtime"
    automatically and an investigate session reports whatever lens was selected (or None).
    """
    st = cache.get("source_type")
    if st:
        return st
    return _source_type_for_mode(cache.get("mode"))
