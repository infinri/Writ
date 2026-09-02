"""The project-write boundary: is this path inside the project the approval was granted for?

A PURE PREDICATE MODULE, mirroring `writ/session/role_scope.py`'s matcher half. Nothing
here reads a session cache, opens a socket, or logs; `writ/session/gates.py` imports these
functions and owns every decision and every friction row. The reason is the same one that
put `path_in_scope` here rather than behind a query: the write gate runs in TWO processes,
the daemon route (`/pre-write-check`, `/session/{sid}/can-write`) and the CLI subprocess the
Bash write gate shells out to when the daemon is unreachable, so anything the predicate
needed at write time that only one of them could reach would allow in one and deny in the
other, and the verdict would depend on which process happened to run.

WHAT MAKES THE ESCAPE SAFE IS NOT IN THIS FILE. `declared_absolute_paths` reads the plan's
`## Files` section, and a plan bullet can only widen the boundary because adding one CHANGES
plan.md's bytes: `locators.plan_md_hash` fingerprints them, `mode_engine.approved_gates_for_plan`
counts an approval only for the plan it was granted against, so both work gates drop and
`[ENF-GATE-DRIFT]` blocks every write until the user says `approved` again. The escape is
therefore not self-grantable, mechanically, and that property lives in the gate binding
rather than in any check below.

NO GLOBS. Containment is a resolved-prefix comparison and the declared set is compared by
equality, because `*` spans `/` in this repo's glob dialect and matches the RAW path, so a
directory-shaped pattern exempts any depth and `..` walks out of it. That defect has been
paid for once already (`docs/reference/session-and-gates.md`'s glob caution); it cannot
recur through this module.
"""
from __future__ import annotations

import os
import tempfile

from writ.session import locators
from writ.session.locators import ROOT_FROM_NONE, resolve_project_root

# The three authorities a refusal can be issued under. One parameter, three values, no
# booleans: `kind` selects both whether the plan's `## Files` declaration is honored (only
# `approved`, because before approval and inside a dispatch the declaration is either not
# yet binding or not the child's to change) and which action the refusal names.
KIND_APPROVED = "approved"
KIND_PRE_APPROVAL = "pre_approval"
KIND_DISPATCH = "dispatch"

# The refusal TAG, for log parsing, matching the `[ENF-ROLE-SCOPE]` precedent. No corpus
# node is authored for it, so it adds no rule-id drift.
TAG = "ENF-PROJECT-BOUNDARY"


def boundary_root(recorded: str) -> str:
    """The resolved project root a write is judged against, or "" to abstain.

    `recorded` is the session cache's `project_root`, which `mode_engine._mode_set` stamps
    as `os.getcwd()` with NO marker walk, so setting a mode from a subdirectory records the
    subdirectory. The walk runs here, through the same `resolve_project_root` the approval
    path uses, and it can only move UP to a marker, so this can only ever WIDEN the
    boundary and never narrow it.

    "" means abstain, and the caller must keep today's decision. An empty or relative
    recorded root reaches `ROOT_FROM_NONE` (that function refuses a relative `start` rather
    than resolving it against the calling process's cwd, which inside the daemon is Writ's
    own install dir). ABSENCE IS NOT A POLICY: a cache with no project recorded must not
    harden into a refusal nobody asked for.
    """
    root, tier = resolve_project_root(explicit="", start=str(recorded or ""))
    if tier == ROOT_FROM_NONE or not root:
        return ""
    return os.path.realpath(root)


def resolve_target(file_path: str, root: str) -> str:
    """The write target as an absolute, symlink-resolved path.

    A RELATIVE path joins `root`, never `os.getcwd()`. The gate is evaluated in two
    processes and the daemon's cwd is Writ's own install dir (systemd WorkingDirectory),
    which carries `.git`, `pyproject.toml` AND a `plan.md`, so resolving against the
    process cwd would give the daemon and the CLI two different answers for one write.

    `realpath`, NOT `normpath`. normpath collapses `..` LEXICALLY, so
    `<root>/<symlink-to-outside>/../x.py` normalizes back to `<root>/x.py` and would be
    judged in bounds, while the OS follows the symlink and writes outside the project.
    That one case is the whole reason `gates.py`'s settings exemption and
    `role_scope.path_in_scope` both make the same choice.
    """
    expanded = os.path.expanduser(file_path)
    if not os.path.isabs(expanded):
        expanded = os.path.join(root, expanded)
    return os.path.realpath(expanded)


def is_contained(target: str, root: str) -> bool:
    """True when the resolved `target` is `root` itself or sits under it.

    The separator is appended before the prefix comparison, so `<root>-evil/x.py` is a
    DIFFERENT project and not a deeper path in this one. A `root` of `/` strips to the empty
    string and contains every absolute path.
    """
    if not target or not root:
        return False
    base = root.rstrip(os.sep)
    if not base:
        return target.startswith(os.sep)
    return target == base or target.startswith(base + os.sep)


def in_scratch_zone(target: str, root: str) -> bool:
    """True when `target` is ephemeral scratch storage that needs no declaration.

    The zone is the OS-DESIGNATED temporary directory, read through `tempfile.gettempdir()`
    at call time, because the mechanism is "storage the OS designates as ephemeral" and not
    "a directory with a particular name". The harness instructs every agent to work in a
    scratchpad there, so this is the one zone the predicate must exempt itself.

    THE EXEMPTION IS SKIPPED WHEN THE PROJECT ROOT IS ITSELF INSIDE THE ZONE. A project
    living in the temp directory would otherwise exempt every sibling checkout beside it,
    which is the whole boundary gone for that project. It is also what keeps the predicate's
    own test file honest: pytest's `tmp_path` is inside the temp directory, so a fixture
    that roots a project there has the zone inert by construction and its "outside" sibling
    denies on containment alone rather than passing for the wrong reason.
    """
    zone = os.path.realpath(tempfile.gettempdir())
    if is_contained(root, zone):
        return False
    return is_contained(target, zone)


def declared_absolute_paths(root: str, session_id: str | None = None) -> set[str]:
    """The out-of-project paths the plan's `## Files` section declares, resolved.

    SECTION-SCOPED AND FULLY-ANNOTATED ONLY, with the same two regexes the approval
    validator applies (`approval_workflow._FILES_LINE_RE` and `_FILES_BOLD_LINE_RE`),
    imported rather than restated so the boundary can never widen to a bullet shape the
    gate would have rejected. `plan_harvest._extract_files` is deliberately NOT reused: it
    also scans prose bullets anywhere in the document, so an absolute path merely MENTIONED
    in `## Analysis` would widen the boundary, and a reason-less bullet (which phase-a
    refuses) would too.

    Only ABSOLUTE and `~`-prefixed bullets are admitted. `templates/plan-template.md`
    documents `## Files` paths as relative to the repo root, so a relative bullet resolves
    inside the boundary and can widen nothing; dropping it here makes that structural
    instead of incidental.

    `_find_plan_md` is called THROUGH the module rather than through a bound name so the
    plan resolution stays one patchable seam, which is how the write path's file-read budget
    is measured rather than asserted. This function is reached only after containment and
    the scratch zone have both failed, so the common in-project write opens no plan.

    The two lazy imports are the pattern `role_scope.fetch_declared_scope` already uses for
    its http client: `gates.py` imports this module on the write path, and neither
    `approval_workflow` (which pulls in the gate-token machinery and a path-loaded
    `approval_match`) nor `gates` itself should be paid for by a write that never parses a
    plan. Importing `gates` at module scope would also close an import cycle.
    """
    from writ.session.approval_workflow import _FILES_BOLD_LINE_RE, _FILES_LINE_RE
    from writ.session.gates import _section_body

    if not root:
        return set()
    plan_md = locators._find_plan_md(root, session_id or None)
    if not plan_md:
        return set()
    try:
        with open(plan_md) as f:
            content = f.read()
    except OSError:
        return set()
    body = _section_body(content, r'^##\s+Files')
    if not body:
        return set()

    declared: set[str] = set()
    for line in body.splitlines():
        stripped = line.strip()
        full = _FILES_LINE_RE.match(stripped)
        if full:
            path = stripped.split("`")[1]
        else:
            bold = _FILES_BOLD_LINE_RE.match(stripped)
            if not bold:
                continue
            path = bold.group(2)
        path = path.strip()
        if not path.startswith("~") and not os.path.isabs(path):
            continue
        declared.add(os.path.realpath(os.path.expanduser(path)))
    return declared


# The declaration every refusal points at. It names the SHAPE by reference to the template
# rather than by restating the bullet grammar, so one artifact teaches the format.
_DECLARE = (
    "add a bullet to plan.md's `## Files` section naming the ABSOLUTE path in the same "
    "shape as every other bullet there (the path, its change type, then a reason; see "
    "templates/plan-template.md), tell the user what the path is for, and ask them to "
    "reply exactly `approved` in their own turn"
)

# Stated in the refusal itself, because a reader who does not know this reads the escape as
# something they can take unilaterally, tries it, and is then blocked by a DIFFERENT tag.
_REARM = (
    "Editing plan.md re-arms both work gates, so the bullet authorizes nothing until that "
    "fresh approval binds the new plan: you cannot widen this by yourself."
)

_SCRATCH = "Paths under the OS temporary directory need no declaration."


def _where(file_path: str, target: str) -> str:
    """The requested path, plus the resolved one when they differ.

    The resolved path IS the explanation for a symlink or `..` refusal: the raw string looks
    in bounds and the target is not. Same disclosure `_role_scope_refusal` makes, for the
    same reason.
    """
    where = f"'{file_path}'"
    if target != file_path:
        where += f" (which resolves to '{target}')"
    return where


def boundary_refusal(kind: str, file_path: str, target: str, root: str) -> str:
    """The refusal text for one out-of-project write.

    EVERY VARIANT NAMES THE ACTION THAT UNBLOCKS IT, in the string itself, which is the only
    place the reader is looking. A refusal that names no action is a deadlock this repo has
    shipped once already: an earlier plan.md refusal advertised `invalidate-gate`, which
    cannot clear an approval, and a session that typed `approved` twice was told no gate
    action was needed. The three actions are genuinely different, which is why this is three
    texts and not one with a placeholder: before approval the declaration is not yet
    binding, after approval it is one edit plus one word from the user, and inside a
    dispatch the child holds neither lever.
    """
    where = _where(file_path, target)
    if kind == KIND_DISPATCH:
        return (
            f"[{TAG}] Write refused: {where} is outside the project you were dispatched "
            f"for ('{root}'). A sub-agent can neither amend the approved plan nor ask the "
            "user, so there is nothing here for you to approve or retry. Stop and report "
            "to the orchestrator which path you need and why; the orchestrator can declare "
            f"it in plan.md's `## Files` and get the user's approval. {_SCRATCH}"
        )
    if kind == KIND_PRE_APPROVAL:
        return (
            f"[{TAG}] Write refused: {where} is outside this project ('{root}'). Excluded "
            "paths INSIDE the project (tests, migrations, __init__.py, .claude) are "
            "writable now, so this project's test skeletons are unaffected. To write "
            f"outside the project, {_DECLARE}. That declaration takes effect once the plan "
            f"is approved, not before. {_SCRATCH}"
        )
    return (
        f"[{TAG}] Write refused: {where} is outside this project ('{root}'), and the "
        f"approved plan's `## Files` section does not declare it. To write it, {_DECLARE}. "
        f"{_REARM} {_SCRATCH}"
    )
