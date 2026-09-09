#!/usr/bin/env python3
r"""The memory-policy guard's whole decision, as a program FILE rather than program TEXT.

Reads the pending memory write's content from STDIN and prints exactly two lines:

    verdict<TAB>override            an accepted override marker is present
    verdict<TAB>clean               neither marker nor pattern
    verdict<TAB>match<TAB><json>    a JSON array of match snippets, each cut to 80 chars
    status<TAB>complete             ALWAYS the last line

WHY THIS FILE EXISTS AT ALL, which is the only reason worth reading first. What stood in
`hooks/scripts/writ-memory-policy-guard.sh` were TWO `python3 <<PY` blocks, the override
pre-filter and the pattern scan, and each one built its own first statement out of a
NESTED command substitution:

    content = $(python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$CONTENT")

The heredoc delimiter was unquoted, so bash ran that substitution and pasted its output
into the outer program's TEXT before python ever started. The memory content therefore
crossed on the INNER call's argv, Linux caps a single argv string at `MAX_ARG_STRLEN`
(32 pages, 131,072 bytes on this machine), `2>/dev/null` hid the failed exec, the
substitution yielded nothing, the outer program's line became `content = `, and
`|| MATCHED=""` plus `[ -z "$MATCHED" ] && exit 0` read the resulting SyntaxError as
"nothing to gate". Measured 2026-09-08 with the same rule-weakening phrase in every
payload: 200 bytes DENY, 131,000 DENY, 200,000 SILENT ALLOW, rc 0 each time.

THE CONTENT ARRIVES ON STDIN, and the hook feeds it with `printf`, which is a bash
BUILTIN: the content reaches no argv and no environment at all, because bash writes it
into the pipe itself. That is stronger than
`docs/adr/ADR-hook-exec-argument-boundary.md`'s stated rule ("a payload that must cross
goes on STDIN, never on the command line") rather than merely compliant with it, and the
nested substitution is gone rather than made bigger.

WHY A FILE AND NOT `python3 -c '<program>'`, because that is the same decision's other
form and a later reader will want to collapse it. Two of the nine patterns carry a single
quote in their own Python source (`r'\bdon\'?t...'` and `r'["\']?i\s+trust\s+you...'`).
A single-quoted `-c` argument cannot contain a single quote, so the inline form would
force those two literals to be re-quoted in a cycle whose entire purpose is transport,
and whether `r"\bdon'?t"` matches exactly what `r'\bdon\'?t'` matched is precisely the
kind of claim this repository has been burned by reasoning about instead of executing. A
file keeps every pattern byte-identical, makes the list testable in process, and cannot
be reopened by a future pattern that happens to contain a quote. `python3 "$FA"
--stdin-json` in the same hook is the precedent for a support script living here.

THE OVERRIDE CHECK RUNS FIRST, AND THAT ORDER IS A DECISION, not an accident of layout.
`scan()` returns on the marker before it reads, compiles or scans a single entry of the
pattern list, so an authorized override can never be reported as a match and can never
write a `memory_policy_deny` row. Merging two blocks into one is exactly where that
ordering could be lost, so it carries its own pin (content holding BOTH an override
marker and a weakening phrase must allow).

THE SENTINEL IS A POSITIVE RECORD, NOT A FORMALITY. `status<TAB>complete` is printed as
the last line on every path that reaches an answer, and on no path that does not, so the
consumer reads "this block finished" from the output instead of inferring it from an exit
status or from an empty string. An exception anywhere above reaches that print through no
path at all, which is the signal the hook's fault arm needs. The marker's text is spelled
here as its own literal rather than read from `bin/lib/common.sh`'s
`WRIT_EXTRACTOR_SENTINEL`, for the reason recorded there: a drift between the two makes
every memory write look like a fault, so it fails loudly rather than quietly loosening.

WHAT CANNOT COME BACK UP THIS CHANNEL. The only content-derived bytes printed are inside
a `json.dumps` string, which cannot hold a raw newline or a raw tab, so content can
neither invent a line nor invent a TSV field. Content that spells `verdict<TAB>override`
on a line of its own is just more text to scan: the hook reads the verdict from the FIRST
line of this program's own output, which content never reaches.
"""
from __future__ import annotations

import json
import re
import sys

# The same five-word constant `bin/lib/common.sh` holds as `WRIT_EXTRACTOR_SENTINEL`.
# See the docstring above for why it is a second literal instead of a third channel.
STATUS_COMPLETE = "status\tcomplete"

# The two accepted override forms, unchanged from the pre-filter this file replaces:
# YAML `explicit_rule_override: true`, or a body line `override authorized by: <name>`.
# The marker is an UNAUTHENTICATED declaration inside model-controlled content, before
# this change and after it, matched by the same two `re.search` calls against the same
# string. It is the guard's designed escape hatch (the deny text tells the model about
# it), not a control, and nothing here makes it more or less forgeable than it was.
OVERRIDE_PATTERNS = [
    r'explicit_rule_override\s*:\s*true',
    r'override\s+authorized\s+by\s*:',
]

# NINE patterns, and the count is MEASURED against the source they came from rather than
# copied from the prose about them: several comments in this tree, and the plan for this
# cycle, said eight. Moved BYTE-IDENTICALLY out of the retired heredoc, deliberately
# including the two lines whose Python source carries a single quote, because a transport
# change that silently altered one regex's compiled meaning would look exactly like a
# working one. Held as STRINGS, uncompiled: the override arm in `scan()` returns before
# anything here is touched.
#
# No pattern is added, removed or re-worded by this cycle. A cycle that changed both the
# transport and the patterns could not attribute a behavior change to either.
patterns = [
    # Skip / no verification variants
    r'\bskip\s+(?:the\s+)?(?:verification|verify|test\s+run|tests?|check|checks|validation|validate)\b',
    r'\bno\s+(?:verification|verify|re-?run|re-?runs?|fresh\s+verification)\b',
    r'\bnever\s+(?:re-?run|verify|test)\b',
    r'\bdon\'?t\s+(?:re-?run|verify|re-?verify)\b',
    # Face-value / trust-as-bypass
    r'take\s+(?:the\s+)?[\w\s\-]{0,40}?(?:report|claim|output|result|answer)\s+at\s+face\s+value',
    r'\btrust\s+[\w\s\-]{0,20}?(?:source|sub-?agent|implementer|worker|report)\s*=\s*(?:no|skip|never|face)',
    # Rule-override / bypass language outside an authorized marker
    r'\b(?:override|bypass|weaken|suspend|disable)\s+[\w\s\-]{0,20}?(?:ENF-|rule|verify|discipline|verification)',
    # PSR-003 exact phrasing
    r'["\']?i\s+trust\s+you["\']?[^\n]{0,120}(?:skip|no|never|face\s+value|move\s+on)',
    r'take\s+[\w\s\-]{0,40}?\s+at\s+face\s+value\s+and\s+move\s+on',
]

# How much of a match reaches the caller. The bound is load-bearing twice over: it keeps
# the verdict line short, and it bounds the `MATCHED_RAW` value the hook then crosses in
# ENV to build the friction row, which is the one argv/env crossing this hook keeps
# (recorded `bounded` in tests/test_exec_boundary_census.py with this cut named as its
# enforcer, across at most nine patterns).
SNIPPET_MAX = 80


def scan(content: str) -> str:
    """The verdict line for `content`: `override`, `clean`, or `match` plus its JSON.

    Returns the line instead of printing it so that `main()` owns the two prints and the
    sentinel is the LAST thing written on every answered path.
    """
    for pattern in OVERRIDE_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return "verdict\toverride"

    matched = []
    for p in patterns:
        m = re.search(p, content, re.IGNORECASE)
        if m:
            matched.append(m.group(0)[:SNIPPET_MAX])
    if matched:
        return "verdict\tmatch\t" + json.dumps(matched)
    return "verdict\tclean"


def main() -> int:
    # `surrogateescape` for the reason writ-pre-write-dispatch.sh's translator gives: it
    # matches how argv and os.environ already decoded these bytes, so a content byte that
    # is not valid UTF-8 cannot turn a decision into a crash. Empty stdin decodes to '',
    # which `scan` answers `clean` -- the same answer the hook's own empty-content arm
    # gives one line earlier, and the honest one for "there is nothing to weaken a rule".
    content = sys.stdin.buffer.read().decode("utf-8", "surrogateescape")
    print(scan(content))
    print(STATUS_COMPLETE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
