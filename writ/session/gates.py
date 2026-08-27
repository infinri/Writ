"""Write/read gates for the session helper (POL-6e).

The two-gate work-mode write gate, the debug-mode root-cause / runtime-lens gates, and the
gate-categories matching helpers. Depends only on lower layers (cache/friction/locators/
mode_engine) -- never the facade -- so the dependency graph stays acyclic. The facade
re-exports this surface, so server.py (/pre-write-check) and main()'s can-write / can-read
dispatch resolve the names unchanged.
"""

import fnmatch
import json
import os
import re
import sys

from writ.session.cache import _read_cache, mutate_cache
from writ.session.friction import _log_friction_event
from writ.session.locators import _find_debug_md, debug_path
from writ.session.mode_engine import _effective_source_type, approved_gates_for_plan

# Fallback gate-categories.json path: <skill_root>/bin/lib/gate-categories.json. Used only
# when a caller passes no skill_dir (hooks pass it); resolved from the skill root because this
# module lives at writ/session/, three levels below the root.
_GATE_CATEGORIES_FALLBACK = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin", "lib", "gate-categories.json",
)

_CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".php", ".go", ".rs", ".java", ".rb", ".c",
    ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cs", ".swift", ".kt", ".kts", ".scala",
    ".m", ".mm", ".sql", ".sh", ".bash", ".pl", ".lua", ".ex", ".exs", ".clj", ".vue", ".svelte",
}

# Credential/secret file path patterns (#6). Writes to these are denied in EVERY
# mode, before any exemption or gate, matched on the PATH ONLY -- contents are
# never opened (the org credential-read ban applies to reads; the same boundary is
# extended to writes so an agent cannot plant or overwrite secret material). This is
# the SINGLE SOURCE: the Bash-redirect gate (hooks/scripts/writ-bash-write-gate.sh)
# imports _is_credential_path from here (with a minimal inline fallback only if the
# package import fails), so the two cannot drift.
_CREDENTIAL_BASENAME_GLOBS = (
    "*.key", "*.pem", "*.p12", "*.pfx", "*.keystore", "*.jks", "*.ppk",
    "*.asc", "*.gpg", "*.env",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    ".htpasswd", ".pgpass", ".netrc", ".npmrc", ".pypirc", ".dockercfg",
    "kubeconfig",
    ".env", ".env.*",
)
# Extensions that, when carried by a *.pub file (server.key.pub), mean the ".pub" is
# hiding private-key material -- so the public-key exemption does NOT apply.
_CREDENTIAL_KEY_EXTS = (".key", ".pem", ".p12", ".pfx", ".keystore", ".jks", ".ppk")
# A path containing one of these segments is credential-bearing regardless of basename.
_CREDENTIAL_DIR_SEGMENTS = ("/.ssh/", "/secrets/", "/secret/", "/.gnupg/", "/.kube/")
# Non-secret look-alikes that the .env globs would otherwise catch -- template/example
# env files carry no secrets and must stay writable so scaffolding is not blocked.
_CREDENTIAL_ALLOW_BASENAMES = (
    ".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults",
    "example.env", "sample.env", "template.env",
)

# Only these exact basenames DIRECTLY under ~/.claude are write-exempt. A bare
# startswith("~/.claude/settings") prefix let settings-evil.json or settingsX/anything
# bypass every write gate (and be logged as settings_exempt). Match exact names only.
_EXEMPT_SETTINGS_BASENAMES = frozenset({"settings.json", "settings.local.json"})


def _is_credential_path(path: str) -> bool:
    """True if `path` names a credential/secret file. PATH-ONLY -- never opens the file.

    Order matters: a secret-bearing DIRECTORY (/.ssh/, /secrets/, ...) wins over any
    basename exemption, so a .pub or .env.example planted there is still denied. Matching
    is case-insensitive (cert.PEM / ID_RSA evade otherwise on case-preserving filesystems).
    Public keys (*.pub) and template env files return False so scaffolding stays unblocked;
    a source module named credentials.py is NOT a secret (the code-extension carve-out).
    Obfuscated targets (var-indirection, base64) are out of scope -- the path is all this sees.
    """
    if not path:
        return False
    low = path.replace("\\", "/").lower()
    norm = "/" + low.strip("/") + "/"
    # 1. Anything under a secret-bearing directory -- regardless of basename.
    if any(seg in norm for seg in _CREDENTIAL_DIR_SEGMENTS):
        return True
    basename = os.path.basename(low)
    # 2. Template/example look-alikes (allowed only OUTSIDE secret dirs, handled above).
    if basename in _CREDENTIAL_ALLOW_BASENAMES:
        return False
    # 3. Public keys are not secret -- unless the pre-.pub stem is itself a key file.
    if basename.endswith(".pub"):
        stem = basename[:-4]
        return any(stem.endswith(ext) for ext in _CREDENTIAL_KEY_EXTS)
    # 4. AWS-style extension-less 'credentials' and 'credentials.<data-ext>', but NOT a
    #    source module named credentials.py / .ts / .go (gated by the normal write gate).
    if basename == "credentials":
        return True
    if basename.startswith("credentials.") and os.path.splitext(basename)[1] not in _CODE_EXTENSIONS:
        return True
    # 5. Credential basename globs.
    return any(fnmatch.fnmatch(basename, glob) for glob in _CREDENTIAL_BASENAME_GLOBS)


def _parse_file_path_from_envelope(envelope: dict) -> str:
    """Extract file_path from a Claude Code hook stdin envelope.

    NotebookEdit uses notebook_path (not file_path); include it so a notebook cell
    edit is gated like any other write instead of falling through the empty-path
    allow (#4 -- closing an unintentional work-gate bypass, not a deliberate
    exemption: the work gate blocks ALL writes before plan approval)."""
    tool_input = envelope.get("tool_input", {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (json.JSONDecodeError, ValueError):
            tool_input = {}
    return (
        tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("notebook_path")
        or ""
    )


def _load_categories(categories_path: str) -> dict:
    """Load gate-categories.json. Returns empty config on error."""
    try:
        with open(categories_path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        # The gate keeps running without its exclusion list, which silently changes
        # what it blocks. Record it rather than degrade invisibly.
        from writ.shared.logging import emit_exception

        emit_exception("session.gates.categories", exc, "", None,
                       categories_path=categories_path)
        return {"exclusions": [], "categories": [], "framework_detection": {}}


def _resolve_categories_path(skill_dir: str) -> str:
    """gate-categories.json under skill_dir when present, else the packaged fallback."""
    path = os.path.join(skill_dir, "bin", "lib", "gate-categories.json") if skill_dir else ""
    if not path or not os.path.isfile(path):
        return _GATE_CATEGORIES_FALLBACK
    return path


def _glob_match(path: str, pattern: str) -> bool:
    """Bash-style glob: * matches any character including /."""
    import re
    regex = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return bool(re.fullmatch(regex, path))


def _matches_any(path: str, patterns: list[str]) -> bool:
    basename = os.path.basename(path)
    for p in patterns:
        if _glob_match(path, p) or _glob_match(basename, p):
            return True
    return False


def _log_gate_denial(session_id: str, cache: dict, gate: str, file_path: str, reason: str) -> None:
    """Log gate_denial, write_attempt, and repeated_denial events. Update denial_counts."""
    mode = cache.get("mode")
    phase = cache.get("current_phase")

    # Increment denial_counts under the per-session lock on the FRESH cache so a
    # stale passed-in snapshot can never clobber mode/current_phase/gates_approved.
    # Mirror the fresh count back onto the caller's dict (A10: _can_write_check
    # reads cache["denial_counts"] after this returns).
    with mutate_cache(session_id) as fresh:
        denial_counts = fresh.get("denial_counts", {})
        denial_counts[gate] = denial_counts.get(gate, 0) + 1
        fresh["denial_counts"] = denial_counts
        count = denial_counts[gate]
    cache["denial_counts"] = dict(denial_counts)

    # The enforcing rule is named in the reason as a [RULE-ID] prefix
    # (e.g. "[ENF-GATE-PLAN] ..."). Surface it as rule_id so the
    # friction analyzer's analyze_rule_effectiveness can attribute the
    # denial to a rule instead of skipping it (it requires e.rule_id).
    _m = re.search(r"\[([A-Z][A-Z0-9-]*)\]", reason or "")
    rule_id = _m.group(1) if _m else None

    _log_friction_event(session_id, mode, "write_attempt",
                        file_path=file_path, result="deny", gate_status=gate, phase=phase)
    _log_friction_event(session_id, mode, "gate_denial",
                        rule_id=rule_id, file_path=file_path, gate=gate,
                        denial_count=count, phase=phase)

    if count > 1:
        _log_friction_event(session_id, mode, "repeated_denial",
                            rule_id=rule_id, gate=gate, denial_count=count,
                            file_path=file_path, phase=phase)


def _validate_root_cause(debug_md_path: str) -> str | None:
    """Presence-only validation of debug.md's '## Root cause' section.

    Returns None if the section exists with a non-empty body before the next
    '## ' heading; else an error string. Mirrors _validate_phase_a's
    regex-presence approach. NOT a truth check -- a regex cannot tell a real
    root cause from a fabricated one (see ENF-PROC-DEBUG-001); Evidence /
    Falsification / Triangulation therefore stay advisory.
    """
    import re
    try:
        with open(debug_md_path) as f:
            content = f.read()
    except OSError:
        return "debug.md not readable"
    heading = re.search(r'^##\s+Root\s+[Cc]ause.*$', content, re.MULTILINE)
    if not heading:
        return "debug.md is missing a '## Root cause' section"
    rest = content[heading.end():]
    nxt = re.search(r'^##\s', rest, re.MULTILINE)
    body = rest[:nxt.start()] if nxt else rest
    if not body.strip():
        return "debug.md '## Root cause' section is empty"
    return None


def _section_body(content: str, heading_pattern: str):
    """Return the body of a '## ...' section (until the next '## '), or None if absent."""
    import re
    h = re.search(heading_pattern, content, re.MULTILINE)
    if not h:
        return None
    rest = content[h.end():]
    nxt = re.search(r'^##\s', rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def _has_real_content(body) -> bool:
    """INV-9: True if a section body has content that is not just blank lines or the
    scaffold's `<...>` placeholder hint lines -- so an unfilled template does not pass."""
    import re
    if not body:
        return False
    for line in body.splitlines():
        s = line.strip()
        if s and not re.match(r'^<.*>$', s):
            return True
    return False


def _validate_evidence_narrowing(debug_md_path: str) -> str | None:
    """INV-9: gate code reading until runtime evidence is recorded.

    Returns None only if BOTH '## Evidence' and '## Narrowing' have real (non-placeholder)
    content; else an error string. Presence-checked, never truth-checked -- a regex cannot
    tell real runtime evidence from a fabricated note (the discipline is ordering, not
    adjudication; see PBK-PROC-DEBUG-001 / DEBUG-MODE-PROPOSAL.md line 126).
    """
    try:
        with open(debug_md_path) as f:
            content = f.read()
    except OSError:
        return "debug.md not readable"
    for name, pat in (("Evidence", r'^##\s+Evidence'), ("Narrowing", r'^##\s+Narrowing')):
        body = _section_body(content, pat)
        if body is None:
            return f"debug.md is missing a '## {name}' section"
        if not _has_real_content(body):
            return f"debug.md '## {name}' needs real runtime evidence before code reading"
    return None


def _check_exempt_write(session_id: str, mode, file_path: str, cache: dict, skill_dir: str) -> dict | None:
    """Categorical write exemptions checked before any mode/gate logic.

    Returns an allow result (and logs it) for skill-infra, global-settings, and
    sub-agent writes; None when none apply so the caller continues. Order matters:
    skill_dir, then settings, then sub-agent -- a sub-agent editing the skill dir
    logs skill_exempt, exactly as the original linear sequence did.
    """
    # Skill infrastructure + global settings are NOT gated (you cannot require gate
    # approval to edit the gate itself), but the allow IS logged so Writ-on-Writ
    # development is observable in the friction log (F3 self-observability). Without
    # this, editing the skill dir -- e.g. developing Writ -- produced zero write
    # telemetry, which is why the repo log's write_attempts were all test-synthetic.
    if skill_dir and file_path.startswith(skill_dir + "/"):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow", gate_status="skill_exempt")
        return {"can_write": True, "reason": None}
    home = os.environ.get("HOME", "")
    if home:
        # Resolve symlinks with realpath (NOT normpath): normpath collapses `..`
        # LEXICALLY, so ~/.claude/<symlinked-dir>/../settings.json normalizes back to
        # ~/.claude/settings.json and would be exempted -- yet the OS follows the
        # symlink and writes an arbitrary file OUTSIDE ~/.claude. realpath follows the
        # symlink first, so both basename and dirname are derived from the TRUE target
        # (one resolution, no double-normalize). A brand-new settings.json under a real
        # ~/.claude still matches: realpath resolves the existing prefix then appends the
        # not-yet-existing final component lexically. A settings.json that is itself a
        # symlink resolves to its target basename and is denied (accepted fail-closed).
        settings_dir = os.path.realpath(os.path.join(home, ".claude"))
        real_path = os.path.realpath(file_path)
        if (
            os.path.basename(real_path) in _EXEMPT_SETTINGS_BASENAMES
            and os.path.dirname(real_path) == settings_dir
        ):
            # Log the ORIGINAL file_path (what was requested), not the resolved target,
            # so telemetry reflects the caller's intent.
            _log_friction_event(session_id, mode, "write_attempt",
                                file_path=file_path, result="allow", gate_status="settings_exempt")
            return {"can_write": True, "reason": None}

    # Sub-agents bypass mode/gate checks. They are workers dispatched by an
    # orchestrator that already passed the human-approval gate; their scope
    # is narrowed by the agent definition + spawn prompt. Gates exist to stop
    # the master from writing code before plan approval, not to re-police
    # workers the orchestrator has already sanctioned. See rules/writ-orchestrator.md.
    if cache.get("is_subagent"):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow",
                            gate_status="subagent_bypass")
        return {"can_write": True, "reason": None}

    return None


def _check_special_files(basename: str, mode, current_phase) -> dict | None:
    """plan.md / capabilities.md special-casing, checked before the no-mode deny.

    Returns a result for the cases the original handled explicitly (pre-mode plan.md,
    capabilities.md in any mode, work-mode plan.md), else None so plan.md in other
    modes (e.g. debug) falls through to that mode's gate. Emits no friction events,
    matching the original. Pure: no IO.
    """
    # plan.md exception: allowed pre-mode
    if basename == "plan.md" and mode is None:
        return {"can_write": True, "reason": None}

    # capabilities.md: always allowed
    if basename == "capabilities.md":
        return {"can_write": True, "reason": None}

    # plan.md in Work mode: allowed during planning/testing, blocked during implementation
    if basename == "plan.md" and mode == "work":
        if current_phase == "implementation":
            return {
                "can_write": False,
                # The old wording sent the reader to invalidate-gate, which records a
                # violation and escalates but deliberately does NOT clear an approval:
                # only the human's approval decides a gate. Advertising it here named an
                # escape that does nothing.
                "reason": "[ENF-GATE-PLAN] plan.md cannot be modified during implementation phase. "
                          "Ask the user before changing the plan: a changed plan re-arms the "
                          "gates and needs one fresh approval before writes resume.",
            }
        return {"can_write": True, "reason": None}

    return None


def _check_debug_gate(session_id: str, mode, file_path: str, basename: str, cache: dict, skill_dir: str) -> dict:
    """Debug mode (Increment 4): block source edits until a root cause is
    established. debug.md, plan.md, and excluded paths (tests, .claude,
    migrations, __init__, conftest) stay writable so the agent can record
    evidence and articulate the cause -- no bootstrap deadlock. Presence-only;
    Evidence/Falsification/Triangulation stay advisory (see ENF-PROC-DEBUG-001).
    """
    categories_path = _resolve_categories_path(skill_dir)
    config = _load_categories(categories_path)
    # Increment 7a: advisory signal -- did the investigation actually run any
    # commands (auto-captured)? Surfaced on the gate event for auditability;
    # does NOT change the allow/deny decision (which stays root-cause presence).
    evidence_backed = any(
        r.get("artifact_type") == "command" for r in cache.get("citation_log", [])
    )
    if basename in ("debug.md", "plan.md") or _matches_any(file_path, config.get("exclusions", [])):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow", gate_status="debug_exempt")
        return {"can_write": True, "reason": None}
    debug_md = _find_debug_md(file_path, session_id)
    if debug_md and _validate_root_cause(debug_md) is None:
        _log_friction_event(session_id, mode, "debug_gate_root_cause_populated",
                            file_path=file_path, result="allow", gate_status="debug_root_cause_ok",
                            evidence_backed=evidence_backed)
        return {"can_write": True, "reason": None}
    # The path names THIS session's scoped file. Naming the project root instead would
    # steer two sessions debugging one project into the same file, which is the bleed the
    # scoped tier exists to stop: recording a root cause there would unblock the other
    # session's source edits too.
    scoped_debug = debug_path(cache.get("project_root") or "", session_id)
    reason = (
        "[DEBUG-GATE-ROOT-CAUSE] Source edits are blocked in debug mode until a root "
        f"cause is established. Create {scoped_debug or 'debug.md at the project root'} "
        "with a populated '## Root cause' section, then edit source. debug.md and test "
        "files are writable now so you can record evidence -- a real command you run is "
        "auto-recorded. Evidence / Falsification / Triangulation are advisory but "
        "recommended (scaffold: templates/debug.md)."
    )
    _log_friction_event(session_id, mode, "debug_gate_source_edit_denied",
                        file_path=file_path, result="deny", gate_status="debug_root_cause_missing",
                        evidence_backed=evidence_backed)
    return {"can_write": False, "reason": reason}


def _check_work_gate(session_id: str, mode, file_path: str, current_phase, cache: dict, skill_dir: str) -> dict:
    """Work mode: two-gate enforcement (phase-a plan approval + test-skeletons).

    A7: check the in-memory gate state FIRST. Once both gates are approved -- the
    dominant steady-state path -- the write is allowed regardless of exclusion, so
    the gate-categories.json disk read (open + json.load, uncached) is skipped
    entirely. The exclusion check is only needed on the NOT-both-approved path,
    where excluded paths (tests, migrations, __init__, .claude) MUST stay writable
    BEFORE approval so test skeletons can be written -- so it runs there, ahead of
    the deny. (Telemetry note: an excluded path written once both gates are approved
    now logs gate_status="all_approved" instead of "excluded" -- same allow
    decision; the only behavioral change from the reorder.)
    """
    # The approved set is the PLAN-PAIRED one, computed by the same function the advance
    # path uses (mode_engine.approved_gates_for_plan). Reading `gates_approved` alone here
    # is what let source writes continue against a plan that had changed since the approval,
    # while an advance was refused for exactly that drift. `granted` keeps the raw set only
    # to tell the two denials apart: never approved reads differently from approved for a
    # plan that no longer exists, and a refusal the user cannot act on is a deadlock.
    granted = set(cache.get("gates_approved", []))
    approved_gates = approved_gates_for_plan(cache, session_id)

    if "phase-a" not in approved_gates or "test-skeletons" not in approved_gates:
        categories_path = _resolve_categories_path(skill_dir)
        config = _load_categories(categories_path)

        if _matches_any(file_path, config.get('exclusions', [])):
            _log_friction_event(session_id, mode, "write_attempt",
                                file_path=file_path, result="allow", gate_status="excluded")
            return {"can_write": True, "reason": None}

        drifted = [gate for gate in ("phase-a", "test-skeletons")
                   if gate in granted and gate not in approved_gates]
        if drifted:
            reason = (
                "[ENF-GATE-DRIFT] ALL writes blocked -- plan.md changed after it was "
                f"approved, so the approval for {', '.join(drifted)} no longer covers it. "
                "DO NOT attempt more writes.\n"
                "Tell the user what changed and say: \"Say approved to proceed.\"\n"
                "One fresh approval re-binds the gates to the current plan. Ticking a "
                "capability box is not a change and never causes this."
            )
            _log_gate_denial(session_id, cache, drifted[0], file_path, reason)
            return {"can_write": False, "reason": reason}

        if "phase-a" not in approved_gates:
            reason = (
                "[ENF-GATE-PLAN] ALL writes blocked -- plan not yet approved. "
                "DO NOT attempt more writes.\n"
                "Present your plan to the user and say: \"Say approved to proceed.\"\n"
                "Wait for the user to say \"approved\" before attempting ANY file writes."
            )
            _log_gate_denial(session_id, cache, "phase-a", file_path, reason)
            return {"can_write": False, "reason": reason}

        # phase-a approved but test-skeletons not (the only remaining case here).
        reason = (
            "[ENF-GATE-TEST] ALL writes blocked -- test skeletons not yet approved. "
            "DO NOT attempt more writes.\n"
            "Write test skeleton files first (test files ARE allowed), "
            "present them to the user, and say: \"Say approved to proceed.\""
        )
        _log_gate_denial(session_id, cache, "test-skeletons", file_path, reason)
        return {"can_write": False, "reason": reason}

    # Both gates approved
    _log_friction_event(session_id, mode, "write_attempt",
                        file_path=file_path, result="allow", gate_status="all_approved",
                        phase=current_phase)
    return {"can_write": True, "reason": None}


def _can_write_check(session_id: str, envelope: dict, skill_dir: str = "", cache: dict | None = None) -> dict:
    """Reusable gate check logic. Returns {"can_write": bool, "reason": str|None}.

    Used by both cmd_can_write (CLI) and /pre-write-check (HTTP endpoint).

    B6h: a thin router. Categorical exemptions, special files, and the per-mode
    gates (debug / work) each live in a dedicated _check_* helper; this function
    only resolves file_path + mode and dispatches. The ordering is identical to the
    former linear sequence: exempt paths -> special files -> no-mode deny -> debug
    gate -> non-work allow -> work gate.

    A10: a caller that has already read the session cache (pre_write_check reads it
    once for the whole request) may pass it as `cache` to avoid a redundant
    _read_cache. The dict is mutated in place on a gate denial (_log_gate_denial),
    so the caller sees the fresh denial_counts. When omitted it is read here,
    preserving every existing caller unchanged.
    """
    file_path = _parse_file_path_from_envelope(envelope)
    if not file_path:
        return {"can_write": True, "reason": None}

    if cache is None:
        cache = _read_cache(session_id)
    mode = cache.get("mode")

    # Credential-path guard (#6): deny writes to secret/credential files in EVERY
    # mode, ahead of every exemption and gate -- even a skill-dir or sub-agent write
    # to a credential path is refused. Path-only; the file is never opened.
    if _is_credential_path(file_path):
        _log_friction_event(session_id, mode, "write_attempt", file_path=file_path,
                            result="deny", gate_status="credential_path")
        return {
            "can_write": False,
            "reason": "[SEC-CREDENTIAL-WRITE] Refusing to write to a credential/secret path "
                      f"('{os.path.basename(file_path)}'). Secret material must not be written or "
                      "overwritten by the agent. If this is a non-secret template, name it "
                      ".env.example / .env.sample / *.pub.",
        }

    exempt = _check_exempt_write(session_id, mode, file_path, cache, skill_dir)
    if exempt is not None:
        return exempt

    basename = os.path.basename(file_path)
    current_phase = cache.get("current_phase")

    special = _check_special_files(basename, mode, current_phase)
    if special is not None:
        return special

    # No mode: deny everything (plan.md handled above). Log the deny -- this is the
    # most security-relevant refusal (writing with no declared mode) and it emitted
    # no telemetry before (audit #5).
    if mode is None:
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="deny", gate_status="no_mode")
        return {
            "can_write": False,
            "reason": "[ENF-GATE-MODE] No mode declared. Set a mode before writing code. "
                      "Modes: conversation, debug, investigate, review, work.",
        }

    if mode == "debug":
        return _check_debug_gate(session_id, mode, file_path, basename, cache, skill_dir)

    # Non-work modes: allow all writes (no gates)
    if mode != "work":
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow", gate_status="no_gates")
        return {"can_write": True, "reason": None}

    return _check_work_gate(session_id, mode, file_path, current_phase, cache, skill_dir)


def _resolve_read_search_dir(tool: str, ti: dict) -> str:
    """Locate the directory to search for debug.md, for Read / Grep / Glob alike.

    Grep and Glob gate on their `path` arg; Read (and any other tool) gates on the
    parent of its `file_path`. Falls back to cwd when neither is present. Pure.
    """
    if tool in ("Grep", "Glob"):
        return ti.get("path") or os.getcwd()
    fp = ti.get("file_path") or ""
    return os.path.dirname(fp) if fp else os.getcwd()


def _classify_runtime_read(target: str, skill_dir: str, reason: str) -> dict:
    """Classify a Read target in the runtime lens (debug.md lacking Evidence+Narrowing).

    Allow debug.md / plan.md / capabilities.md, skill-dir files, excluded paths, and
    any non-code file so evidence-gathering is never blocked; deny only code-extension
    source files. Returns {"can_read": bool, "reason": str|None}.
    """
    if not target:
        return {"can_read": True, "reason": None}  # nothing to gate -> allow
    basename = os.path.basename(target)
    if basename in ("debug.md", "plan.md", "capabilities.md"):
        return {"can_read": True, "reason": None}
    if skill_dir and target.startswith(skill_dir + "/"):
        return {"can_read": True, "reason": None}
    categories_path = _resolve_categories_path(skill_dir)
    try:
        if _matches_any(target, _load_categories(categories_path).get("exclusions", [])):
            return {"can_read": True, "reason": None}
    except Exception as exc:
        # Falling through skips the exclusion check entirely, so an excluded path can
        # be classified as blocked code. Same outcome as before, now visible.
        from writ.shared.logging import emit_exception

        emit_exception("session.gates.read_exclusions", exc, "", None, target=target)
    if os.path.splitext(target)[1].lower() in _CODE_EXTENSIONS:
        return {"can_read": False, "reason": reason}
    return {"can_read": True, "reason": None}  # non-code data/doc


def _can_read_code_check(session_id: str, envelope: dict, skill_dir: str = "") -> dict:
    """INV-9: the runtime-lens read/search gate. Returns {"can_read": bool, "reason": str|None}.

    In the runtime lens (debug, or investigate+source_type=runtime) with debug.md lacking
    Evidence + Narrowing, deny Grep (code search) and Read of source-code files; allow reading
    debug.md / plan.md / logs / non-code / test + excluded paths so evidence-gathering is never
    blocked. Fail-OPEN: any uncertainty or error -> allow, so a gate bug never wedges the agent.
    """
    try:
        cache = _read_cache(session_id)
        if _effective_source_type(cache) != "runtime":
            return {"can_read": True, "reason": None}

        tool = envelope.get("tool_name", "") or ""
        ti = envelope.get("tool_input", {}) or {}
        if not isinstance(ti, dict):
            ti = {}

        # Locate debug.md from the search target so it works for Read and Grep alike.
        search_dir = _resolve_read_search_dir(tool, ti)
        debug_md = _find_debug_md(os.path.join(search_dir, "_"), session_id)

        # Lens open once runtime evidence is recorded.
        if debug_md and _validate_evidence_narrowing(debug_md) is None:
            return {"can_read": True, "reason": None}

        reason = (
            "[DEBUG-EVIDENCE-FIRST] Code search/reading is blocked in the runtime (debug) lens "
            "until debug.md has runtime Evidence + Narrowing. Observe runtime data FIRST (logs, "
            "traces, queries via Bash -- auto-captured), record it in debug.md's '## Evidence' and "
            "'## Narrowing' (the smallest affected unit), then read code. Reading debug.md, logs, "
            "and non-code files is allowed now. (PBK-PROC-DEBUG-001)"
        )

        if tool == "Grep":
            return {"can_read": False, "reason": reason}

        if tool == "Read":
            return _classify_runtime_read(ti.get("file_path") or "", skill_dir, reason)

        # #5: Glob is file enumeration -- classify by its pattern's extension so a
        # source hunt (**/*.py) is blocked premature, but a log/doc/navigation glob
        # (**/*.log, src/**) is allowed. splitext on the pattern yields the extension;
        # no-extension or non-code patterns fall through to allow (fail-open).
        if tool == "Glob":
            return _classify_runtime_read(ti.get("pattern") or "", skill_dir, reason)

        return {"can_read": True, "reason": None}
    except Exception as exc:
        # Fail-open is deliberate (a gate bug must never wedge the agent), but an
        # allow-from-crash is otherwise indistinguishable from a legitimate allow.
        from writ.shared.logging import emit_exception

        emit_exception("session.gates.can_read", exc, session_id, None)
        return {"can_read": True, "reason": None}  # fail-open


def cmd_can_write(session_id: str, skill_dir: str = "") -> None:
    """Decide whether a file write is allowed. Reads tool envelope from stdin.

    Gating rules:
    - Sub-agents (is_subagent=True): allow all writes. Workers are dispatched
      by an orchestrator that already cleared the human-approval gate.
    - No mode (master): deny all except plan.md and capabilities.md
    - conversation/debug/review (master): allow all (no gates)
    - work (master): two-gate enforcement (phase-a + test-skeletons)

    Output: JSON {"decision": "allow"} or {"decision": "deny", "reason": "..."}
    """
    raw = sys.stdin.read()
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        envelope = {}

    result = _can_write_check(session_id, envelope, skill_dir)

    if result["can_write"]:
        json.dump({"decision": "allow"}, sys.stdout)
        sys.stdout.write("\n")
    else:
        json.dump({"decision": "deny", "reason": result["reason"]}, sys.stdout)
        sys.stdout.write("\n")


def cmd_can_read_code(session_id: str, skill_dir: str = "") -> None:
    """INV-9: decide whether a Grep/Read is allowed in the runtime lens. Reads the tool
    envelope from stdin; emits {"decision": "allow"} or {"decision": "deny", "reason": ...}."""
    raw = sys.stdin.read()
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        envelope = {}
    result = _can_read_code_check(session_id, envelope, skill_dir)
    if result["can_read"]:
        json.dump({"decision": "allow"}, sys.stdout)
    else:
        json.dump({"decision": "deny", "reason": result["reason"]}, sys.stdout)
    sys.stdout.write("\n")
