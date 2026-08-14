"""Single source for the gate-token mechanism (H2).

The gate token is written by auto-approve-gate.sh ONLY on genuine user
approval (input the agent cannot forge), so requiring it is what makes the
human the approver. The path, the read semantics (missing -> ""), the
match comparison, and consumption are security-critical and MUST be defined
once: three call sites (server advance-phase, server promote-candidate, CLI
cmd_advance_phase) previously each reimplemented the comparison, and they
had already drifted. Callers keep their own friction events, error
envelopes, and async wrapping; only the security decision lives here.

WHAT THE TOKEN AUTHORIZES IS PART OF THE TOKEN (cycle 1, plan.md finding 4).
The file used to hold a bare secret, while this module's own docstring promised that
one approval authorizes exactly one gated action. A bare secret cannot keep that
promise: whatever gate happened to be pending when the token was spent got the
approval, so an "approved" typed at a plan.md could advance the test-skeletons gate,
or be spent promoting a decision-memory candidate into the canon. The file is now
three lines:

    line 1           the secret, as before
    line 2           the gate this approval authorizes, empty when none was pending
    line 3           locators.plan_md_hash of the plan.md the user was looking at

read_gate_token() returns LINE ONE ONLY, so the non-destructive presence checks in
the advance and promote-candidate routes keep comparing what they always compared.

DECISIONS AND THE ALTERNATIVES THEY WERE CHOSEN OVER:
  * The plan fingerprint, not a fresh nonce. plan_md_hash already exists for the
    mode switch's restore-versus-re-arm decision and normalizes checkbox tick state,
    so ticking a capability box the documented way is not read as a pivot. A nonce
    would prove only that the token is the one that was minted; the fingerprint
    proves the approval still covers the plan it was given for.
  * The gate, not the phase. A phase can host more than one gate over its life;
    the gate is the thing the human actually approved.
  * A pre-cycle token file (one line, no binding) is REFUSED rather than
    grandfathered: tokens live one turn, so the cost of the upgrade is one retyped
    approval, against the alternative of shipping the hole behind a compatibility
    flag that nothing would ever remove.
  * gate= and plan_hash= on claim_gate_token are keyword-only and REQUIRED. A
    default meaning "do not enforce this half" is a fail-open bypass on the function
    that decides whether a human approved an action, so omitting one is a TypeError
    at the call site instead of a silent unguarded claim. A caller with no gate
    pending passes the literal "" that mint_gate_token wrote, and "" is ENFORCED as
    "must be exactly empty"; it is not a skip sentinel.
"""

import os
import secrets
import uuid

# The refusal classes, which are also the friction-event names callers emit. A
# fail-closed gate used to be indistinguishable in the log from an absent one, so each
# reason a claim can be refused for is named separately at the point of refusal.
BINDING_UNBOUND = "gate_token_unbound"
BINDING_GATE_MISMATCH = "gate_token_gate_mismatch"
BINDING_PLAN_DRIFT = "gate_token_plan_drift"


def gate_token_path(session_id: str) -> str:
    # Must match the bash writer (auto-approve-gate.sh) byte-for-byte: it
    # hardcodes /tmp, as do server.py's comment and the explore.html doc. Using
    # tempfile.gettempdir() here would diverge the instant $TMPDIR is set (the
    # reader would look elsewhere than the writer wrote), fail-closing every
    # gate advance. A hardcoded /tmp guarantees writer and reader always agree.
    return os.path.join("/tmp", f"writ-gate-token-{session_id}")


def mint_gate_token(session_id: str, *, gate: str, plan_hash: str, token: str | None = None) -> str:
    """Write the three-line token file and return the token.

    The bash writer (common.sh write_gate_token_file) produces the same bytes for the
    same inputs; the hook mints from bash so a broken writ package cannot cost the
    user their approval, and this is the python side of that one format.

    `token` exists so a test can drive one fixed value through both writers and
    compare bytes. Production callers omit it and get a fresh secret.
    """
    if token is None:
        token = secrets.token_hex(16)
    path = gate_token_path(session_id)
    with open(path, "w") as f:
        f.write(f"{token}\n{gate}\n{plan_hash}\n")
    # The secret sits in a world-readable directory; the bash writer chmods too.
    os.chmod(path, 0o600)
    return token


def read_gate_token(session_id: str) -> str:
    """Return the on-disk gate token, or "" if absent/unreadable (fail-closed).

    LINE ONE ONLY. The advance and promote-candidate routes compare this against a
    caller-supplied token, so binding text leaking onto the returned value would
    fail-close every gate the moment the binding was added.
    """
    try:
        with open(gate_token_path(session_id)) as f:
            return f.readline().strip()
    except FileNotFoundError:
        # No approval outstanding: the normal state on most turns, not a failure.
        return ""
    except OSError as exc:
        # Present but unreadable is anomalous -- a fail-closed gate that should have
        # opened. Distinguished from the absent case so this stays signal, not noise.
        from writ.shared.logging import emit_exception

        emit_exception("session.gate_token.read", exc, session_id, None)
        return ""


def _token_file_lines(session_id: str) -> list[str] | None:
    """The token file split into lines, or None when it is absent/unreadable.

    Non-destructive: this is the read a caller uses to decide whether a claim is even
    worth attempting, so a refusal does not have to consume an approval that
    legitimately authorizes something else.
    """
    try:
        with open(gate_token_path(session_id)) as f:
            return f.read().split("\n")
    except OSError:
        # Absent or unreadable both mean "no binding to trust", which the caller
        # treats as a refusal. read_gate_token already instruments the unreadable case.
        return None


def _binding_refusal(lines: list[str] | None, gate: str, plan_hash: str) -> str:
    """Return the refusal class for these token-file lines, or "" when they authorize.

    Pure, and the ONLY comparison of a binding in the codebase: the destructive claim
    and the non-destructive pre-check below both route through it, because the last
    time this module let two call sites answer the same security question separately
    they drifted.
    """
    if lines is None or len(lines) < 3:
        return BINDING_UNBOUND
    if lines[1].strip() != (gate or ""):
        return BINDING_GATE_MISMATCH
    if lines[2].strip() != (plan_hash or ""):
        return BINDING_PLAN_DRIFT
    return ""


def gate_binding_refusal(session_id: str, *, gate: str, plan_hash: str) -> str:
    """The refusal class for the on-disk token, or "" when it authorizes this gate.

    Non-destructive, so a caller can name WHY it is refusing (and log a distinct
    friction event) without spending the user's approval on a claim it already knows
    will fail.
    """
    return _binding_refusal(_token_file_lines(session_id), gate, plan_hash)


def read_gate_binding(session_id: str) -> tuple[str, str] | None:
    """(bound gate, bound plan fingerprint), or None when the file carries no binding.

    Callers that must treat a pre-binding token file differently from a mismatched
    one need to ask that question without consuming anything.
    """
    lines = _token_file_lines(session_id)
    if lines is None or len(lines) < 3:
        return None
    return lines[1].strip(), lines[2].strip()


def gate_token_valid(token: str, expected: str) -> bool:
    """True only when a non-empty supplied token matches a non-empty expected one."""
    return bool(token) and bool(expected) and token == expected


def consume_gate_token(session_id: str) -> None:
    """Remove the token: one user approval authorizes exactly one gated action."""
    try:
        os.remove(gate_token_path(session_id))
    except FileNotFoundError:
        pass  # Already consumed: the normal idempotent case.
    except OSError as exc:
        # A token that cannot be removed stays claimable, which is the one failure
        # mode that could let a single approval authorize a second action.
        from writ.shared.logging import emit_exception

        emit_exception("session.gate_token.consume", exc, session_id, None)


def _claim_file(session_id: str) -> str | None:
    """Win the rename-mutex and return the claimed file's contents, else None.

    os.rename is atomic on POSIX, so of N concurrent callers renaming the same source
    to distinct temp names, exactly ONE rename succeeds; every other caller's rename
    raises FileNotFoundError (a subclass of OSError) and gets None. The winner reads
    the renamed file and removes it, so the token is spent by the act of claiming it.

    Returns "" (not None) when the claimed file cannot be read: the claim was won and
    the file is gone, it just cannot be matched, which every caller treats as a
    fail-closed refusal.
    """
    src = gate_token_path(session_id)
    claimed = f"{src}.claiming-{uuid.uuid4().hex}"
    try:
        os.rename(src, claimed)
    except OSError:
        # Absent, or another concurrent caller already won the rename.
        return None
    try:
        with open(claimed) as f:
            content = f.read()
    except OSError as exc:
        # The rename above just succeeded, so this file exists and is ours; failing
        # to read it now means the claim is lost to a real I/O fault, not contention.
        from writ.shared.logging import emit_exception

        emit_exception("session.gate_token.claim_read", exc, session_id, None)
        content = ""
    try:
        os.remove(claimed)
    except OSError as exc:
        # Leaves a stray .claiming-* file behind; harmless per call, but it is the
        # only trace that cleanup is failing.
        from writ.shared.logging import emit_exception

        emit_exception("session.gate_token.claim_cleanup", exc, session_id, None,
                       claimed_path=claimed)
    return content


def _claim_token_mutex(session_id: str, supplied_token: str) -> bool:
    """Atomically claim the gate token: exactly one concurrent caller wins.

    The token FILE is the mutual-exclusion primitive (see _claim_file). This makes
    "one user approval authorizes exactly one gated action" hold even under concurrent
    same-token requests (closes the advance-phase double-fire): only the claim winner
    runs the advance + side effects; every loser returns False and does nothing.

    Returns False when the token is absent, already claimed by a concurrent caller, or
    present-but-mismatched (fail-closed, mirrors gate_token_valid).

    Compares the WHOLE file against supplied_token, which is what makes this the
    UNBOUND primitive: it is the pre-binding claim, kept because the mutual-exclusion
    property is what callers need and the binding is a separate question.
    claim_gate_token below does NOT delegate its comparison here, because a
    three-line file never equals a one-line secret.
    """
    content = _claim_file(session_id)
    if content is None:
        return False
    return gate_token_valid(supplied_token, content.strip())


def claim_gate_token(
    session_id: str, supplied_token: str, *, gate: str, plan_hash: str
) -> bool:
    """Claim the token for ONE named gate against ONE plan fingerprint.

    Refuses when the token is absent, already claimed, mismatched, bound to a
    different gate, bound to a plan fingerprint that no longer matches, or carries no
    binding at all (a pre-cycle token file). gate and plan_hash are required: see the
    module docstring on why a default here would be a fail-open bypass.

    The binding is checked BEFORE the claim so a refusal does not consume an approval
    that authorizes a different action, and again from the claimed bytes, which are
    the only bytes that were actually spent.
    """
    if _binding_refusal(_token_file_lines(session_id), gate, plan_hash):
        return False
    content = _claim_file(session_id)
    if content is None:
        return False
    lines = content.split("\n")
    if _binding_refusal(lines, gate, plan_hash):
        return False
    return gate_token_valid(supplied_token, lines[0].strip())
