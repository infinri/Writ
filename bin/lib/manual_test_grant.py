"""Manual-testing grant: the user's explicit concession to verify by hand.

ENF-PROC-TDD-001 demands an assertion-bearing test before production code. Some
code has no runnable harness (a Knockout view component in a project with no
node_modules, a template, a layout file), so the honest alternative is the user
testing it by hand. This module is the single source of truth for that grant:
the hook and the gate both bind here so the predicate cannot drift.

The grant lives in its OWN file, writ-grant-<session>.json, not inside the shared
session state. Many hooks fire on one UserPromptSubmit event and each does a full
read-modify-write of the session document, so a key added there is silently lost
to whichever hook writes last. A dedicated file has no such race.

Trust model, stated plainly:

  * A grant can only be MINTED from UserPromptSubmit, whose payload is the
    user's literal typed text. The assistant has no tool that fires that hook,
    so it cannot mint a grant by asking for one.
  * The phrases are exact and specific. Fuzzy matching is deliberately absent:
    "ok", "go", "proceed" must never open a test-first gate the way they open
    an approval prompt.
  * Grants are time-boxed and record every file they admit, so the audit trail
    shows what was accepted on manual-test faith.
  * The grant file sits in the session cache directory, which writ-state-write-gate.sh
    denies for Write/Edit and writ-bash-write-gate.sh denies for Bash.

What this does NOT defend against: an agent editing these hook scripts. No
local hook can prevent that; review of the writ repo is the control there.
"""

import hashlib
import json
import os
import re
import sys
import tempfile
import time

# Grant lives for one focused stretch of work, not the whole session.
GRANT_TTL_SECONDS = 1800

# Exact clauses only. Each names BOTH the manual nature and the concession, so
# none of them can appear by accident in ordinary conversation.
GRANT_PHRASES = (
    'manual testing approved',
    'manual test approved',
    'manual tests approved',
    'manual verification approved',
    'approve manual testing',
    'approved for manual testing',
    'i will test manually',
    "i'll test manually",
    'i will test it manually',
    "i'll test it manually",
    'i will test this manually',
    "i'll test this manually",
)

_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_CACHE_DIR = os.path.join(_SKILL_ROOT, 'var', 'session')


def cache_dir():
    """Resolved at call time so WRIT_CACHE_DIR overrides work in tests."""
    return os.environ.get('WRIT_CACHE_DIR', _DEFAULT_CACHE_DIR)


def grant_path(session_id):
    return os.path.join(cache_dir(), 'writ-grant-%s.json' % session_id)


def is_grant_phrase(prompt):
    """True when the user's own words concede manual testing.

    Pure, fail-closed: any internal error returns False, so a defect here
    degrades to "no grant", which keeps the gate shut.
    """
    try:
        text = (prompt or '').lower()
        # Collapse whitespace so a line-wrapped sentence still matches.
        text = re.sub(r'\s+', ' ', text).strip()
        return any(phrase in text for phrase in GRANT_PHRASES)
    except Exception:
        return False


def _read(session_id):
    try:
        with open(grant_path(session_id)) as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write(session_id, grant):
    """Atomic replace so a crash cannot leave a truncated grant."""
    directory = cache_dir()
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=directory, prefix='writ-grant-%s.json.' % session_id, suffix='.tmp'
        )
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(grant, handle)
            os.chmod(tmp, 0o600)
            os.replace(tmp, grant_path(session_id))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def mint(session_id, prompt, now=None):
    """Record a grant. Only ever called from the UserPromptSubmit hook.

    Returns the grant dict on success, None on failure.
    """
    if not session_id or not is_grant_phrase(prompt):
        return None

    now = int(now if now is not None else time.time())
    grant = {
        'granted_at': now,
        'expires_at': now + GRANT_TTL_SECONDS,
        # Ties the grant to the turn that produced it. A grant whose hash matches
        # no user turn in the transcript is forged.
        'prompt_sha256': hashlib.sha256((prompt or '').encode('utf-8')).hexdigest(),
        'source': 'user_prompt',
        'session_id': session_id,
        'admitted': [],
    }
    return grant if _write(session_id, grant) else None


def active(session_id, now=None):
    """Return the live grant, or None when absent, expired or malformed."""
    grant = _read(session_id)
    if not isinstance(grant, dict):
        return None
    if grant.get('source') != 'user_prompt':
        return None
    # A grant is bound to the session it was minted for; a copied file is not valid.
    if grant.get('session_id') != session_id:
        return None
    try:
        expires_at = int(grant.get('expires_at', 0))
    except (TypeError, ValueError):
        return None
    now = int(now if now is not None else time.time())
    return grant if expires_at > now else None


def inherit(parent_session_id, child_session_id, now=None):
    """Copy a live parent grant to a sub-agent session (SubagentStart).

    Same contract as gates_approved inheritance in writ-subagent-start.sh: the
    user's concession was given to the orchestrating session, and a dispatched
    worker acts on its behalf, so the concession is not re-typed per worker.
    The child keeps the parent's expiry (remaining TTL, never refreshed), gets
    its own empty admitted list, and records inherited_from for the audit trail.

    Returns the child grant dict, or None when there is no live parent grant.
    """
    if not parent_session_id or not child_session_id \
            or parent_session_id == child_session_id:
        return None
    grant = active(parent_session_id, now=now)
    if grant is None:
        return None
    child = dict(grant)
    child['session_id'] = child_session_id
    child['inherited_from'] = parent_session_id
    child['admitted'] = []
    return child if _write(child_session_id, child) else None


def admit(session_id, file_path, now=None):
    """Consume the grant for one file, recording it for the audit trail.

    Returns True when a live grant admitted the file.
    """
    grant = active(session_id, now=now)
    if grant is None:
        return False

    admitted = grant.setdefault('admitted', [])
    if isinstance(admitted, list) and file_path not in admitted:
        admitted.append(file_path)
    _write(session_id, grant)
    return True


def _cli():
    """is-phrase <text> | mint <sid> <text> | active <sid> | admit <sid> <path>
    | inherit <parent-sid> <child-sid> | path <sid>

    Exit 0 means yes/success so shell hooks can branch on the status alone.
    `path` prints where this process resolves the grant file, so the minter can
    record the actual store in the audit row -- a second writ checkout resolving
    a different var/session is then visible in the log instead of a mystery.
    """
    args = sys.argv[1:]
    if not args:
        return 2
    command = args[0]

    if command == 'is-phrase':
        return 0 if is_grant_phrase(args[1] if len(args) > 1 else '') else 1
    if command == 'path':
        print(grant_path(args[1] if len(args) > 1 else ''))
        return 0
    if command == 'mint':
        return 0 if mint(args[1], args[2] if len(args) > 2 else '') else 1
    if command == 'active':
        grant = active(args[1])
        if grant is None:
            return 1
        remaining = max(0, int(grant.get('expires_at', 0)) - int(time.time()))
        print(remaining)
        return 0
    if command == 'admit':
        return 0 if admit(args[1], args[2] if len(args) > 2 else '') else 1
    if command == 'inherit':
        return 0 if inherit(args[1], args[2] if len(args) > 2 else '') else 1
    return 2


if __name__ == '__main__':
    sys.exit(_cli())
