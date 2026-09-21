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
# The project-write boundary, as pure functions for the same reason role_scope's matcher is
# pure: the write gate runs in the daemon AND in a CLI subprocess, so a predicate that
# needed anything only one of them could reach would allow in one and deny in the other.
from writ.session.project_boundary import (
    KIND_APPROVED,
    KIND_DISPATCH,
    KIND_PRE_APPROVAL,
    boundary_refusal,
    boundary_root,
    declared_absolute_paths,
    in_project_memory_dir,
    in_scratch_zone,
    is_contained,
    resolve_target,
    scratch_zone,
)
# The pure matcher only. The dispatch-time FETCHER in that module is never called from
# here: the write path reads the scope the dispatch already stamped into the session cache,
# so a write costs no graph query and no HTTP call, and the daemon route and the CLI
# fallback cannot reach different verdicts.
from writ.session.role_scope import path_in_scope
# Where a resolved role's name and provenance literals live, so the gate names the
# unobserved cases with the same constants the resolver stamps.
from writ.session.subagent_role import SOURCE_UNRESOLVED, UNKNOWN_ROLE
# One source for what "lazily seeded" means. Duplicating the literal here would be the
# same defect cycle K removed: a constant restated in a second file goes stale silently.
from writ.session.subagent_seed import CACHE_SOURCE_START, is_lazily_seeded

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


def _role_node_id(role: str) -> str:
    """The SubagentRole node id a role name resolves to (writ-test-writer ->
    ROL-TEST-WRITER-001), mirroring the derived convention in
    `node_store.get_subagent_role`'s resolution clause.

    DISPLAY ONLY. It is named in the refusal so the human who has to change a boundary can
    find the file that declares it; nothing keys a decision on this string.
    """
    return "ROL-" + role.replace("writ-", "").upper() + "-001"


def _role_scope_refusal(role: str, patterns: list, file_path: str) -> str:
    """The refusal text for an out-of-scope write by a role that declares a scope.

    IT NAMES NO ESCAPE, because there is none. Advertising one that does nothing is a
    defect this codebase has already paid for once (an earlier plan.md refusal pointed at
    `invalidate-gate`, which cannot clear an approval), and a role boundary is genuinely
    not approvable: no gate, no phase change and no mode opens it. The only lever is a
    human editing the role node, so that is the only lever named.

    The RESOLVED path is disclosed when it differs from the requested one, because that is
    the whole explanation for a symlink or `..` refusal: the raw string looks in scope and
    the target is not.
    """
    resolved = os.path.realpath(file_path)
    where = f"'{file_path}'"
    if resolved != file_path:
        where += f" (which resolves to '{resolved}')"
    if patterns:
        boundary = ("may write only these paths: " + ", ".join(str(p) for p in patterns)
                    + f", and {where} matches none of them")
    else:
        boundary = f"declares no writable paths at all, and {where} is outside it"
    return (
        f"[ENF-ROLE-SCOPE] Write refused: your role ({role}) {boundary}. This is your "
        "role's boundary, not a gate: no approval, no phase change and no other path "
        "opens it. Do the work your role is for and report the rest to the orchestrator. "
        f"The scope is declared on {_role_node_id(role)} in the graph, and only a human "
        "editing that node changes it."
    )


def _check_role_scope_write(session_id: str, mode, file_path: str, cache: dict) -> dict | None:
    """The role's own write boundary, or None when this role has no boundary to apply.

    THE SCOPE IS COMPUTED FROM THE ROLE ALONE. This function reads exactly four cache
    fields -- `agent_type` (the resolved role), `role_source` (where that role came from),
    `cache_source` (how the cache came to exist) and `role_write_scope` (the scope stamped
    from the role's graph node at dispatch) -- plus `is_subagent`. It never reads `mode`,
    `current_phase`, `gates_approved`, `parent_session_id` or `project_root`. `mode` is a
    parameter only because every friction row in this module carries it as a column.

    The parent's approval is a PRECONDITION for the dispatch existing at all: SubagentStart
    fired because a human approved the work that spawned this worker. It is never an INPUT
    to the scope. "The parent's gates, narrowed by the role" is the same words with the
    opposite property -- under it a parent who approves more widens the child -- so
    authority would still inherit, which is the shape this arm exists to remove.

    IT ABSTAINS UNLESS ALL FOUR CONDITIONS HOLD, and every abstention falls through to the
    blanket sub-agent allow below, i.e. to exactly today's decision. ABSENCE IS NOT A
    POLICY: a legacy cache, a lazily seeded one, an unresolved role, a role with no node at
    all (general-purpose and any third-party agent), a role whose node declares nothing,
    and a dispatch whose fetch failed are all MISSING RECORDS, and a missing record must
    never harden into a refusal the user never asked for. The gap is made visible instead,
    by `writ doctor`'s subagent-role-scope-coverage check.

    The role resolution is re-checked HERE rather than inferred from the presence of a
    stamped scope: two independent guards for one property, the same way `_authority_mode`
    and the bypass narrowing are two guards for cycle K's.
    """
    # 1. A sub-agent, and a cache a real dispatch created. `subagent_start` excludes the
    #    lazily seeded cache by construction (that value is `lazy_seed`), and it also
    #    excludes a cache written before `cache_source` existed, which cycle K established
    #    keeps today's authority rather than being retroactively narrowed.
    if not cache.get("is_subagent"):
        return None
    if str(cache.get("cache_source") or "") != CACHE_SOURCE_START:
        return None

    # 2. The role was OBSERVED. An empty or `unknown` agent_type names no role, and an
    #    `unresolved` role_source says the resolver ran and found nothing, so there is
    #    nothing whose boundary this could be.
    role = str(cache.get("agent_type") or "").strip()
    if not role or role == UNKNOWN_ROLE:
        return None
    if str(cache.get("role_source") or "") == SOURCE_UNRESOLVED:
        return None

    # 3. A DECLARED scope, which an empty list is and None is not.
    patterns = cache.get("role_write_scope")
    if not isinstance(patterns, list):
        return None

    if path_in_scope(file_path, patterns):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow",
                            gate_status="role_scope_allow", agent_type=role)
        return {"can_write": True, "reason": None}

    # DELIBERATELY NOT _log_gate_denial. That helper increments `denial_counts`, which
    # feeds the escalation machinery that tells the user which gate to approve, and a role
    # boundary is not approvable. Counting it there would manufacture a remedy that does
    # not exist. One write_attempt row, so the refusal is still countable.
    _log_friction_event(session_id, mode, "write_attempt",
                        file_path=file_path, result="deny",
                        gate_status="role_scope_deny", agent_type=role)
    return {"can_write": False, "reason": _role_scope_refusal(role, patterns, file_path)}


def _check_project_boundary(session_id: str, mode, file_path: str, recorded_root,
                            recorded_zone, kind: str) -> dict | None:
    """The project-write boundary, or None when the write is in bounds or unjudgeable.

    IT CAN ONLY CONVERT AN ALLOW INTO A DENY. Every call site guards an arm that was about
    to allow, so no existing refusal changes its tag and the pre-approval baseline is
    unchanged envelope for envelope: a non-excluded path before approval still reaches
    `[ENF-GATE-PLAN]` because this is never consulted first.

    THE ORDER OF THE FIVE CHECKS IS THE FILE-READ BUDGET. Containment answers the dominant
    case (an in-project write) with one marker walk, two `realpath` calls and one prefix
    comparison, and returns before the scratch zone is resolved or the plan is opened. The
    memory-directory exemption is a pure derivation from the root plus one `realpath`, so it
    too returns before any plan read. The plan is read only for an out-of-project write on
    the approved arm, where `approved_gates_for_plan` has already opened it once this
    request.

    Returns None rather than an allow, so the caller's own allow (and its own friction row,
    `all_approved` or `excluded`) still happens. The deny emits ONE `write_attempt` row and
    deliberately NOT a `_log_gate_denial`: that helper increments `denial_counts`, which
    feeds the escalation that tells the user which pending gate to approve, and the remedy
    here is an edited plan plus a FRESH approval, not the advance of a pending gate. Same
    reasoning, same shape as the role-scope deny above.

    `recorded_zone` TRAVELS WITH `recorded_root`, both out of the same cache, because the
    scratch zone is now a stamped session value rather than something either write-gate
    process resolves for itself. It is resolved AFTER the empty-root abstain so a cache with
    no project pays nothing for it; the cost is one `isabs` plus one `realpath` and no file
    read. An absent zone removes the EXEMPTION only, so containment, the memory dir and the
    declared `## Files` all still judge the write.
    """
    root = boundary_root(recorded_root)
    if not root:
        return None
    zone = scratch_zone(recorded_zone)
    target = resolve_target(file_path, root)
    if is_contained(target, root):
        return None
    if in_scratch_zone(target, root, zone):
        return None
    # THIS PROJECT'S OWN MEMORY DIRECTORY, on all three kinds. `~/.claude/projects/<encoded
    # root>/memory` is the project's sidecar, derived from the root it belongs to, and an
    # agent writes it every session: while planning, after approval, and inside a dispatch
    # alike, which is why this is not restricted to the approved arm. It sits ahead of the
    # declared set because it costs one string derivation and opens no plan.
    if in_project_memory_dir(target, root):
        return None
    if kind == KIND_APPROVED and target in declared_absolute_paths(root, session_id):
        return None
    _log_friction_event(session_id, mode, "write_attempt",
                        file_path=file_path, result="deny",
                        gate_status="project_boundary_deny", boundary_kind=kind)
    return {"can_write": False,
            "reason": boundary_refusal(kind, file_path, target, root, zone)}


def _check_subagent_boundary(session_id: str, mode, file_path: str, cache: dict) -> dict | None:
    """The dispatched sub-agent's half of the boundary, or None to keep today's decision.

    The blanket sub-agent allow below justifies itself by the orchestrator having already
    cleared a human approval gate. That approval was granted FOR A PLAN, IN A PROJECT, so
    the bypass's own justification names the boundary, and granting the child a wider
    surface than the approval it stands on inverts it.

    IT DEFERS TO A DECLARED ROLE SCOPE BY ARM ORDER, NOT BY RE-READING THE SCOPE. A declared
    list, empty included, is decided by `_check_role_scope_write` ABOVE, which returns
    non-None and never reaches here, so `path_in_scope` stays the sole judge and the `None`
    versus `[]` distinction is honored without this function reading that field at all.

    An explicit `isinstance(role_write_scope, list) -> return None` guard was written here
    first and REMOVED, because in the one state where it was not simply redundant it was
    wrong. STATE THE HARNESS WITH THE RESULT: that state was reached with
    `role_scope.fetch_declared_scope` PATCHED to return a list, and it is NOT reachable
    through the shipped corpus. Unpatched, the real seeder stamps `None` for every spelling
    tried (`'unknown '`, `' unknown'`, `'   '`, `'unknown'`), measured, so the reachable
    count is zero. Two independent reasons: the fetcher strips the role itself before the
    request, so `'   '` returns None without a round trip and the padded spellings collapse
    to `'unknown'`, and no `SubagentRole` node is named `unknown` (the corpus declares five,
    all `writ-*`), so that request answers with no `write_scope`.

    What the patched measurement DID establish, and why the guard is gone: on that
    constructed cache the guard deferred to a judge that had already abstained, because
    `subagent_seed._declared_scope` compares the RAW role to `unknown` while the arm above
    compares the STRIPPED role. So NOTHING judged the path and the verdict went from DENY
    (confined to the parent's project) to ALLOW (unbounded), both directions measured. A
    guard whose only non-redundant effect is to remove the last judge is worse than no
    guard, independently of how the input arrived.

    What would make the state reachable: a role node named `unknown`, or the two guards
    otherwise disagreeing about which roles are judgeable. They already disagree up to
    whitespace, which is a latent trap for a future DIRECT caller of `seed_subagent_cache`
    (both real callers pre-strip) and a defect in that comparison, not something a second
    read of the field here can fix. Field-independence is pinned instead, by
    TestBoundaryArmIgnoresRoleWriteScope.

    `cache_source == "subagent_start"` is required, the same first two conditions the
    role-scope arm uses, so a `lazy_seed` cache abstains and cycle K's property holds: a
    lazily seeded cache decides exactly as no cache at all, reason string included.

    The root comes from the PARENT's cache, because a sub-agent cache deliberately does not
    stamp `project_root` (stamping the parent's would make every mode rotation look
    contested). No parent, or a parent with no project recorded, abstains: absence is not a
    policy, and this arm does NOT honor the plan's `## Files` either, because a sub-agent
    may write `plan.md` itself, so honoring the declaration here would be self-grantable.
    """
    if not cache.get("is_subagent"):
        return None
    if str(cache.get("cache_source") or "") != CACHE_SOURCE_START:
        return None
    parent_session_id = str(cache.get("parent_session_id") or "")
    if not parent_session_id:
        return None
    parent_cache = _read_cache(parent_session_id)
    parent_root = parent_cache.get("project_root") or ""
    if not parent_root:
        return None
    # The zone comes out of the SAME parent-cache read as the root, so the two can never
    # describe different parent caches. The child's own cache stamps neither, for the same
    # reason: a sub-agent cache is not a session working the project, and inheriting either
    # would make `rotation._sessions_claiming_project` count it as one.
    parent_zone = parent_cache.get("scratch_zone") or ""
    return _check_project_boundary(session_id, mode, file_path, parent_root, parent_zone,
                                   KIND_DISPATCH)


def _check_subagent_gate_inheritance(session_id: str, mode, file_path: str,
                                     cache: dict, skill_dir: str) -> dict | None:
    """A sub-agent may not write what its orchestrator was just refused.

    The blanket allow in `_check_exempt_write` states its own justification: the
    workers were "dispatched by an orchestrator that already passed the
    human-approval gate". MEASURED 2026-09-21, that premise can be false while the
    bypass still fires. Parent session f053345b sat in work mode at
    `current_phase: testing` with `gates_approved: ['phase-a']`, was refused five
    times by test-skeletons on legal/index.php, GoogleSheets.php and
    contact/api.php, then dispatched sub-agents that wrote 24 files in that project
    under 28 `write_attempt` rows tagged `subagent_bypass`. Three of the refused
    files are in the written set. This turns the stated premise into a condition.

    ENF-SYS-002, because this runs after the authoritative event. The approval is
    decided when the human types the phrase and the hook records it in the parent's
    `gates_approved`. This arm does NOT re-decide that. It asks the parent's own
    checker what the parent may write RIGHT NOW and refuses the child where the
    parent is refused, so it can never contradict a recorded approval: it only
    refuses where none was recorded. Staleness cuts both ways and both are correct:
    a parent that clears the gate after dispatch reads as cleared, and a parent
    whose plan drifts after dispatch reads as blocked, which is exactly the state in
    which the parent itself may no longer write.

    THE PARENT'S OWN CHECKER IS THE JUDGE, not a second predicate for "which gate
    does this path need". A second copy is how two readers of one policy drift
    apart, which this repository has paid for twice: the duplicated count pins, and
    the two registration collectors that agreed only because every matcher block
    happened to hold exactly one command. No recursion, because the parent is not a
    sub-agent and the sub-agent arms abstain on its evaluation.

    ABSTAINS (returns None, keeping today's decision) wherever nothing establishes
    what the parent was allowed: not a sub-agent, a cache that is not
    `subagent_start`, no `parent_session_id`, an unreadable parent cache, or a
    parent NOT IN WORK MODE. That last one is load-bearing rather than defensive.
    Only work mode has gates, and a parent with no mode is refused by its own
    checker with `[ENF-GATE-MODE]`, which would silently convert this arm from
    "inherit the gates" into "deny every dispatched write". Pinned by
    TestParentWithNoGatesIsUnchanged.
    """
    if not cache.get("is_subagent"):
        return None
    if str(cache.get("cache_source") or "") != CACHE_SOURCE_START:
        return None
    parent_session_id = str(cache.get("parent_session_id") or "")
    if not parent_session_id:
        return None
    parent_cache = _read_cache(parent_session_id)
    if not parent_cache:
        return None
    if str(parent_cache.get("mode") or "") != "work":
        return None

    parent_verdict = _can_write_check(
        parent_session_id, {"tool_input": {"file_path": file_path}},
        skill_dir, parent_cache,
    )
    if parent_verdict.get("can_write"):
        return None

    reason = (
        "[ENF-GATE-SUBAGENT] Write refused: the orchestrator that dispatched this "
        "sub-agent cannot write this path itself right now, so the sub-agent cannot "
        "either. The blanket sub-agent allow exists because the orchestrator has "
        "already cleared a human approval gate; here it has not.\n"
        "The orchestrator's own refusal follows, and resolving THAT resolves this:\n"
        + str(parent_verdict.get("reason") or "(the parent recorded no reason)")
    )
    _log_friction_event(session_id, mode, "write_attempt", file_path=file_path,
                        result="deny", gate_status="subagent_gate_inheritance")
    return {"can_write": False, "reason": reason}


def _check_exempt_write(session_id: str, mode, file_path: str, cache: dict, skill_dir: str) -> dict | None:
    """Categorical write exemptions checked before any mode/gate logic.

    Returns an allow result (and logs it) for skill-infra, global-settings, and
    sub-agent writes; None when none apply so the caller continues. Order matters:
    skill_dir, then settings, then the role scope, then sub-agent -- a sub-agent editing
    the skill dir logs skill_exempt, exactly as the original linear sequence did, and a
    role-scoped sub-agent is judged by its role before the blanket allow can grant it
    everything. The role-scope arm is the only one of the four that can DENY; the other
    three either allow or fall through.
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

    # THE ROLE'S OWN BOUNDARY, ahead of the blanket allow so a role that declares one is
    # judged by it, and returning None whenever it has no opinion so the blanket allow
    # stays the fallthrough for every other case. It sits AFTER the two exemptions above
    # on purpose: you cannot require a gate's permission to edit the gate, so on the Writ
    # repo itself the skill-dir exemption still wins and the role scope is inert. That
    # ordering must not change; the enforcement is real when a governed sub-agent writes
    # into an ordinary project.
    role_scoped = _check_role_scope_write(session_id, mode, file_path, cache)
    if role_scoped is not None:
        return role_scoped

    # THE PROJECT THE DISPATCH STANDS ON, between the role's own boundary and the blanket
    # allow. It runs AFTER the role scope so a declared scope stays the sole judge, and
    # BEFORE the blanket allow so an undeclared role is confined instead of unbounded. It
    # returns None for every case it cannot judge, so the blanket allow is still the
    # fallthrough for everything else.
    dispatched = _check_subagent_boundary(session_id, mode, file_path, cache)
    if dispatched is not None:
        return dispatched

    # THE GATES THE DISPATCH STANDS ON, last of the confining arms and immediately
    # before the blanket allow, so a declared role scope and the project boundary
    # both stay the judges where they speak and this only ever narrows what is left.
    inherited = _check_subagent_gate_inheritance(session_id, mode, file_path, cache, skill_dir)
    if inherited is not None:
        return inherited

    # Sub-agents bypass mode/gate checks. They are workers dispatched by an
    # orchestrator that already passed the human-approval gate; their scope
    # is narrowed by the agent definition + spawn prompt. Gates exist to stop
    # the master from writing code before plan approval, not to re-police
    # workers the orchestrator has already sanctioned. See rules/writ-orchestrator.md.
    # NOT for a lazily seeded cache. That cache was created by a hook because
    # SubagentStart never fired, so nothing here establishes that an orchestrator passed a
    # human gate on this agent's behalf, which is the entire justification for the bypass.
    # This is the second of two independent guards; _authority_mode is the first, and both
    # would have to fail for a seeded cache to gain a write.
    if cache.get("is_subagent") and not is_lazily_seeded(cache):
        _log_friction_event(session_id, mode, "write_attempt",
                            file_path=file_path, result="allow",
                            gate_status="subagent_bypass")
        return {"can_write": True, "reason": None}

    return None


def _authority_mode(cache: dict):
    """The mode the WRITE DECISION may use, which is not always the mode in the cache.

    A cache created by a hook running inside a sub-agent (`cache_source: lazy_seed`) exists
    so the agent can be seen and can receive rules. It is not evidence that anyone
    authorized the agent to write, and reading its inherited mode here would turn a missing
    file into a grant. Measured on one envelope before this function existed:

        no cache at all                  -> False  [ENF-GATE-MODE] No mode declared...
        seeded, parent's mode and gates  -> True
        seeded, is_subagent removed      -> True

    The third line is why this is not solved by narrowing the sub-agent bypass alone: the
    work gate allows on its own once a mode and its approved gates are present. Resolving
    the mode as ABSENT reproduces the previous decision on EVERY path rather than on the
    paths someone remembered to enumerate, including the exemptions that run ahead of the
    mode check and therefore still allow.

    Retrieval is unaffected: it reads `cache["mode"]` directly, and rules are not authority.
    """
    if is_lazily_seeded(cache):
        return None
    return cache.get("mode")


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
                # THE MESSAGE NAMES THE ESCAPE, because for a while none existed. Two
                # earlier wordings both sent the reader nowhere: one advertised
                # invalidate-gate, which records a violation and escalates but
                # deliberately does NOT clear an approval; the other said "ask the user",
                # and no reply the user could type acted on the refusal (a live session
                # typed `approved` twice and was told no gate action was needed). The
                # phrase below is the reply that works, so the refusal and the fix are one
                # message. The [ENF-GATE-PLAN] tag stays for log parsing.
                "reason": "[ENF-GATE-PLAN] plan.md cannot be modified during the implementation "
                          "phase. Only the user can re-open planning: tell them what you want to "
                          "change and why, then ask them to reply exactly `replan approved` in "
                          "their own turn. That returns this session to planning, CLEARS both "
                          "approved gates, and keeps every source write blocked until phase-a "
                          "and test-skeletons are approved again against the new plan. Nothing "
                          "you can run does this.",
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

    THE ARM ORDER ON THE NOT-BOTH-APPROVED PATH, in the order the code checks it:
    exclusions allow (bounded by the pre-approval project boundary), then drift denies
    with [ENF-GATE-DRIFT], then the OS SCRATCH ZONE allows with
    gate_status="scratch_zone", then [ENF-GATE-PLAN], then [ENF-GATE-TEST]. The scratch
    arm sits after drift on purpose -- drift is the one refusal here that says "ALL writes
    blocked" and stays strictly stronger than every other arm -- and ahead of both gate
    denials, because the defect it fixes is the whole pre-approval window and not just the
    plan gate. Placing it beside the exclusions arm would have matched how exclusions
    already behave and was rejected for widening the drift state.
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
            # The exclusion allow is BOUNDED because `_matches_any` lets `*` span `/` and
            # matches the raw path, so `*/tests/*` is satisfied by
            # `/any/other/project/tests/x.py`. Left unguarded it would be a one-line bypass
            # of the whole boundary, reachable before any approval.
            bounded = _check_project_boundary(session_id, mode, file_path,
                                              cache.get("project_root"),
                                              cache.get("scratch_zone"), KIND_PRE_APPROVAL)
            if bounded is not None:
                return bounded
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

        # THE SCRATCH ZONE, IN THE PRE-APPROVAL WINDOW. `in_scratch_zone` has always
        # SUPPRESSED the project-boundary refusal for the OS temporary directory, and its
        # docstring states why (the harness instructs every agent to work in a scratchpad
        # there). Suppressing a refusal is not an allow, so before this arm the write fell
        # through to [ENF-GATE-PLAN] below, whose named action, approve the plan, does not
        # unblock writing a scratch file. Measured live in a work-mode session with no
        # gates approved: /tmp/writ-scratch-xyz and /tmp/scratch.py both denied, on both
        # write doors. Recorded as an accepted cost in
        # docs/adr/ADR-project-write-boundary.md and now closed.
        #
        # AFTER THE DRIFT CHECK, ON PURPOSE. Drift is a deliberately loud stop signal
        # ("DO NOT attempt more writes") and stays strictly stronger than every other arm
        # in this function; the measured defect lives entirely in the pre-approval window,
        # so this is the smallest placement that fixes it. Placing it beside the exclusions
        # arm above would have matched how exclusions already behave and was rejected for
        # widening the drift state.
        #
        # THE ROOT IS RESOLVED EXACTLY AS THE BOUNDARY RESOLVES IT: boundary_root, then
        # resolve_target, including _check_project_boundary's own empty-root abstain. A
        # target resolved differently from the boundary's own resolution would let the two
        # disagree about one path, and a relative envelope path with no root would resolve
        # against the process cwd, which is Writ's install dir in the daemon and the
        # project in the CLI fallback, so the two doors would answer differently for one
        # write. No recorded project, no exemption: absence is not a policy and today's
        # decision stands.
        #
        # THE ZONE IS RESOLVED THE SAME WAY THE BOUNDARY RESOLVES IT TOO, out of the cache
        # via `scratch_zone`, and it inherits the same abstain. No stamped zone means no
        # exemption and NEVER a live `tempfile.gettempdir()`: resolving one here is exactly
        # what let the daemon and the CLI fallback answer differently for one path.
        scratch_root = boundary_root(cache.get("project_root"))
        zone = scratch_zone(cache.get("scratch_zone"))
        if scratch_root and zone and in_scratch_zone(
                resolve_target(file_path, scratch_root), scratch_root, zone):
            _log_friction_event(session_id, mode, "write_attempt",
                                file_path=file_path, result="allow",
                                gate_status="scratch_zone", phase=current_phase)
            return {"can_write": True, "reason": None}

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

    # Both gates approved. The approval was granted for a plan, and a plan is for a
    # project, so it authorizes writes to THAT project plus whatever its `## Files` section
    # declares by absolute path. Before this arm was bounded, an approved plan for one repo
    # authorized a write to any absolute path on the filesystem (measured: post-approval,
    # `/etc/passwd-probe.txt` allowed). Checked BEFORE the allow row is emitted, so a
    # refusal leaves exactly one `write_attempt` and not an allow followed by a deny.
    bounded = _check_project_boundary(session_id, mode, file_path,
                                      cache.get("project_root"),
                                      cache.get("scratch_zone"), KIND_APPROVED)
    if bounded is not None:
        return bounded
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
    mode = _authority_mode(cache)

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

    # The plan.md implementation freeze denied and recorded NOTHING, so the one refusal a
    # reader is most likely to ask about after the fact left no row. The record is emitted
    # HERE, at the call site, rather than inside _check_special_files: that helper is pure
    # (no IO, no telemetry) and every other deny in this module logs from the router, so
    # keeping the emit here means one place to look and one function fewer with a side
    # effect. Only the deny is recorded; the two allows it returns are not refusals.
    special = _check_special_files(basename, mode, current_phase)
    if special is not None:
        if not special["can_write"]:
            _log_friction_event(session_id, mode, "write_attempt",
                                file_path=file_path, result="deny",
                                gate_status="plan_frozen", phase=current_phase)
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
            decision = {"can_read": False, "reason": reason}
            target = ti.get("path") or ""
        elif tool == "Read":
            target = ti.get("file_path") or ""
            decision = _classify_runtime_read(target, skill_dir, reason)
        # #5: Glob is file enumeration -- classify by its pattern's extension so a
        # source hunt (**/*.py) is blocked premature, but a log/doc/navigation glob
        # (**/*.log, src/**) is allowed. splitext on the pattern yields the extension;
        # no-extension or non-code patterns fall through to allow (fail-open).
        elif tool == "Glob":
            target = ti.get("pattern") or ""
            decision = _classify_runtime_read(target, skill_dir, reason)
        else:
            decision = {"can_read": True, "reason": None}
            target = ""

        # All three denies emitted NOTHING, so the runtime lens was the one gate whose
        # refusals could not be counted. Recorded from the router rather than from each arm,
        # so _classify_runtime_read stays pure and one call covers Read, Grep and Glob alike.
        # read_denied, NOT read_blocked: the latter is summed into a published token floor by
        # writ/analysis/token_audit.py::attribute_prevented, and a lens deny carries no byte
        # estimate, so reusing that name would inflate the count with zero-token rows.
        # The record must never be able to change the verdict. This call sits inside the
        # function's fail-open handler, which turns ANY exception into can_read: True, so
        # an unlucky raise in the logging path would convert a real deny into an allow:
        # the gate would open because writing down that it closed failed. Its own handler
        # keeps that impossible, and losing a row is strictly better than losing a refusal.
        if not decision["can_read"]:
            try:
                _log_friction_event(session_id, cache.get("mode"), "read_denied",
                                    tool_name=tool, file_path=target,
                                    gate_status="runtime_lens_evidence_missing")
            except Exception:  # noqa: BLE001 - a lost record must not become an allow
                pass
        return decision
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
