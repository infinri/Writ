"""Approval-phrase detector for auto-approve-gate.sh (stdlib-only).

Single importable source for the approval predicate so the hook and the tests
bind to the same logic (no inline-python extraction drift). No writ-package
import (matches gate_advance_outcome.py / writ_mode_hint.py load-robustness):
the hook calls it by path even if the package is unimportable.

The caller passes an already-lowercased, already-stripped prompt (the hook's
PROMPT_LOWER and the test wrapper both lower+strip first). is_approval may
.strip() defensively but does not change the matching semantics.

TWO TIERS, AND ONLY ONE OF THEM CAN ADVANCE A GATE (cycle 1). is_approval stays the
exact-tier predicate, unchanged: it decides what mints a token and advances. classify()
adds a middle tier for the case that cost a turn on 2026-08-10, "ok remember we want to
fix all our findings, approved", which is a genuine approval that hits none of the
anchored patterns below. The embedded tier ASKS instead of advancing, so recall goes up
while the set of things that can advance a gate does not widen.
"""

import re
import sys


def _levenshtein(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def is_approval(prompt: str) -> bool:
    """Return True if the (lowercased) prompt is a human approval signal.

    Pure function, no I/O, fail-closed: any internal error returns False so a
    hook defect degrades to "no approval detected" (the safe default).
    """
    try:
        prompt = (prompt or "").strip()

        exact = {
            'approved', 'approve', 'lgtm', 'proceed', 'go ahead',
            'looks good', 'ship it', 'yes', 'yep', 'y', 'ok', 'okay',
            'go', 'do it', 'continue', 'accepted', 'accept',
        }

        clean = re.sub(r'[.!,]+$', '', prompt.strip())

        if clean in exact:
            return True

        # Strip common prefix words and re-check exact match
        prefixes = ('ok ', 'okay ', 'sure ', 'yeah ', 'yes ', 'yep ', 'alright ')
        stripped = clean
        for p in prefixes:
            if clean.startswith(p):
                stripped = re.sub(r'^' + re.escape(p) + r'[,]?\s*', '', clean)
                break
        if stripped != clean and stripped in exact:
            return True

        fuzzy_targets = ['approved', 'approve', 'proceed', 'accepted', 'accept']
        if len(clean) <= 12:
            for target in fuzzy_targets:
                if _levenshtein(clean, target) <= 2:
                    return True

        if len(prompt) < 120:
            approval_words = r'(?:approved?|proceed|go ahead|continue|accept(?:ed)?|lgtm|looks? good|ship it)'
            prefix_words = r'(?:ok|okay|sure|yeah|yes|yep|alright)'
            patterns = [
                r'^(?:yes|yep|yeah),?\s*' + approval_words,
                r'^' + approval_words + r'\s*[.!]*$',
                r'^(?:phase\s*[a-d]|test.skeletons?)\s*(?:approved?|lgtm)\s*[.!]*$',
                r'^(?:approve|create)\s+(?:phase|gate)',
                # Prefix word + optional comma/space + approval word (+ optional trailing context)
                r'^' + prefix_words + r'[,.]?\s+' + approval_words,
                # Approval word + conjunction/comma + short trailing instruction.
                # Precision signal: user approves AND issues an instruction (not
                # a sentence merely beginning with an approval word).
                r'^' + approval_words + r'\s*(?:,|\s+(?:and|then|plus|&))\s+[\w][\w ,]*[.!]*$',
            ]
            for p in patterns:
                if re.match(p, prompt):
                    return True

        return False
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
