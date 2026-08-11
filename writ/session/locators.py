"""Project-file locators for the session helper.

POL-6d extracts these pure path-walk helpers into a low-level module shared by mode_engine
(the debug->work handoff), the gates, and the approval workflow. Keeping them here (below all
of those) breaks the would-be mode_engine<->gates dependency cycle. stdlib only; no other
writ imports.
"""

import glob
import hashlib
import os
import re

# The files whose presence marks a project root. The root walk tests `any` of
# these, so order is irrelevant -- this is the single source for the set that
# was inlined across mode_engine / approval_workflow / friction / metrics and
# both walks below. NOTE: only the marker SET is shared; the walk semantics
# (cwd-inclusive vs file-dir-first) deliberately differ per caller.
PROJECT_ROOT_MARKERS = ("composer.json", "package.json", "Cargo.toml", "go.mod", "pyproject.toml", ".git")

# Tiers resolve_project_root reports, in the order it tries them.
ROOT_FROM_EXPLICIT = "explicit"
ROOT_FROM_MARKER = "marker"
ROOT_FROM_CWD = "cwd"
ROOT_FROM_NONE = "none"


def resolve_project_root(explicit: str = "", start: str = "") -> tuple[str, str]:
    """Resolve the project root for a gate decision. Returns (root, tier).

    Tiers, in order: an explicitly supplied root, the nearest marker dir at or above
    `start`, then `start` itself, then nothing.

    The `start`-itself tier exists because Claude Code runs ANYWHERE: a directory with
    no composer.json/package.json/Cargo.toml/go.mod/pyproject.toml/.git resolved to ""
    and the approval gate then refused every advance, so Writ's workflow was unusable
    outside conventionally-marked repos. Marker-first is kept so that working deep
    inside a repo still approves the plan.md at the repo root.

    `start` is REQUIRED for the marker and cwd tiers: this function never consults
    os.getcwd() itself. The daemon's cwd is Writ's own install dir (systemd
    WorkingDirectory), which carries both a .git and a pyproject.toml AND a plan.md, so
    an implicit cwd fallback server-side would validate Writ's own plan for someone
    else's project. Callers that legitimately mean "the user's cwd" (the CLI) pass it
    in; the server passes the cwd from the hook payload.
    """
    if explicit:
        return explicit, ROOT_FROM_EXPLICIT
    # A RELATIVE start is refused, not resolved. os.path.abspath would resolve it against
    # the calling process's cwd -- inside the daemon that is Writ's own install dir, which
    # carries .git, pyproject.toml AND a plan.md, so a caller that sent "." or "sub/dir"
    # would have the gate validate and approve WRIT'S plan for someone else's project.
    # No caller sends a relative cwd today; refusing it here is what makes the "never
    # consults os.getcwd()" guarantee above true of the code and not just of its callers.
    if not start or not os.path.isabs(start):
        return "", ROOT_FROM_NONE
    path = start
    while True:
        if any(os.path.exists(os.path.join(path, m)) for m in PROJECT_ROOT_MARKERS):
            return path, ROOT_FROM_MARKER
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return start, ROOT_FROM_CWD


# ── Session-scoped gate artifacts ───────────────────────────────────────────────
# An approval writes <project_root>/.claude/gates/<session_id>/<gate>.approved. The
# session id used to be the file's CONTENTS and not part of the path, so two Claude Code
# instances working in one repo shared one approval set: A's approval read as B's, and B
# running `mode set` deleted A's files.
#
# THE SESSION ID IS A PATH COMPONENT NOW, so it is validated BEFORE it is ever joined.
# The charset rejects a separator (a `sid` of "../.." would otherwise let an approval
# artifact be written, and later deleted, anywhere on the filesystem), and "." / ".." are
# rejected outright even though the charset admits their characters, because both name a
# directory that is not a session. A rejected id yields the EMPTY STRING rather than an
# exception: writers must create nothing, and readers must answer "no approval". A reader
# that raised would fail OPEN under `set -e` -- the exception aborts the hook before the
# gate check it was trying to run.
#
# bin/lib/common.sh `writ_gate_dir` is the byte-identical bash mirror (five of the readers
# are shell, one of them on the per-prompt path where a python spawn costs ~19.5ms). Two
# implementations of one path is a seam this repo has been bitten by before, so
# tests/test_gate_dir_bash_python_parity.py runs both sides over the same inputs,
# including a rejected id, and compares bytes. CHANGE BOTH OR NEITHER.
_SESSION_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def is_valid_session_component(session_id) -> bool:
    """True when `session_id` is safe to use as a single path component."""
    if not isinstance(session_id, str) or not session_id:
        return False
    if session_id in (".", ".."):
        return False
    return bool(_SESSION_COMPONENT_RE.match(session_id))


def gate_dir(project_root: str, session_id: str) -> str:
    """The gate-artifact directory for ONE session, or "" when there is no valid path.

    "" means "no gate directory": an absent project root or a session id that failed
    `is_valid_session_component`. Callers must treat it as no-approval, never join to it.
    """
    if not project_root or not is_valid_session_component(session_id):
        return ""
    # Trailing separators are stripped so a root passed as "/srv/app/" produces the same
    # bytes as "/srv/app" -- the bash mirror does the same strip, and the parity test
    # compares byte for byte. A root of "/" strips to "" and rejoins as "/.claude/...".
    root = project_root.rstrip("/") or "/"
    return os.path.join(root, ".claude", "gates", session_id)


def gate_artifact_path(project_root: str, session_id: str, gate_name: str) -> str:
    """The `<gate>.approved` artifact path for ONE session, or "" when there is none.

    The gate name is validated with the same component check as the session id: it
    arrives from a CLI argument on the invalidation path (violations.py), so an
    unvalidated name is the same traversal the session id would have been.
    """
    directory = gate_dir(project_root, session_id)
    if not directory or not is_valid_session_component(gate_name):
        return ""
    return os.path.join(directory, f"{gate_name}.approved")


def _find_debug_md(file_path: str) -> str | None:
    """Find debug.md for the project containing file_path.

    Walks up from the file's directory to a project marker, then checks
    debug.md, docs/debug.md, .claude/debug.md at that root. Returns the path or
    None. Distinct from _find_plan_md (different filename, not reused).
    """
    path = os.path.dirname(os.path.abspath(file_path))
    root = None
    while True:
        if any(os.path.exists(os.path.join(path, m)) for m in PROJECT_ROOT_MARKERS):
            root = path
            break
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    if root is None:
        return None
    for rel in ("debug.md", os.path.join("docs", "debug.md"), os.path.join(".claude", "debug.md")):
        candidate = os.path.join(root, rel)
        if os.path.isfile(candidate):
            return candidate
    return None


def _is_own_project(candidate_dir: str, project_root: str) -> bool:
    """True when candidate_dir is a project in its own right, not a module of project_root.

    A monorepo module (app/code/Vendor/Module, src/pkg) carries no marker of its own, so it
    stays eligible. A sibling checkout with its own .git/package.json/pyproject.toml is a
    different project and its plan must never satisfy this project's gate. The root itself
    is never "its own project" for this purpose -- it is the project.
    """
    if os.path.normpath(candidate_dir) == os.path.normpath(project_root):
        return False
    return any(os.path.exists(os.path.join(candidate_dir, m)) for m in PROJECT_ROOT_MARKERS)


def _find_plan_md(project_root: str) -> str | None:
    """Find the plan.md the approval gate should validate.

    The root plan.md WINS when it exists. That is what the old docstring claimed, but a
    single mtime sort across every candidate meant a more recently touched plan one level
    down beat it -- so the gate could approve a plan the user was not looking at.

    Only when there is no root plan.md do the module globs apply (monorepo layouts keep a
    plan per module), newest first. A candidate whose OWN directory carries a project
    marker is dropped: that is a separate project's plan, not a module of this one. Without
    that filter, a root that resolved high (a $HOME with a .git, say) let `*/plan.md` reach
    into unrelated sibling projects and satisfy this project's gate with their plan.
    """
    root_plan = os.path.join(project_root, 'plan.md')
    if os.path.isfile(root_plan):
        return root_plan

    candidates = glob.glob(os.path.join(project_root, 'app/code/*/*/plan.md'))
    candidates += glob.glob(os.path.join(project_root, 'src/*/plan.md'))
    candidates += glob.glob(os.path.join(project_root, '*/plan.md'))
    found = [
        c for c in candidates
        if os.path.isfile(c) and not _is_own_project(os.path.dirname(c), project_root)
    ]
    if not found:
        return None
    found.sort(key=os.path.getmtime, reverse=True)
    return found[0]


# The fingerprint of a plan.md that EXISTS but could not be read. It is not a hash and is
# deliberately not hex, so it can never collide with a real 12-char digest. Absent and
# unreadable have to be two different answers: None == None compares equal, so folding
# unreadable into None made an unreadable plan at both ends RESTORE the approved gates,
# even though its bytes may have changed while the session was away. Callers treat this
# value as never-equal (see _mode_switch), which re-arms instead.
PLAN_HASH_UNREADABLE = "unreadable"

# A markdown task-list marker that is TICKED, and nothing else: optional indent, a bullet,
# then [x] or [X]. Matched on bytes because the fingerprint hashes bytes. The bullet and the
# brackets are kept in the replacement, so the line still occupies the same shape it did:
# only the character INSIDE the box is rewritten, and adding or deleting a whole checkbox
# line still changes the digest.
_TICKED_CHECKBOX_RE = re.compile(rb"(?m)^([ \t]*[-*+][ \t]+)\[[xX]\]")


def _untick_checkboxes(raw: bytes) -> bytes:
    """Rewrite every ticked checkbox marker to its unticked spelling, and nothing else."""
    return _TICKED_CHECKBOX_RE.sub(rb"\1[ ]", raw)


def plan_md_hash(project_root: str | None) -> str | None:
    """Fingerprint the plan.md the approval gate would validate.

    Returns the digest, None when there is no plan.md at all, or PLAN_HASH_UNREADABLE when
    one exists but cannot be read.

    SAME ALGORITHM AS bin/lib/validate-rules-helper.py's prior_plan_hash (md5, truncated to
    the first 12 hex chars), DIFFERENT INPUT, so the two digests are NOT interchangeable:
    that one hashes plan.md's bytes verbatim, this one hashes them after the checkbox
    normalization described below. For any plan.md containing a ticked box the two values
    differ for identical input, and neither is a substitute for the other. They answer
    different questions and nothing compares them: prior_plan_hash records the exact bytes a
    plan had when a rule violation invalidated its gate, which is evidence about one
    specific document and must stay byte-exact to be worth anything; this one asks whether
    the work a paused session was approved for is still the same work. Do not "unify" them.

    CHECKBOX TICK STATE IS NORMALIZED AWAY before hashing: every `- [x]` and `- [X]` marker
    is hashed as `- [ ]`. templates/plan-template.md tells the author the capability boxes
    "are ticked off after the implementation proves them", so finishing a cycle the
    documented way edits plan.md, and without this the return path read that edit as a
    pivot and demanded a fresh approval of a plan whose substance never changed. Only the
    character inside the box is rewritten. Everything else is hashed verbatim: no whitespace
    stripping, no section parsing, no other markdown touched. So a reworded sentence, a
    reordered heading, or a checkbox line ADDED or REMOVED all still change the digest,
    because which capabilities the plan claims is substance; whether they are ticked yet is
    progress against it.

    The mode switch uses this to decide restore-vs-re-arm on the way back into work. Equal
    means the same work, so the approved gates survive the detour; different means the plan
    pivoted while the session was away, so the old approvals no longer cover it. Absent at
    both ends is genuinely equal (no plan means nothing pivoted). Unreadable is NOT: the
    caller forces a re-arm on it, because a needless re-approval is the safe direction to
    fail when the bytes cannot be checked.
    """
    if not project_root:
        return None
    path = _find_plan_md(project_root)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.md5(_untick_checkboxes(f.read())).hexdigest()[:12]
    except OSError:
        return PLAN_HASH_UNREADABLE
