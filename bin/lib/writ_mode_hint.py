"""Standalone, stdlib-only mode classifier for prompt auto-routing.

SOURCE OF TRUTH for classify_mode_hint. Kept as a single file with ZERO writ-package
imports so the UserPromptSubmit hook (writ-rag-inject.sh) can import it cheaply and
reliably from inside its latency-sensitive prompt-parse block. Importing the full
writ.session package chain there was intermittently failing under heavy machine load
(the import was swallowed by the block's error handling, so the auto-route silently
didn't fire). A one-file stdlib-only import removes that failure surface.

writ.session.mode_engine re-exports classify_mode_hint from here so the rest of the
system and the unit tests have a single definition.
"""

from __future__ import annotations

import re

# High-precision detection of audit/explore/research-shaped prompts, which map to
# investigate mode (the gate-light audit/explore/research engine). PRECISION over recall:
# a miss falls back to the manual mode directive, but a false positive on a build task is
# friction. We match audit as a VERB ("audit the/our/...") -- never the noun ("audit log",
# "audit trail") -- plus a small set of unambiguous security/explore/research phrases.
_INVESTIGATE_SIGNALS = re.compile(
    r"\bvulnerabilit"  # vulnerability / vulnerabilities / vulnerable
    r"|\bcwe-?\d"  # CWE-79 ...
    r"|\bcves?\b"  # CVE / CVEs
    r"|security\s+(posture|process|review|audit|assessment|risks?|gaps?|issues?|concerns?|holes?|exposure|vulnerabilit)"
    r"|\baudit\s+(the|our|this|your|all|my|for|against)\b"  # audit-as-VERB, not "audit log"
    r"|\binvestigate\b"
    r"|explore\s+(the|our|this|your)\s+(\w+\s+)?(codebase|code|project|repo|repository|module|system|architecture|structure)"
    r"|\bresearch\s+(the|our|whether|how|what|best|current|options)"
    r"|\bassess\s+(the|our|whether|how|security|risk)"
    r"|how\s+secure\b"
    r"|\bpenetration\s+test|\bpentest|\bthreat\s+model",
    re.IGNORECASE,
)


# Detection of implementation/build-shaped prompts, which map to work mode (the full
# plan -> test-skeletons -> implementation gated workflow). Auto-routing here is deliberate
# (agent self-classification proved unreliable). Recall-biased toward catching real build
# work -- a false positive only means an early "set a mode" gate the user clears with one
# `mode set conversation` -- but still gated to an action VERB + a code/artifact OBJECT so
# plain discussion / questions / reviews do not trip it.
_WORK_SIGNALS = re.compile(
    r"\b(implement|refactor|rewrite|reimplement|scaffold)\b"
    r"|\b(add|create|build|write|make|introduce|wire\s*up|set\s*up|hook\s*up|stub\s*out|extract)\s+"
    r"(a|an|the|some|new|another)?\s*(\w+[\s-]+){0,3}"
    r"(feature|function|method|class|endpoint|route|component|module|service|handler|"
    r"migration|schema|model|test|tests|hook|script|api|integration|page|form|field|"
    r"column|table|command|helper|wrapper|adapter|interface|controller|middleware|job|task|queue|cron)"
    r"|\bfix\s+(the|a|this|that|our)?\s*(\w+[\s-]+){0,2}"
    r"(bug|error|issue|crash|failure|fault|regression|typo|exception|test|build|leak|race)"
    r"|\b(change|update|modify|patch|edit|adjust|tweak)\s+(the|a|this|our)?\s*(\w+[\s-]+){0,3}"
    r"(function|method|class|code|logic|file|component|endpoint|behavior|implementation|handler|query|config|schema)"
    r"|\b(add|write)\s+(a|the|some|unit|integration|e2e)?\s*tests?\b"
    r"|\bmigrate\s+(the|our|this)\b",
    re.IGNORECASE,
)

# Imperative build verbs, added after the classifier was measured against real prompts:
# _WORK_SIGNALS matched 18 of 808 past user prompts, and "yes please update readme", "remove
# the deprecated handler" and "rename the config key" all missed because each wants its object
# from a closed noun list. PRECISION IS KEPT BY POSITION instead: the verb has to open a clause
# (start of the prompt or a line, after sentence punctuation or a comma, or after a go-ahead
# lead such as "please", "yes", "ok", "can you"), which is where an instruction puts it and
# where a question does not. git push / commit / merge are deliberately absent: they publish
# work rather than change code, and a plan gate in front of a push is pure friction. "port"
# and "drop" are absent because "port 8765 is in use" and "drop it" are not requests.
_IMPERATIVE_LEAD = (
    r"(?:^|[.!;:,\n]\s*|\b(?:please|pls|yes|yeah|yep|ok|okay|sure|now|then|also|and|"
    r"go\s+ahead\s+and|let'?s|can\s+you|could\s+you|would\s+you|will\s+you)\s+)"
)
_BUILD_VERB = (
    r"(?:update|remove|delete|rename|move|replace|clean\s*-?\s*up|cleanup|bump|upgrade|"
    r"convert|deprecate)"
)
# A verb followed by one of these is conversation, not a code change: "update me on",
# "move on", "clean up after". Pull-request and tracker objects ("update pr message",
# "update the ticket status") are housekeeping, not code, and would otherwise put a plan
# gate in front of a commit-and-push.
_NOT_AN_OBJECT = (
    r"(?!(?:me|us|you|yourself|on|forward|ahead|along|after)\b)"
    r"(?!(?:(?:the|a|an|this|that|my|our)\s+)?"
    r"(?:prs?|pull\s+requests?|tickets?|jira|story|epic|confluence)\b)"
)
_WORK_IMPERATIVE = re.compile(
    _IMPERATIVE_LEAD + _BUILD_VERB + r"\s+" + _NOT_AN_OBJECT + r"[\w./-]",
    re.IGNORECASE | re.MULTILINE,
)
# "data cleanup of orphaned option_ids" names the task as a noun, so there is no verb to lead
# with. Only the cleanup noun is accepted this way, and only with an object after it.
_WORK_CLEANUP_NOUN = re.compile(
    r"\b(?:clean\s*-?\s*up|cleanup)\s+(?:of|for)\s+\w"
    r"|\b(?:data|code|db|database|repo|dead[\s-]+code)\s+clean\s*-?\s*up\b",
    re.IGNORECASE,
)
# A prompt that OPENS as a question is a question, whatever verb appears later in it ("why did
# the cleanup of X fail", "is the rename done"). Applied only to the two patterns above, so no
# prompt _WORK_SIGNALS already matched changes its answer.
_QUESTION_OPENING = re.compile(
    r"^\s*(?:why|where|what|when|who|whom|whose|which|how|is|are|was|were|do|does|did|"
    r"has|have|had|should|shall)\b",
    re.IGNORECASE,
)


def classify_mode_hint(prompt: str | None) -> str | None:
    """Best-effort mode suggestion from a user prompt. Returns 'investigate' for an
    audit/explore/research-shaped request, 'work' for a build/implementation request, else
    None. Pure. Investigate is checked first (an "audit and fix" request is investigation)."""
    if not prompt:
        return None
    if _INVESTIGATE_SIGNALS.search(prompt):
        return "investigate"
    if _WORK_SIGNALS.search(prompt):
        return "work"
    if not _QUESTION_OPENING.match(prompt) and (
        _WORK_IMPERATIVE.search(prompt) or _WORK_CLEANUP_NOUN.search(prompt)
    ):
        return "work"
    return None


if __name__ == "__main__":  # CLI: print the hint (or nothing) for argv[1]
    import sys

    hint = classify_mode_hint(sys.argv[1] if len(sys.argv) > 1 else "")
    if hint:
        sys.stdout.write(hint)
    sys.stdout.write("\n")
