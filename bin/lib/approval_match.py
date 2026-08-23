"""Approval-phrase detector for auto-approve-gate.sh (stdlib-only).

Single importable source for the approval predicate so the hook and the tests
bind to the same logic (no inline-python extraction drift). No writ-package
import (matches gate_advance_outcome.py / writ_mode_hint.py load-robustness):
the hook calls it by path even if the package is unimportable.

The caller passes an already-lowercased, already-stripped prompt (the hook's
PROMPT_LOWER and the test wrapper both lower+strip first). is_approval may
.strip() defensively but does not change the matching semantics.

TWO TIERS, AND ONLY ONE OF THEM CAN ADVANCE A GATE. is_approval is the exact-tier
predicate and accepts exactly one word, `approved`: it decides what mints a token and
advances. classify() adds a middle tier for the case that cost a turn on 2026-08-10, "ok
remember we want to fix all our findings, approved", which is a genuine approval that is
not the bare word. The embedded tier ASKS instead of advancing, so recall stays high while
the set of things that can advance a gate is one phrase wide.

The exact tier was narrowed from seventeen phrases plus fuzzy matching plus seven regex
shapes to that single word, by user directive, after a message merely DISCUSSING approval
phrases fired both this predicate and the manual-testing grant. See is_approval.
"""

import re
import sys


def is_approval(prompt: str) -> bool:
    """Return True only for the exact word `approved`.

    ONE PHRASE, BY USER DIRECTIVE. This predicate mints the gate token that advances a
    phase, so its trigger surface is the set of words a user would deliberately choose and
    nothing else. What was deleted, and why each had to go:

      * a seventeen-member phrase set including `ok`, `okay`, `y`, `yes`, `go`, `do it` and
        `continue`. Every one of those is ordinary conversational acknowledgement, and each
        minted a token.
      * a prefix-stripping retry (`ok approved`, `sure proceed`), which turned a sentence
        that merely began with an approval word into an approval.
      * a Levenshtein distance <= 2 pass over approve/proceed/accept for short prompts. It
        accepted the typo `except`, which is a real word with an unrelated meaning.
      * seven regex shapes, including `approved and <short instruction>` and a
        `phase<a-d> approved` form that never matched the hyphenated spelling users type,
        because the pattern's whitespace class cannot cross a hyphen.

    Why the mint deserves this and the claim side did not: a token minted when no phase gate
    is pending carries an EMPTY gate line, and the empty-gate token is exactly what the
    promotion route accepts as a canon-write credential. So a casual `ok` left a durable
    credential in /tmp. Narrowing the trigger removes that without touching the binding or
    the atomic claim, which were already sound.

    Trailing `.`/`!`/`,` are still stripped: a user typing a full stop has not changed their
    mind. Case and surrounding whitespace are the caller's job (the hook passes an
    already-lowered, already-stripped prompt) and are handled defensively here anyway.

    Pure function, no I/O, fail-closed: any internal error returns False so a defect
    degrades to "no approval detected", which is the safe direction.
    """
    try:
        prompt = (prompt or "").strip().lower()
        return re.sub(r"[.!,]+$", "", prompt).strip() == "approved"
    except Exception:
        return False


# The embedded tier's vocabulary, deliberately NARROWER than the exact tier's. The exact
# set includes ok/good/go/yes/continue, and admitting those as embedded signals would make
# "ok good work, now do X" ask the user to confirm a gate on an ordinary instruction. Only
# words whose sole ordinary use IS an approval survive here, matched as whole words so
# "go" inside "going" cannot fire -- that substring behavior is exactly what the deleted
# LOOKS_LIKE_APPROVAL scan in auto-approve-gate.sh did, and every approval_pattern_miss
# row in the friction log on 2026-08-10 was one of its false positives.
_EMBEDDED_APPROVAL_RE = re.compile(r'\b(?:approved|approve|lgtm|ship it|go ahead)\b')

# A negated approval is not an approval. The negator has to be adjacent to the approval
# word (whitespace or a hyphen only), so this catches "not approved" and "unapproved"
# without swallowing a sentence that merely contains "not" somewhere earlier.
_NEGATED_APPROVAL_RE = re.compile(r"\b(?:not|never|isn't|is not|un)\s*-?\s*approv")

# An interrogative lead means the user is ASKING about approval, not giving one. These
# three shapes ("is this approved", "how do i get this approved", plus a trailing question
# mark) are the cases the exact tier already rejects; without the guard the embedded tier
# would turn each of them into a gate-confirmation question every time it was typed.
_INTERROGATIVE_LEAD_RE = re.compile(r'^(?:is|how|why|does|did|can|should|what)\b')

# Beyond this length a prompt is a piece of work, not an approval, however it ends.
_EMBEDDED_MAX_CHARS = 200


def classify(prompt: str) -> str:
    """Return the approval tier: "exact", "embedded", or "none".

    "exact" is precisely what is_approval accepts, so today's mint-and-advance behavior
    is preserved by construction. "embedded" is a strong approval word inside a longer
    sentence: the hook ASKS the user to confirm and advances nothing, because the
    expensive mistake is advancing a gate the user did not mean to approve. "none" is
    everything else, and the hook does nothing at all for it (no directive, no telemetry
    row, no project-root walk, no mode lookup).

    Pure function, no I/O, fail-closed: any internal error returns "none".
    """
    try:
        prompt = (prompt or "").strip()
        if is_approval(prompt):
            return "exact"
        if not prompt or len(prompt) >= _EMBEDDED_MAX_CHARS:
            return "none"
        if prompt.endswith("?"):
            return "none"
        if _INTERROGATIVE_LEAD_RE.match(prompt):
            return "none"
        if _NEGATED_APPROVAL_RE.search(prompt):
            return "none"
        if _EMBEDDED_APPROVAL_RE.search(prompt):
            return "embedded"
        return "none"
    except Exception:
        return "none"


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    sys.exit(0 if is_approval(arg) else 1)
