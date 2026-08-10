"""Phase-advance and gate validation for the session helper (POL-6f).

The work-mode gate validators (plan-completeness, gate-final, test-skeletons + the rule-ID
hallucination detector _validate_citations), the gate->validator registry, and the
cmd_advance_phase / cmd_current_phase commands. Depends only on lower layers
(cache/friction/locators/mode_engine) -- never the facade -- so the graph stays acyclic. The
facade re-exports this surface, so main()'s advance/current-phase dispatch resolves unchanged.
"""

import os
import re
import sys

from writ.session.cache import _read_cache, mutate_cache, record_transition
from writ.session.friction import _log_friction_event
from writ.session.gate_token import (
    BINDING_GATE_MISMATCH,
    BINDING_PLAN_DRIFT,
    BINDING_UNBOUND,
    claim_gate_token,
    gate_binding_refusal,
    gate_token_valid,
    read_gate_binding,
    read_gate_token,
)
from writ.session.locators import _find_plan_md, plan_md_hash, resolve_project_root
from writ.session.mode_engine import (
    MODE_CONFIG,
    _gate_sequence_for_mode,
    _initial_phase_for_mode,
    _next_pending_gate,
)
from writ.session.cli_io import _emit_json

# A fully-annotated ## Files bullet: a backtick path, a (change_type), and a
# non-empty reason after ' -- '. The (\S.*) group rejects a trailing-blank reason.
_FILES_LINE_RE = re.compile(r'^-\s+`[^`]+`\s+\((\w+)\)\s+--\s+(\S.*)$')
# A ## Files bullet that names a backtick path (with or without a reason).
_FILES_PATH_ONLY_RE = re.compile(r'^-\s+`[^`]+`')
# A bold-change-type ## Files bullet: a **change_type** prefix, a backtick path,
# and a non-empty reason after ' -- '. The (\S.*) group rejects a trailing-blank
# reason, mirroring _FILES_LINE_RE. Groups: (change_type, path, reason).
_FILES_BOLD_LINE_RE = re.compile(r'^-\s+\*\*(\w+)\*\*\s+`([^`]+)`\s+--\s+(\S.*)$')
# A bold-change-type ## Files bullet that names a backtick path (with or without a reason).
_FILES_BOLD_PATH_ONLY_RE = re.compile(r'^-\s+\*\*\w+\*\*\s+`[^`]+`')

# The canonical fill-in plan skeleton. Every rejection message names it, so a session
# learns the format from ONE artifact instead of re-deriving it from each rejection.
_PLAN_TEMPLATE_REF = 'templates/plan-template.md'

# A cited id in ## Rules Applied: the canonical RULE-DOMAIN-NNN shape, or an
# abstraction id, whose trailing segment is a label rather than three digits
# (ABS-TESTING-E2E). Summary-mode injection renders abstractions as '[ABSTRACT: <id>]'
# blocks, so the model sees and naturally cites them like any other id; excluding that
# SHAPE made every such citation read as a hallucination. Widening the shape cannot
# widen what is citable -- the check below still admits only ids the session loaded.
_CITED_ID_RE = re.compile(r'ABS(?:-[A-Z0-9]+)+|[A-Z][A-Z0-9]+(?:-[A-Z][A-Z0-9]+)*-\d{3}')


def _validate_citations(cited: set, available: set) -> set:
    """INV-2: the mode-agnostic citation-hallucination detector.

    Returns the cited artifacts absent from the auto-captured `available` set.
    An empty `available` yields the empty set -- absence cannot be proven when
    nothing was captured (mirrors _validate_phase_a's historical `if loaded_ids:`
    guard). The same set-difference catches a hallucinated rule ID today and a
    hallucinated URL (INV-7) tomorrow. Presence, never truth.
    """
    if not available:
        return set()
    return set(cited) - set(available)


def _validate_phase_a(project_root: str, session_id: str = "") -> str | None:
    """Validate plan.md for phase-a gate. Returns error message or None."""
    import re
    plan_path = _find_plan_md(project_root)
    if not plan_path:
        return (f"plan.md not found. Write it by filling in {_PLAN_TEMPLATE_REF} in the "
                "Writ skill directory, which encodes this gate's exact contract: "
                "## Files (one bullet per file: - `path` (change_type) -- reason), "
                "## Analysis (what and why, contracts, integration points), "
                "## Rules Applied (cite ONLY rule IDs injected in this session's WRIT RULES blocks), "
                "## Capabilities (- [ ] checkbox per testable behavior, all unchecked). "
                "All four sections are required in a single write. Do not present partial plans.")
    with open(plan_path) as f:
        content = f.read()
    missing = []
    files_match = re.search(r'^##\s+Files', content, re.MULTILINE)
    if not files_match:
        missing.append('## Files')
    else:
        section_start = files_match.end()
        rest = content[section_start:]
        next_section = re.search(r'^## ', rest, re.MULTILINE)
        section_text = rest[:next_section.start()] if next_section else rest
        for line in section_text.splitlines():
            stripped = line.strip()
            # Legacy markdown-table rows (pipe-delimited with a reason) are accepted.
            if stripped.startswith('|'):
                continue
            # A bullet that names a backtick path but lacks a non-empty reason after
            # the ' -- ' separator has nothing for the harvest to capture.
            if _FILES_PATH_ONLY_RE.match(stripped) and not _FILES_LINE_RE.match(stripped):
                missing.append(
                    '## Files line missing a reason (use: - `path` (change_type) -- reason, '
                    f'as modeled in {_PLAN_TEMPLATE_REF}): ' + stripped
                )
            # The bold-change-type shape ('- **type** `path` -- reason') is a
            # first-class ## Files form; a bold bullet naming a path but lacking a
            # reason is flagged with the same message as the canonical shape.
            if _FILES_BOLD_PATH_ONLY_RE.match(stripped) and not _FILES_BOLD_LINE_RE.match(stripped):
                missing.append(
                    '## Files line missing a reason (use: - `path` (change_type) -- reason, '
                    f'as modeled in {_PLAN_TEMPLATE_REF}): ' + stripped
                )
    if not re.search(r'^##\s+Analysis', content, re.MULTILINE):
        missing.append('## Analysis')
    rules_match = re.search(r'^##\s+Rules\s+[Aa]pplied', content, re.MULTILINE)
    if not rules_match:
        missing.append('## Rules Applied')
    else:
        section_start = rules_match.end()
        rest = content[section_start:]
        next_section = re.search(r'^## ', rest, re.MULTILINE)
        section_text = rest[:next_section.start()] if next_section else rest
        has_rule_id = bool(_CITED_ID_RE.search(section_text))
        has_no_match = bool(re.search(r'[Nn]o matching rules', section_text))
        if not has_rule_id and not has_no_match:
            missing.append('rule ID or "No matching rules" in ## Rules Applied')
        # Validate cited rule IDs against session's loaded_rule_ids
        elif has_rule_id and session_id:
            cited_ids = set(_CITED_ID_RE.findall(section_text))
            cache = _read_cache(session_id)
            # Collect all rule IDs loaded across all phases
            loaded_ids = set(cache.get("loaded_rule_ids", []))
            by_phase = cache.get("loaded_rule_ids_by_phase", {})
            for phase_ids in by_phase.values():
                loaded_ids.update(phase_ids)
            # Always-on rules are injected into every prompt by a channel that used to
            # record only its token count, so citing one -- the natural thing to do --
            # was reported as a hallucination and SPENT the user's approval token. This
            # widens the legitimate set; it never narrows it, so nothing that validates
            # today can start failing.
            loaded_ids.update(cache.get("always_on_rule_ids", []))
            hallucinated = _validate_citations(cited_ids, loaded_ids)
            if hallucinated:
                _log_friction_event(
                    session_id, cache.get("mode"),
                    "hallucinated_rule_ids",
                    cited=sorted(cited_ids),
                    loaded=sorted(loaded_ids),
                    hallucinated=sorted(hallucinated),
                )
                missing.append(
                    f'hallucinated rule IDs in ## Rules Applied: {", ".join(sorted(hallucinated))}. '
                    f'Only cite rules from the injected --- WRIT RULES --- block'
                )
    caps_match = re.search(r'^##\s+Capabilities', content, re.MULTILINE)
    if not caps_match:
        missing.append('## Capabilities (use checkbox format: - [ ] description)')
    else:
        section_start = caps_match.end()
        rest = content[section_start:]
        next_section = re.search(r'^## ', rest, re.MULTILINE)
        section_text = rest[:next_section.start()] if next_section else rest
        if not re.search(r'\[[ x]\]', section_text):
            missing.append('## Capabilities must use checkbox format: - [ ] description (not dashes or bullets)')
        # Capabilities must start unchecked -- pre-checked boxes bypass verification
        elif re.search(r'\[x\]', section_text):
            missing.append('capabilities must start as [ ] (unchecked), not [x]. They are checked after implementation')
    if missing:
        return f"plan.md validation failed: {'; '.join(missing)}. Fix ALL issues in one edit."
    return None


def _validate_test_skeletons(project_root: str, session_id: str = "") -> str | None:
    """Validate that at least one test file with a method signature was written this session.

    Checks files_written in the session cache first. If session tracking is available,
    only files written this session count. Falls back to scanning the project if no
    session is provided.
    """
    import re

    method_patterns = [
        r'function\s+test\w+', r'def\s+test_\w+', r'func\s+Test\w+',
        r'fn\s+test_\w+', r'it\s*\(', r'test\s*\(', r'describe\s*\(',
        r'@Test',
    ]

    test_path_patterns = [
        r'/Test/', r'/tests/', r'/test/', r'/__tests__/',
        r'Test\.php$', r'test_.*\.py$', r'_test\.go$', r'_test\.rs$',
        r'\.test\.[jt]sx?$', r'\.spec\.[jt]sx?$',
    ]

    # Check session-tracked files first
    if session_id:
        cache = _read_cache(session_id)
        files_written = cache.get("files_written", [])
        for filepath in files_written:
            if not any(re.search(p, filepath) for p in test_path_patterns):
                continue
            if not os.path.isfile(filepath):
                continue
            try:
                with open(filepath) as f:
                    content = f.read()
                for mp in method_patterns:
                    if re.search(mp, content):
                        return None  # found a valid session test
            except OSError:
                continue

    # Fallback: scan project for test files (excludes vendor/node_modules)
    import glob
    file_patterns = [
        '**/Test/**/*Test.php', '**/tests/**/*test*.py', '**/test/**/*test*.py',
        '**/__tests__/**/*.test.*', '**/tests/**/*_test.go', '**/test/**/*_test.rs',
        '**/test_*.py', '**/*.test.ts', '**/*.test.js', '**/*.spec.ts', '**/*.spec.js',
    ]
    for pat in file_patterns:
        full = os.path.join(project_root, pat)
        matches = glob.glob(full, recursive=True)
        matches = [m for m in matches if '/vendor/' not in m and '/node_modules/' not in m]
        for match in matches:
            try:
                with open(match) as f:
                    content = f.read()
                for mp in method_patterns:
                    if re.search(mp, content):
                        return None  # found a valid test
            except OSError:
                continue
    return "No test files found with test method signatures. Write test skeleton files to disk before requesting approval."


# Gate -> validation function mapping
# Gate -> validation function registry. Dispatched by cmd_advance_phase so
# adding a gate is a registry entry, not a new branch. Validators keep the
# (project_root, session_id="") -> str | None signature.
_GATE_VALIDATORS: dict[str, object] = {
    "phase-a": _validate_phase_a,
    "test-skeletons": _validate_test_skeletons,
}


# What to tell the user when the token they hold does not authorize the gate now
# pending. Each message names the cause and says plainly whether a fresh approval is
# needed, because the failure mode being fixed is a refusal the user read as "no gate is
# pending" and then retried unchanged.
_BINDING_REFUSAL_REASONS: dict[str, str] = {
    BINDING_GATE_MISMATCH: (
        "Your approval is bound to the {bound} gate, but the {target} gate is what is "
        "pending now. Approve again while {target} is the gate being asked about."
    ),
    BINDING_PLAN_DRIFT: (
        "plan.md changed after this approval was given, so the approval no longer covers "
        "the plan on disk. Review the current plan and approve again."
    ),
    BINDING_UNBOUND: (
        "This gate token records nothing about what it authorizes (it is the pre-binding "
        "format), so it cannot be checked against the {target} gate. Approve again to "
        "mint a bound token."
    ),
}


def _detect_project_root(project_root: str) -> str:
    """Return project_root as given, else the marker dir at or above cwd, else cwd.

    Thin wrapper over locators.resolve_project_root, which owns the tier order for
    every gate caller (CLI here, HTTP route in server/routes/gate.py). os.getcwd() is
    supplied HERE because this is the CLI path, where the process cwd IS the user's
    cwd; the resolver never reads cwd on its own (see its docstring for why that
    matters to the daemon).
    """
    root, _tier = resolve_project_root(explicit=project_root, start=os.getcwd())
    return root


def apply_phase_advance(
    cache: dict,
    target_gate: str,
    old_phase: str,
    new_phase: str,
    *,
    trigger: str,
    mode: str | None,
    confirmation_source: str | None = None,
    artifacts_validated: list | None = None,
) -> None:
    """The single shared cache-mutation unit for an approved-gate phase advance.

    Both advance callers (cmd_advance_phase = CLI/token path; server._advance =
    live HTTP path) delegate ALL six cache mutations here so the fields can no
    longer drift between paths. Mutates `cache` in place; the caller still owns
    _read_cache / _write_cache. Robust to a near-empty cache (uses .get defaults
    and creates missing dicts).

    The six mutations:
      1. gates_approved   -- sorted set-union add of target_gate (idempotent).
      2. current_phase    -- set to new_phase.
      3. denial_counts    -- pop target_gate (cleared on a successful advance).
      4. loaded_rule_ids_by_phase -- roll old_phase IDs into _historical, empty
         the old_phase bucket, seed the new_phase bucket.
      5. phase_transitions -- append one record via record_transition (full
         schema: from/to/UTC-aware ts/trigger/mode/gate/artifacts_validated).
         confirmation_source is additive: included only when not None (the live
         path), so the CLI path's record stays free of the key.
      6. escalation       -- clear it when the approved gate IS the escalated one
         (a resolved escalation must stop reprinting the "do not proceed" banner).
    """
    # 1. gates_approved -- sorted set-union add (idempotent, preserves prior gates).
    cache["gates_approved"] = sorted(set(cache.get("gates_approved", [])) | {target_gate})

    # 2. current_phase.
    cache["current_phase"] = new_phase

    # 3. denial_counts -- clear the advanced gate's count.
    denial_counts = cache.get("denial_counts", {})
    denial_counts.pop(target_gate, None)
    cache["denial_counts"] = denial_counts

    # 4. loaded_rule_ids_by_phase -- roll old-phase IDs to _historical, seed new phase.
    by_phase = cache.get("loaded_rule_ids_by_phase", {})
    current_ids = by_phase.get(old_phase, [])
    if current_ids:
        by_phase.setdefault("_historical", []).extend(current_ids)
        by_phase[old_phase] = []
    by_phase.setdefault(new_phase, [])
    cache["loaded_rule_ids_by_phase"] = by_phase

    # 5. phase_transitions -- one full-schema audit record. confirmation_source is
    # additive (live path only): omit the key entirely when None.
    extra = {"confirmation_source": confirmation_source} if confirmation_source is not None else {}
    record_transition(
        cache,
        from_phase=old_phase,
        to_phase=new_phase,
        trigger=trigger,
        mode=mode,
        gate=target_gate,
        artifacts_validated=(artifacts_validated or []),
        **extra,
    )

    # 6. escalation -- clear when the escalated gate is the one being approved. A
    # resolved escalation must stop cmd_check_escalation reporting needed=True (the
    # "do not proceed" banner reprinting every prompt). Only the matching gate
    # resets: gates advance in sequence, so an escalation on a later gate is not
    # cleared by advancing an earlier one. invalidation_history is left as the audit
    # trail; the banner is keyed on escalation.needed alone.
    esc = cache.get("escalation")
    if isinstance(esc, dict) and esc.get("needed") and esc.get("gate") == target_gate:
        cache["escalation"] = {
            "gate": None, "needed": False, "diagnosis": None, "feedback_sent": False,
        }


def _log_phase_token_summary(session_id: str, mode, cache: dict, old_phase: str) -> None:
    """Emit phase_token_summary from the token snapshots accumulated during old_phase."""
    snapshots = cache.get("token_snapshots", [])
    phase_snapshots = [s for s in snapshots if s.get("phase") == old_phase]
    if phase_snapshots:
        pcts = [s.get("context_percent", 0) for s in phase_snapshots]
        tokens = [s.get("context_tokens", 0) for s in phase_snapshots]
        _log_friction_event(
            session_id, mode, "phase_token_summary",
            phase=old_phase, snapshot_count=len(phase_snapshots),
            peak_context_percent=max(pcts) if pcts else 0,
            peak_context_tokens=max(tokens) if tokens else 0,
            final_context_percent=pcts[-1] if pcts else 0,
            final_context_tokens=tokens[-1] if tokens else 0,
        )


def cmd_advance_phase(session_id: str, project_root: str = "", token: str = "") -> None:
    """Validate artifacts and advance to the next phase gate.

    Creates gate file on disk as artifact. Updates session cache as source of truth.
    Clears current-phase loaded_rule_ids. Logs transition to audit trail.

    Requires a --token matching the gate token created by auto-approve-gate.sh.
    This prevents the agent from calling advance-phase directly via Bash.

    Output: JSON {"advanced": true, "gate": "...", "phase": "..."} or
            {"advanced": false, "reason": "..."}
    """
    # Validate caller token (shared mechanism: writ.session.gate_token).
    expected_token = read_gate_token(session_id)

    if not gate_token_valid(token, expected_token):
        cache = _read_cache(session_id)
        _log_friction_event(
            session_id, cache.get("mode"),
            "agent_self_approval_blocked",
            had_token=bool(token),
            had_expected=bool(expected_token),
        )
        _emit_json({"advanced": False, "reason": "Invalid or missing gate token. Gates can only be advanced by the approval hook, not by the agent."})
        sys.stdout.write("\n")
        return

    # Consume stdin (hooks may pipe prompt text)
    sys.stdin.read()

    # C2 (audit): the decision-and-write span runs under mutate_cache's per-session
    # flock (atomic read-modify-write) so a concurrent cache writer's update is not
    # lost. The early-return branches (no-gate / all-approved / validator-error) exit
    # cleanly and harmlessly rewrite the unchanged cache. The post-write token
    # consumption, gate-file creation, and success JSON stay outside the lock.
    with mutate_cache(session_id) as cache:
        mode = cache.get("mode")

        # Gate config is read from MODE_CONFIG. Modes without a gate sequence
        # (debug/review/conversation/none) have nothing to advance.
        gate_sequence = _gate_sequence_for_mode(mode)
        if not gate_sequence:
            _emit_json({"advanced": False, "reason": "No gates for this mode"})
            sys.stdout.write("\n")
            return

        # Find next pending gate (shared scan: mode_engine._next_pending_gate; reached
        # only in work mode here, where its mode=="work" guard matches this path).
        target_gate = _next_pending_gate(cache)

        if target_gate is None:
            _emit_json({"advanced": False, "reason": "All gates already approved"})
            sys.stdout.write("\n")
            return

        project_root = _detect_project_root(project_root)

        # Validate artifacts for the target gate via the gate->validator registry.
        validator = _GATE_VALIDATORS.get(target_gate)
        error = validator(project_root, session_id) if validator else None

        if error:
            _emit_json({"advanced": False, "reason": error, "gate": target_gate})
            sys.stdout.write("\n")
            return

        # The approval must authorize THIS gate and THIS plan. The fingerprint comes
        # from the cache's project_root, which is the same input cmd_current_phase
        # reports to the approval hook at mint time, so the mint and the claim hash the
        # same file by construction; deriving one of them from a separately-resolved
        # root would refuse legitimate approvals whenever the two roots differ.
        plan_hash = plan_md_hash(cache.get("project_root")) or ""
        refusal = gate_binding_refusal(session_id, gate=target_gate, plan_hash=plan_hash)
        if refusal:
            # Refuse WITHOUT claiming: the token may legitimately authorize something
            # else (another gate, a candidate promotion), and spending it here would
            # destroy an approval the user did give. One friction event per refusal
            # class, so the log can tell a fail-closed gate from an absent one.
            binding = read_gate_binding(session_id)
            bound_gate = binding[0] if binding else ""
            _log_friction_event(
                session_id, mode, refusal,
                gate=target_gate, bound_gate=bound_gate,
            )
            _emit_json({
                "advanced": False,
                "gate": target_gate,
                "reason": _BINDING_REFUSAL_REASONS[refusal].format(
                    bound=bound_gate, target=target_gate,
                ),
            })
            sys.stdout.write("\n")
            return

        # G1 concurrency: atomically CLAIM the token before mutating. Exactly one
        # concurrent CLI advance wins; the loser returns a no-op WITHOUT advancing,
        # so one token can never advance two gates (recompute-under-lock double-
        # advance -- see project_advance_phase_token_race). The claim REPLACES the
        # post-lock consume_gate_token below: claiming IS consuming. It sits AFTER
        # the validator so a validator error / no-op still does not consume.
        if not claim_gate_token(session_id, token, gate=target_gate, plan_hash=plan_hash):
            _emit_json({
                "advanced": False,
                "reason": (
                    "Gate token already consumed by a concurrent approval; this "
                    "duplicate advance is a no-op."
                ),
            })
            sys.stdout.write("\n")
            return

        # Validation passed -- update cache
        old_phase = cache.get("current_phase", "planning")
        new_phase = MODE_CONFIG.get(mode, {}).get("phase_after_gate", {}).get(target_gate, "implementation")

        # Audit-trail artifacts: the plan.md rel-path on every gate but test-skeletons.
        artifacts = []
        plan_path = _find_plan_md(project_root)
        if plan_path and target_gate != "test-skeletons":
            artifacts.append(os.path.relpath(plan_path, project_root))

        # All five cache mutations (gates_approved, current_phase, denial_counts,
        # loaded_rule_ids_by_phase, phase_transitions) go through the shared unit.
        apply_phase_advance(
            cache, target_gate, old_phase, new_phase,
            trigger="user-approved", mode=mode,
            artifacts_validated=artifacts, confirmation_source=None,
        )

    # H3/G1: the claim above (inside the lock, after the validator) consumed the
    # gate token -- claiming IS consuming -- so one user approval authorizes exactly
    # one advance and the genuine token cannot be replayed through repeated CLI
    # advance calls. The old post-lock consume_gate_token(session_id) is removed:
    # the claim both mutual-excludes concurrent advances and spends the token.
    _log_phase_token_summary(session_id, mode, cache, old_phase)

    # Create gate file on disk as artifact (not source of truth)
    gate_dir = os.path.join(project_root, ".claude", "gates")
    os.makedirs(gate_dir, exist_ok=True)
    gate_file = os.path.join(gate_dir, f"{target_gate}.approved")
    with open(gate_file, "w") as f:
        f.write(session_id + "\n")

    _emit_json({
        "advanced": True,
        "gate": target_gate,
        "phase": new_phase,
        "from_phase": old_phase,
    })
    sys.stdout.write("\n")


def cmd_current_phase(session_id: str) -> None:
    """Return the authoritative current phase from session state.

    Output: JSON {"phase": "...", "mode": "...", "gates_approved": [...],
                  "next_gate": "..."|null, "plan_hash": "..."|null}

    next_gate and plan_hash are the token binding the approval hook mints with. They are
    reported HERE because the hook already makes this call once per approval turn and the
    single cache read already has both answers in hand, so the binding costs the
    per-prompt path nothing. null means "nothing to bind to": no gate is pending, or
    there is no plan.md under the session's project root.
    """
    cache = _read_cache(session_id)
    mode = cache.get("mode")
    phase = cache.get("current_phase")

    # Derive phase if not set
    if phase is None and mode is not None:
        phase = _initial_phase_for_mode(mode)

    _emit_json({
        "phase": phase or "unclassified",
        "mode": mode,
        "gates_approved": cache.get("gates_approved", []),
        "next_gate": _next_pending_gate(cache),
        "plan_hash": plan_md_hash(cache.get("project_root")),
    })
    sys.stdout.write("\n")
