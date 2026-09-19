"""Approval-request evidence reader for auto-approve-gate.sh (stdlib-only).

WHAT THIS ANSWERS, AND WHY NOTHING ELSE DID. The gate token already records which gate
an approval authorizes (line 2), which plan.md it was given for (line 3), and whether it
has been spent (the claim-by-rename). The question nothing answered is "was an approval
REQUESTED at all". That is the gap a live near miss went through: the user answered a
scope question with an approval word while the test-skeletons gate was pending, no
skeletons had been presented, and only a typo stopped the advance.

So this module answers one question and no others: did the immediately preceding
assistant turn ask the user for this approval. Adjacency is what supplies the referent.
An approval request two exchanges back is not what this turn is answering.

No writ-package import (matches approval_match.py / gate_advance_outcome.py load
robustness): the hook calls it by path even if the package is unimportable. Pure, no
writes, and FAIL-CLOSED in one direction only. An absent path, a missing or unreadable
file, a transcript with no assistant row, a garbled line and a turn with no marker all
return the same answer: no evidence. Absence is never an advance. The cost of a false
negative is one question; the cost of a false positive is an approval nobody gave.

TWO DELIBERATE DIFFERENCES FROM hooks/scripts/writ-comms-output-gate.sh, whose tail scan
this otherwise mirrors:

  1. An assistant row carrying no text block (a tool-call-only turn) does NOT erase the
     evidence found in the text turn before it. The comms gate overwrites its
     accumulator on every assistant row, which would let a trailing tool call hide a
     genuine request.
  2. Code spans are NOT stripped before matching. The comms gate strips them so a
     backticked example is not flagged; here, agents routinely backtick the approval
     word, so stripping would turn the most common honest phrasing into a false
     negative. Backticks, asterisks and quote characters are REMOVED instead, which is
     what makes "Say `approved` to proceed" and "Reply **approved** when ready" both
     count.

THE RESIDUAL, STATED PLAINLY: an agent that emits a marker phrase without presenting
anything passes this check. That is the same residual plan.md already carries (the agent
chooses what it writes), it is out of reach of any prompt-side check, and it is not the
failure that happened. What the transcript does prove is that the words were actually put
in front of the user, because Claude Code writes the transcript rather than the agent's
claim about itself.
"""

import json
import re

# Bound the scan, exactly as the comms gate does: only the tail of the transcript can
# hold the turn immediately before this one, and a session transcript is unbounded.
_TAIL_LINES = 400

# A CLOSED SET, and membership has one test: the phrase's only ordinary use is asking the
# user for the approval word. "say approved" and "approved to proceed" are the ritual
# sentence SKL-PROC-WRIT-FAILURE-001 and PHA-WORK-002 already mandate verbatim ("Test
# skeletons written: ClassName (N tests). Say approved to proceed."). A phrase joins this
# set only under the same test.
#
# GATE SPECIFICITY IS DELIBERATELY NOT REQUIRED. These prove a request was made, not
# which gate it named. Requiring per-gate artifact tokens would multiply false negatives
# across legitimate wording to defend a case with no observed instance, and the
# cross-gate direction is already the token's line 2.
REQUEST_MARKERS = (
    "say approved",
    "reply approved",
    "type approved",
    "approved to proceed",
)

# Markdown decoration and quote characters, removed rather than treated as word
# boundaries: `approved` and **approved** must normalize to the same bytes as approved.
# The curly quotes are written as \u escapes (no literal non-ASCII character in this
# file), the same discipline hooks/scripts/writ-comms-output-gate.sh uses for the dash
# characters it scans for, so no encoding pass can mangle the set.
_DECORATION_RE = re.compile("[" + "`*\"'" + "\u2018\u2019\u201c\u201d" + "]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, decoration removed, whitespace collapsed. Pure."""
    return _WHITESPACE_RE.sub(" ", _DECORATION_RE.sub("", text or "")).strip().lower()


def has_request_marker(text: str) -> bool:
    """True when the normalized text contains any member of REQUEST_MARKERS."""
    normalized = normalize(text)
    return any(marker in normalized for marker in REQUEST_MARKERS)


def _assistant_text(row: dict) -> str:
    """The text blocks of one assistant transcript row, joined; "" for a tool-call-only
    turn. Content is a list of blocks in the shape Claude Code writes, or a bare string.
    """
    content = (row.get("message") or {}).get("content")
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if isinstance(content, str):
        return content
    return ""


def evidence_flags(transcript_path: str) -> tuple[bool, bool]:
    """(an approval was requested, any assistant text was found at all).

    The second flag is not a second opinion on the first: it is what the
    `approval_evidence_missing` audit row records so "the transcript had nothing in it"
    is distinguishable from "the transcript had a turn that did not ask", which are
    different operational problems.

    AT MOST ONE `user` ROW MAY FOLLOW THE EVIDENCE TURN. That one is the prompt being
    classified, which may or may not have been appended to the transcript yet, so both
    orderings are tolerated. Two or more mean the request belongs to an earlier exchange.
    """
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        # Absent path, missing file, a directory, an I/O fault: all mean no evidence.
        return False, False

    last_text = ""
    user_rows_after = 0
    for line in lines[-_TAIL_LINES:]:
        try:
            row = json.loads(line)
        except Exception:
            # A garbled line reads as unreadable, which is the fail-closed direction.
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("type")
        if kind == "user":
            user_rows_after += 1
            continue
        if kind != "assistant":
            continue
        text = _assistant_text(row)
        if text:
            last_text = text
            user_rows_after = 0

    if not last_text:
        return False, False
    return (user_rows_after <= 1 and has_request_marker(last_text)), True


def request_was_asked(transcript_path: str) -> bool:
    """True only when the preceding assistant turn asked for this approval.

    The single predicate the hook gates the exact tier on. Fail-closed: any read or parse
    failure, no assistant row, or a turn with no marker all return False.
    """
    return evidence_flags(transcript_path)[0]
