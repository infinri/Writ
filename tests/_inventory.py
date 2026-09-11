"""Repo populations, derived from their single source.

WHY THIS EXISTS. Four count pins broke during the Maat program, each costing a full suite
run to find, and the cause was DUPLICATION rather than the pins themselves: the hook
registration count was asserted in 4 test files and the hook event count in 7. Adding one
doctor check meant editing a count, a second count, a name set, and a test whose NAME said
"seventeen". Four edits for one change, three of them copies.

So each population gets ONE canonical assertion, kept where the test's declared job is to
review that number (`test_phase51_doc_counts.py` calls itself a "source-derived count
regression gate"), and every other site derives from here and asserts agreement. Growth then
touches production plus at most one test line.

THE TRIPWIRES ARE NOT REMOVED. This module exists to delete copies, not checks. A silent
population change must still fail the suite, and `test_count_pin_discipline.py` asserts that
the canonical tripwire still pins the real number.

THE RISK, and it is the reason every derivation here is asserted non-empty by
`test_count_pin_discipline.py`: a derivation that silently returned nothing would make every
dependent assertion pass on any tree, which is worse than the duplicated literals it
replaced. A vacuous test is a lie that reads like coverage.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
HOOK_SCRIPTS_DIR = REPO / "hooks" / "scripts"
TESTS = REPO / "tests"

# The THREE ways a hook script can refuse, and there are only three. Each pattern is matched
# against source lines that are not whole-line comments, so a script that merely DESCRIBES a
# deny in its header (writ-debug-code-gate.sh says "Emits a deny permissionDecision" on line
# 7) is classified on its code, not its prose.
_REFUSAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 1. The single-source PreToolUse funnels in bin/lib/common.sh.
    ("emit_deny_or_ask", re.compile(r"\b(emit_deny|emit_ask)\b")),
    # 2. A non-zero bash exit status. Zero is an allow, so the digit is part of the pattern.
    ("nonzero_exit", re.compile(r"^\s*exit\s+[1-9][0-9]*\s*(?:#.*)?$")),
    # 3. A permissionDecision of deny or ask, hand-built inside an embedded python block.
    #    Both quoting styles, because the scripts use single quotes inside heredocs and
    #    double quotes outside them.
    ("permission_decision", re.compile(
        r"""permissionDecision["']?\s*:\s*["'](deny|ask)["']"""
    )),
)


def _hooks_manifest() -> dict:
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def hook_events() -> list[str]:
    """The Claude Code event names `hooks/hooks.json` registers for.

    The template contract in `test_settings_template_sync.py` is the canonical assertion on
    this population's SIZE; this returns the names so other sites can agree with the source
    instead of restating a number.
    """
    return sorted((_hooks_manifest().get("hooks") or {}).keys())


def hook_registrations() -> list[tuple[str, str]]:
    """(event, command) for every registered hook command.

    Counting `command` leaves is exactly how `test_phase51_doc_counts.py` derives the
    canonical number, so the two cannot disagree about what a registration is.
    """
    out: list[tuple[str, str]] = []
    for event, entries in (_hooks_manifest().get("hooks") or {}).items():
        for entry in entries or []:
            for hook in (entry.get("hooks") or []):
                command = hook.get("command")
                if command:
                    out.append((event, command))
    return out


def hook_script_names() -> list[str]:
    """Basenames without `.sh` for every registered hook script.

    Separate from `hook_registrations` because one script can be registered for several
    events, so the two counts legitimately differ and conflating them was how an earlier
    scan reported 41 hooks against 44 registrations.
    """
    names: set[str] = set()
    for _event, command in hook_registrations():
        for token in str(command).split():
            if token.endswith(".sh"):
                names.add(token.rsplit("/", 1)[-1][:-3])
    return sorted(names)


def matcher_tools_for_script(script_name: str) -> list[str]:
    """The PreToolUse matcher's tool names for a registered hook script, split on `|`.

    Derived from `hooks/hooks.json` rather than copied as a literal (e.g. `["Grep",
    "Read", "Glob"]` hand-typed into a test), so a property test's tool FACTOR stays
    locked to the real registration. Hand-listing it would let a future matcher edit
    (a tool added or removed from `writ-debug-code-gate.sh`'s registration) leave the
    test matrix silently narrower or stale than what Claude Code actually invokes the
    hook for, the same duplication problem this module exists to delete (see the
    module docstring's four-broken-pins story).

    Returns [] when `script_name` is not registered under any event, so a caller can
    fail loudly on an empty result rather than silently testing zero tools.
    """
    for _event, entries in (_hooks_manifest().get("hooks") or {}).items():
        for entry in entries or []:
            for hook in (entry.get("hooks") or []):
                command = str(hook.get("command") or "")
                if command.rstrip().endswith(f"/{script_name}"):
                    matcher = entry.get("matcher") or ""
                    return [t for t in matcher.split("|") if t]
    return []


def _refusal_markers(source: str) -> list[str]:
    """Which of the three refusal mechanisms this script's CODE uses, in pattern order."""
    lines = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    found: list[str] = []
    for name, pattern in _REFUSAL_PATTERNS:
        if any(pattern.search(ln) for ln in lines):
            found.append(name)
    return found


def refusing_script_markers() -> dict[str, list[str]]:
    """Every hook script that can refuse, mapped to the mechanisms it refuses WITH.

    Derived from `hooks/scripts/*.sh` source, never from a list. The mechanisms are returned
    alongside the names so a triager reading a red completeness test can see WHY a script was
    classified as refusing without re-deriving it by hand.
    """
    out: dict[str, list[str]] = {}
    for path in sorted(HOOK_SCRIPTS_DIR.glob("*.sh")):
        markers = _refusal_markers(path.read_text(encoding="utf-8", errors="replace"))
        if markers:
            out[path.name] = markers
    return out


def derive_refusing_scripts() -> list[str]:
    """The canonical refusing-script population, derived from hook source.

    ONE HOME for this population. The negative-controls fire drill declares a census of
    refusals per script, and a census compared against a literal list copied into a drill
    module would go stale the moment a new refusing script landed: that is the exact
    duplication this module exists to delete.

    THE DERIVATION IS DELIBERATELY NOT NARROWED TO WHAT IS ALREADY COVERED. A script that
    refuses and is not declared anywhere must make the completeness test RED, because that
    red is the only signal a new, untested refusal exists. Filtering this function down to
    the declared set would make every future gap invisible, which is worse than the gap.
    """
    return sorted(refusing_script_markers())


# ── OUT-capture coverage (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482, Decision 2) ──
#
# Three source-derived populations that keep hook-reply capture structural. The funnel
# `emit_hook_reply` lives in `bin/lib/common.sh`, which is OUTSIDE the scanned directory,
# so the one place allowed to print an envelope needs no allowlist entry.
_ENVELOPE_KEY = "hookSpecificOutput"
_FUNNEL_HELPERS = re.compile(r"\b(emit_deny|emit_ask)\b")
# `cat <<EOF` / `python3 <<'PY'` / `<<-DELIM`: the body streams from the next line to the
# delimiter, so it belongs to the opening line's emission text rather than being read as a
# command of its own.
_HEREDOC_START = re.compile(r"<<-?\s*(?P<q>['\"]?)(?P<delim>\w+)(?P=q)")
# A redirect operator with its optional file descriptor. Not `hooks_lint._REDIRECT`, which
# matches `2>>"$SINK"` and would wave through two real emissions in this tree: a
# stderr-only redirect leaves stdout exactly where it was.
_REDIRECT_OP = re.compile(r"(?P<fd>[0-9]?)>{1,2}")
_UNESCAPED_QUOTE = re.compile(r'(?<!\\)"')
_ASSIGNMENT = re.compile(r"^\s*(?:local\s+|export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=")
# An assignment whose value STARTS with the substitution, so the substitution is what the
# variable receives. `WRIT_AC="text $(basename x)" python3 ...` does not match: there the
# `=` is followed by a quote, and the command after it keeps its own stdout.
_CAPTURING_ASSIGNMENT = re.compile(
    r"^\s*(?:local\s+|export\s+)?[A-Za-z_][A-Za-z0-9_]*=(?P<open>\$\(|`)"
)
_STDOUT_COMMAND = re.compile(r"^\s*(echo|printf)\b")


def _blanked_lines(source: str) -> list[str]:
    """Whole-line comments blanked IN PLACE, keeping every line number intact.

    `_refusal_markers` above drops comment lines because it only asks whether a pattern
    occurs anywhere. CHECK 3 reports (script, line), so dropping would shift every number
    it reports. Same predicate, different disposal.

    Split on "\\n" and NOT with `str.splitlines()`, which also breaks on U+2028 and would
    report every line after one off by one: `writ-pre-write-dispatch.sh` holds a literal
    U+2028 inside a python string, at line 308 as of the write-path spawn-reduction
    cycle (it was line 200 before). The LINE NUMBER drifts with every edit to that hook
    and nothing asserts it, so re-derive it rather than trusting it; what does not drift
    is that a real file in this tree contains the character, which is the whole reason
    this function does not use `str.splitlines()`.
    """
    return ["" if ln.lstrip().startswith("#") else ln for ln in source.split("\n")]


def _blanked_source(path: Path) -> str:
    return "\n".join(_blanked_lines(path.read_text(encoding="utf-8", errors="replace")))


def direct_blackbox_out_calls() -> list[str]:
    """CHECK 1: hook scripts that still call `blackbox_log out` themselves.

    Exact, with no false-positive surface. A hook that cannot reach the logger cannot
    record something other than what it sent, so byte identity holds by construction
    rather than by review. Must be empty.
    """
    return [
        path.name
        for path in sorted(HOOK_SCRIPTS_DIR.glob("*.sh"))
        if "blackbox_log out" in _blanked_source(path)
    ]


def envelope_emitting_scripts(*, scripts_dir: Path = HOOK_SCRIPTS_DIR) -> list[str]:
    """CHECK 2: hook scripts that build a hookSpecificOutput envelope or refuse through
    the shared deny/ask helpers.

    THE ANTI-VACUITY POPULATION. It is asserted NON-EMPTY in
    `test_count_pin_discipline.py`, because a scanner whose matching silently broke would
    empty this out and make every emptiness assertion beside it pass on any tree. That is
    the positive signal a check that only ever asserts emptiness cannot give.
    """
    out: list[str] = []
    for path in sorted(scripts_dir.glob("*.sh")):
        src = _blanked_source(path)
        if _ENVELOPE_KEY in src or _FUNNEL_HELPERS.search(src):
            out.append(path.name)
    return out


def _has_odd_quotes(text: str) -> bool:
    return len(_UNESCAPED_QUOTE.findall(text)) % 2 == 1


def _substitution_tail(opening: str, start: int, opener: str) -> str | None:
    """Whatever follows the substitution opened at `start`, or None when it does not
    close on this line, which is the multi-line case where the emission sits INSIDE it."""
    if opener == "`":
        close = opening.find("`", start)
        return None if close < 0 else opening[close + 1:]
    depth = 1
    for index in range(start, len(opening)):
        if opening[index] == "(":
            depth += 1
        elif opening[index] == ")":
            depth -= 1
            if depth == 0:
                return opening[index + 1:]
    return None


def _is_captured(opening: str) -> bool:
    """The emission's OWN output is consumed by an assignment, so it is a value rather
    than a reply. This is what exempts every converted site and the three read-only uses
    (reading permissionDecision back out, mutating an envelope, building into a variable)
    without an exception each.

    NOT "a `$(` appears somewhere on the line". That weaker rule read an env-var PREFIX
    holding a substitution, `VAR="text $(basename x)" python3 <<PY`, as a capture: there
    the substitution feeds a SEPARATE command and the heredoc's own output still goes
    straight to stdout. Two live emissions had exactly that shape before this cycle
    converted them, and CHECK 2 cannot see it either once the script reaches the funnel
    on any other line, so a converted script gaining a new bare emission would have gone
    green on all three checks.
    """
    match = _CAPTURING_ASSIGNMENT.match(opening)
    if not match:
        return False
    tail = _substitution_tail(opening, match.end(), match.group("open"))
    return tail is None or tail.strip() == ""


def _is_stdout_redirected(opening: str) -> bool:
    """A STDOUT redirect only. `2>>"$SINK"` is a stderr sink and leaves the envelope on
    stdout, so it must not exempt the line."""
    return any(m.group("fd") in ("", "1") for m in _REDIRECT_OP.finditer(opening))


def _emission_end(lines: list[str], start: int) -> int:
    """The last line of the command that opens at `start`.

    Three continuations, applied in order, because a single command can use all three:
    a backslash line join, an odd count of unescaped double quotes (how a `python3 -c "`
    block runs on to the line that closes the quote), and a heredoc body running to its
    delimiter.
    """
    end = start
    last = len(lines) - 1
    while end < last and lines[end].rstrip().endswith("\\"):
        end += 1
    if _has_odd_quotes("\n".join(lines[start:end + 1])):
        j = end + 1
        while j <= last and '"' not in lines[j]:
            j += 1
        if j <= last:
            end = j
    match = _HEREDOC_START.search("\n".join(lines[start:end + 1]))
    if match:
        delim = match.group("delim")
        j = end + 1
        while j <= last and lines[j].strip() != delim:
            j += 1
        if j <= last:
            end = j
    return end


def _scan_script(path: Path) -> list[tuple[str, int]]:
    lines = _blanked_lines(path.read_text(encoding="utf-8", errors="replace"))
    findings: list[tuple[str, int]] = []
    envelope_vars: set[str] = set()
    index = 0
    while index < len(lines):
        opening = lines[index]
        if not opening.strip():
            index += 1
            continue
        end = _emission_end(lines, index)
        block = "\n".join(lines[index:end + 1])
        if _ENVELOPE_KEY in block:
            if _is_captured(opening):
                assigned = _ASSIGNMENT.match(opening)
                if assigned:
                    envelope_vars.add(assigned.group("name"))
            elif not _is_stdout_redirected(opening):
                findings.append((path.name, index + 1))
        elif (envelope_vars and _STDOUT_COMMAND.match(opening)
                and not _is_captured(opening) and not _is_stdout_redirected(opening)):
            for name in envelope_vars:
                if re.search(r"\$\{?" + re.escape(name) + r"\b", block):
                    findings.append((path.name, index + 1))
                    break
        index = end + 1
    return findings


def bare_envelope_emissions(
    *, scripts_dir: Path = HOOK_SCRIPTS_DIR
) -> list[tuple[str, int]]:
    """CHECK 3, the heuristic: (script, line) for every envelope emission that reaches
    stdout without going through the funnel.

    It catches the shape the two exact checks miss, a bare emission added to a script
    that already calls the funnel. `scripts_dir` is a keyword rather than a constant so
    the precision of the matching can be pinned against synthetic fixtures per shape,
    the same parameterization `writ/hooks_lint.py::lint_hooks` uses for `plugin_root`.

    WHAT IT DOES NOT COVER, stated rather than implied: an emission whose capture cannot
    be decided from its own opening line, such as an envelope printed by a shell function
    defined elsewhere and called bare. CHECK 2 catches that only when it lands in a
    script that never reaches the funnel.
    """
    findings: list[tuple[str, int]] = []
    for path in sorted(scripts_dir.glob("*.sh")):
        findings.extend(_scan_script(path))
    return sorted(set(findings))


# ── Exit-code capture coverage (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482) ──
#
# Two more source-derived populations, holding refusal-exit capture by DERIVATION rather
# than by a list of exit sites. The one enumerated list in this tree,
# `tests/firedrill/_census.py`, declares 7 of the 10 real non-zero exit sites, which is the
# measured cost of registering each one; a shared trap needs no registration for the
# eleventh.


def direct_blackbox_exit_calls() -> list[str]:
    """Hook scripts that call `blackbox_log exit` themselves. Must be empty.

    The exit-row sibling of `direct_blackbox_out_calls`, and the same argument: the shared
    exit trap in `bin/lib/common.sh` is the one writer, so a hook that cannot reach the logger
    cannot record an exit code other than the one it actually returned.
    """
    return [
        path.name
        for path in sorted(HOOK_SCRIPTS_DIR.glob("*.sh"))
        if "blackbox_log exit" in _blanked_source(path)
    ]


def nonzero_exit_scripts() -> list[str]:
    """Hook scripts with a non-zero exit site, filtered out of `refusing_script_markers()`.

    Derived from that population's OWN `nonzero_exit` marker rather than re-scanning, so the
    two cannot disagree about what a non-zero exit is. It is asserted NON-EMPTY by
    `test_blackbox_record_schema.py`, because a silently-empty derivation would make the
    subset check beside it (every member is auto-instrumented by `common.sh`, so its exit
    reaches the shared trap) pass on any tree.

    KNOWN LIMIT OF THE DERIVATION, stated rather than worked around: `_REFUSAL_PATTERNS`'s
    `nonzero_exit` regex matches a LITERAL digit, so `exit "$WARN_EXIT"`
    (`validate-rules.sh` lines 255 and 263) does not match it. `validate-rules.sh` is in the
    population anyway through its two `exit 2` sites, so no script is missed today. Widening
    that regex is deliberately NOT done here: it is shared with the fire drill's completeness
    check, and changing it changes that check's meaning too.
    """
    return sorted(
        name for name, markers in refusing_script_markers().items()
        if "nonzero_exit" in markers
    )


def doctor_check_names() -> list[str]:
    """The doctor's checks, in registry order, straight from `doctor._CHECKS`.

    Returned as a LIST rather than a set so a test can assert order and count without
    holding either one as a literal.
    """
    from writ.session import doctor

    return [name for name, _fn in doctor._CHECKS]


# ── Documentation style ratchet (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482) ──
#
# The user's standing rule: no em dash, and no double hyphen standing in for one; hyphens
# only to join words, with commas, colons, semicolons or parentheses for clause breaks.
# `hooks/scripts/writ-comms-output-gate.sh` enforces it on the assistant's REPLY text, which
# is a population disjoint from this one: that gate never reads a file, so a document written
# with the pattern in its body passes it cleanly. These two derivations cover files at rest.

_HYPHEN = chr(0x2D)
# A space, two hyphens, a space. Assembled from `_HYPHEN` rather than typed, the same way the
# runtime gate builds its dash constants with `chr(...)`, so this module's own source stays
# clean of the sequence it forbids. Bare long-option flags are excluded for free: `--verbose`
# has no trailing space inside the match, so naming a CLI flag in prose is not a violation.
_EMDASH_SUBSTITUTE = re.compile(" " + (_HYPHEN * 2) + " ")
# An opening or closing fence, either delimiter, indented or not. Matching the same line
# shape for both is what lets the marker pairing below track a block.
_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})")
_INLINE_SPAN = re.compile(r"`[^`]*`")


def _is_style_sweep_excluded(rel: str) -> bool:
    """The four exclusion CATEGORIES, each with its reason, because a bare list rots.

    Anything tracked, markdown and not named here is guarded by default.
    """
    # Rendered from the graph's ROL nodes and byte-compared against that export by
    # `tests/test_fix5_role_coverage.py`, so a prose edit here reads as drift.
    if rel.startswith("agents/"):
        return True
    # Mechanically appended by the session-end hook's printf, which re-emits the pattern on
    # every append no matter how often the file is swept.
    if rel == ".claude/session-metrics.md":
        return True
    # A LIVE FORMAT SPEC, not documentation. `_FILES_LINE_RE` in
    # `writ/session/approval_workflow.py` parses every plan's `## Files` bullet on that exact
    # separator, and `tests/test_plan_template.py` both asserts the substring and runs the
    # real `_validate_phase_a` against the template itself, so sweeping it would make the
    # shipped template fail the validator it documents.
    if rel == "templates/plan-template.md":
        return True
    # DEFERRED to increment 2, NOT permanently exempt. These paths quote verbatim harness
    # output, including the real "PreToolUse:Agent hook error" string with the separator
    # before "Failed with non-blocking status code", so sweeping them would falsify recorded
    # evidence and break the grep a future triager runs against the real error text.
    if rel.startswith("docs/pressure-runs/"):
        return True
    return False


def _unterminated_fence_line(text: str) -> int | None:
    """The 1-based line of a fence marker that never closes, or None when every fence pairs.

    THE UNSCANNABLE PREDICATE, kept separate from the scan so the decision to refuse is one
    readable line at the call site. "No finding" and "could not read the tail" have to be
    distinguishable from the caller's own behavior rather than from a separate probe someone
    remembers to run: an unclosed fence is an ordinary markdown typo, and this guard exists
    for files nobody has written yet.
    """
    open_at: int | None = None
    for number, line in enumerate(text.split("\n"), start=1):
        if _FENCE.match(line):
            open_at = None if open_at is not None else number
    return open_at


def _prose_lines(text: str) -> list[str]:
    """CLOSED fenced blocks and inline code spans blanked IN PLACE, one entry per source line.

    Blanked rather than deleted so reported line numbers stay true: a fence above a
    violation must not shift the line the violation is reported on, which is the same reason
    `_blanked_lines` above exists.

    ONLY A CLOSED FENCE EXEMPTS ITS BODY. Pairing markers, instead of toggling a flag, is
    what keeps an unclosed fence from blanking every remaining line in the file: the earlier
    toggle turned one stray fence marker into a scanner that returned [] for a real violation
    in the tail and read as clean. The exemption is a property of a COMPLETE block, so an
    incomplete one degrades to prose here rather than to silence.

    That degradation is a SECOND line of defense, not the primary one. `emdash_substitute_hits`
    refuses such a file outright through `_unterminated_fence_line`, because a clean tail after
    an unclosed fence would otherwise still look clean. This helper is written so that a future
    caller which skips that check gets over-reporting rather than under-reporting, which is the
    safe direction for a ratchet.

    Split on "\\n" and NOT with `str.splitlines()`, which also breaks on U+2028 and would
    report every line after one of those off by one.
    """
    lines = text.split("\n")
    fenced: set[int] = set()
    open_index: int | None = None
    for index, line in enumerate(lines):
        if not _FENCE.match(line):
            continue
        if open_index is None:
            open_index = index
        else:
            fenced.update(range(open_index, index + 1))
            open_index = None
    if open_index is not None:
        # The unterminated marker itself is still a marker, so it carries no prose; its body
        # is deliberately left OUT of `fenced` and scanned.
        fenced.add(open_index)
    return [
        "" if index in fenced else _INLINE_SPAN.sub("", line)
        for index, line in enumerate(lines)
    ]


def style_swept_docs(*, repo: Path = REPO) -> list[str]:
    """The guarded documentation population: every TRACKED markdown path, minus four
    categories, as repo-relative strings.

    THE POPULATION COMES FROM `git ls-files`, NEVER FROM A FILESYSTEM WALK, and that is a
    correctness requirement rather than a preference. Both search tools available when this
    was written are wrong in opposite directions: the shell's wrapped grep silently DROPPED
    `templates/plan-template.md`, the highest-risk path in the sweep, and the agent-side
    search tool omits the whole `bible/` tree on a root search while silently INCLUDING
    gitignored content (`RESUME.md`, the root `plan.md`, nine `plan-*.ARCHIVED.md`: about 355
    lines). An unscoped recursive walk also reaches `.venv/lib/python3.12/site-packages`,
    which inflated one measurement from 2,406 to 5,696. Tracked-ness is the only definition
    of "ours" that neither over- nor under-counts, so `tests/test_doc_style_ratchet.py` pins
    that a known-gitignored path carrying the pattern is absent from this list.

    NEW DOCUMENTATION IS IN THE POPULATION BY DEFAULT. That default is what makes this a
    ratchet instead of a snapshot; the exclusions below are categories with reasons, never an
    enumeration of today's files.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    out: list[str] = []
    for rel in listed.split("\0"):
        if not rel or not rel.endswith(".md") or _is_style_sweep_excluded(rel):
            continue
        # Tracked AND still on disk. An index entry for a file deleted but not yet
        # committed would otherwise raise on read, turning a scan into an error.
        if (repo / rel).is_file():
            out.append(rel)
    return sorted(out)


def emdash_substitute_hits(
    *, docs: list[Path] | None = None, repo: Path = REPO
) -> list[tuple[Path | str, int]]:
    """(path, 1-based line) for every double-hyphen em-dash substitute in PROSE. Must be
    empty for the derived population, which is the ratchet.

    AN UNSCANNABLE FILE RAISES; IT NEVER RETURNS []. A fence that never closes is the one
    construct here that can suppress arbitrarily many lines, and the earlier toggle turned a
    single stray marker into a scanner that blanked the rest of the file, so a real violation
    in the tail returned [] and read as clean. Silence is the one failure mode this guard
    cannot have, so the file is refused loudly instead, with the opener's line in the message.

    WHY LOUD REFUSAL RATHER THAN SCANNING THE TAIL AS PROSE (both were on the table): an
    unterminated fence whose tail happens to be clean would still return [] under the
    scan-the-tail shape, so the defect would be invisible in exactly the case where nothing
    else flags it. Refusal is detectable from this function's own behavior for EVERY
    unterminated fence, not only the ones that happen to hide an occurrence. The cost, stated
    rather than hidden: the scan stops at the first unscannable file instead of reporting all
    of them, which is acceptable because the fix (close the fence) is unambiguous and is also
    the fix the markdown itself needs.

    CODE SPANS ARE EXEMPT BY PROPERTY, NOT BY A LIST ENTRY. Closed fenced blocks and inline
    spans are blanked before the scan, exactly as
    `hooks/scripts/writ-comms-output-gate.sh:96-98` already does for the reply channel. That
    is what exempts a doc which documents this very rule by quoting the pattern in backticks,
    with no exclusion entry and no staleness: the carve-out is a consequence of the rule, so
    every future doc gets it too.

    `docs` is a keyword so the scanner's precision can be pinned against synthetic files
    under `tmp_path`, the same parameterization `envelope_emitting_scripts(*, scripts_dir=)`
    uses. It also keeps the exemption tests independent of any live document's wording,
    which the standing directive against asserting on documentation prose requires.

    KNOWN LIMITS, stated rather than worked around. The population is markdown, so
    `writ.toml.example` is swept by this cycle but not guarded by it; widening to other
    extensions pulls in source comments and string literals, which are load-bearing. And
    only the double-hyphen substitute is scanned, not the em dash and en dash glyphs the
    runtime gate also refuses, because those two populations were never measured here.
    """
    if docs is None:
        docs = [repo / rel for rel in style_swept_docs(repo=repo)]
    findings: list[tuple[Path | str, int]] = []
    for doc in docs:
        text = Path(doc).read_text(encoding="utf-8", errors="replace")
        unterminated = _unterminated_fence_line(text)
        if unterminated is not None:
            raise ValueError(
                f"cannot scan {doc} for em-dash substitutes: the fence opened at line "
                f"{unterminated} never closes, so no line after it can be read as prose or "
                f"as code. Close the fence, or delete the stray marker; until then this file "
                f"cannot be certified clean and must not be reported as such."
            )
        for number, line in enumerate(_prose_lines(text), start=1):
            if _EMDASH_SUBSTITUTE.search(line):
                findings.append((doc, number))
    return sorted(findings, key=lambda finding: (str(finding[0]), finding[1]))


def route_tuples() -> list[tuple[str, str]]:
    """The live (method, path) set from the FastAPI app.

    Same derivation as `test_server_split_seam.py::_current_route_tuples`, which is where
    the frozen baseline is reviewed; that file keeps its baseline LIST as the review gate and
    no longer restates its length as a number.
    """
    from writ.server import app

    return sorted(
        (method, route.path)
        for route in app.routes
        for method in (getattr(route, "methods", None) or [""])
    )


# ── The write-gate regression population (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482) ──
#
# The cycle that shipped as e881c6d had four regressions from one cause: its Verification
# section listed the regression modules BY HAND, and the list omitted
# `tests/test_mode_infrastructure.py::TestModeCanWrite` and
# `tests/test_phase3_centralization.py::TestCanWrite`, the two modules whose entire subject
# is the can-write decision. Nothing could have noticed, because the population was not
# derived from the property. These three derivations derive it.
#
# TWO ARMS PLUS A UNION, and the split is mechanical rather than aesthetic. A caller wants
# the union: one list of files for one pytest process. The arms stay separately observable
# because the guards in `test_count_pin_discipline.py` get their discriminating power from
# that. Several modules match BOTH arms today, so in a single merged population a mutation
# that broke one whole arm's pattern set would leave those dual-matched modules in place,
# and a floor or witness assertion made against the merged set would still pass with half
# the population silently gone.
#
# THE PREDICATE IS AUTHORED; ONLY THE POPULATION IS DERIVED, which is the same distinction
# `_REFUSAL_PATTERNS` above already draws: a derivation derives the POPULATION from the
# source tree, and its predicate is code. So every authored alternative carries its own
# executable witness on a synthetic tree, and no alternative can be deleted silently.
#
# NO COUNT IS PINNED ANYWHERE. This population grows with every new test module, and a
# count pin on a growing population is the exact breakage this module exists to delete.
_CAN_WRITE_SURFACE = re.compile(r"_can_write_check|cmd_can_write|call_can_write|can-write")
_WRITE_GATE_HOOKS = re.compile(
    r"writ-bash-write-gate|pre-write-check|writ-state-write-gate|writ-pre-write-dispatch"
)


def _matching_test_modules(pattern: re.Pattern[str], tests_dir: Path) -> list[str]:
    """Collected test modules whose RAW text matches, as sorted relative POSIX paths.

    RAW TEXT: comments are not blanked and docstrings are not stripped, and the polarity is
    inverted from every other scanner in this module. CHECK 1, CHECK 2, CHECK 3 and the
    style ratchet report VIOLATIONS, where a comment match is a false alarm that blocks a
    correct change, which is why `_refusal_markers` and `_blanked_source` exist for them.
    This population SELECTS TESTS TO RUN: a false positive costs a few seconds of runtime,
    a false negative costs a shipped regression, and four shipped regressions are the reason
    these derivations exist. So `_blanked_source` is deliberately NOT reused here, and
    `test_count_pin_discipline.py::TestCommentAndDocstringMentionsCountTowardMembership`
    holds that decision as an executable assertion rather than as prose.

    `rglob`, NOT `glob`. `pyproject.toml` sets no `testpaths` and no `python_files`, so
    pytest collects on the default `test_*.py` pattern, which reaches the two test
    sub-packages (`tests/firedrill/`, `tests/plugin/`). Narrowing to `glob` is a
    one-character edit that silently drops both while leaving 60-odd members in place, so
    non-emptiness, the synthetic per-pattern cases and the import oracle would all stay
    green; the subdirectory floor is the only guard that sees it.

    `test_*.py` is also what keeps this file OUT of its own populations, structurally rather
    than through an exclusion entry: `_inventory.py` is not a module pytest collects, so the
    file that defines the patterns cannot be pulled in by defining them. Non-test helpers
    under `tests/` are excluded by that same glob even when they name a pattern
    (`tests/firedrill/_census.py` names `writ-state-write-gate` twice).

    Paths come back relative to `tests_dir.parent`, so the default yields the repo-relative
    `tests/test_bash_write_gate.py` that a caller can paste straight into a pytest
    invocation, and a synthetic tree under `tmp_path` still yields a readable relative path
    instead of raising on a directory outside the repo.
    """
    root = Path(tests_dir).parent
    return sorted(
        path.relative_to(root).as_posix()
        for path in Path(tests_dir).rglob("test_*.py")
        if pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    )


def can_write_surface_modules(*, tests_dir: Path = TESTS) -> list[str]:
    """Test modules that name the can-write decision surface.

    FOUR AUTHORED ALTERNATIVES, and they name the real surface rather than a guess at it:
    `writ/session/gates.py` holds the single decision function `_can_write_check` (whose
    docstring says it is "Used by both cmd_can_write (CLI) and /pre-write-check (HTTP
    endpoint)"), `cmd_can_write` in the same module is the CLI entry,
    `writ/session/cli_dispatch.py` registers the `can-write` subcommand, and
    `tests/fixtures/session_state.py` exposes `call_can_write` as the shared in-process
    caller.

    THIS ARM HAS AN INDEPENDENT ORACLE, and the hook arm cannot have one. Fixtures here are
    imported by name per module rather than registered in a root conftest, which
    `tests/fixtures/session_state.py` states as this repo's convention, so the dependency is
    carried by a resolvable import statement.
    `test_count_pin_discipline.py::_call_can_write_importers` therefore witnesses membership
    by resolving `ast.ImportFrom` nodes, a mechanism this regex does not share, instead of
    restating the scan it checks.

    THE RESIDUAL HOLE, stated because nothing in the derivation keeps it closed: a test that
    reached the gate through a fixture whose name matches no alternative would be a FALSE
    NEGATIVE. Measured when this was written, the hole is empty: `tests/conftest.py`,
    `tests/plugin/conftest.py` and `tests/firedrill/conftest.py` define no can-write fixture.

    `tests_dir` is a keyword for the reason `envelope_emitting_scripts(*, scripts_dir=)` and
    `style_swept_docs(*, repo=)` give: the precision of the matching gets pinned per
    alternative against synthetic fixtures under `tmp_path`, never against a real module's
    current wording.
    """
    return _matching_test_modules(_CAN_WRITE_SURFACE, tests_dir)


def write_gate_hook_modules(*, tests_dir: Path = TESTS) -> list[str]:
    """Test modules that name a write-gate hook script or the endpoint one of them calls.

    `writ-pre-write-dispatch` is a MEASURED correction to the three-string set an earlier
    hand scan used, not a defensive addition. It is the PreToolUse hook Claude Code invokes
    on every Write and Edit, 26 test modules name it, and fifteen of those were in neither
    recorded scan, including `tests/test_pre_write_dispatch_line_split.py` and
    `tests/test_debug_gating.py`: precisely the modules a write-gate change should run.
    With it, the union reconciles with the recorded measurement of 71 modules, which the
    three-string set does not (it gives 56).

    TWO CANDIDATE DERIVATIONS FOR THIS PREDICATE WERE REJECTED, both wrong in a way that
    reads as rigorous:

    1. Derive the names from `hooks/hooks.json` matchers containing `Write`, `Edit` or
       `NotebookEdit`. Wrong direction twice over: a matcher describes the TOOL, not whether
       the script consults a write decision, so it admits `validate-design-doc.sh`,
       `validate-test-file.sh` and `pre-validate-file.sh` while MISSING
       `writ-bash-write-gate.sh`, which is registered on `Bash`. The population would blur
       from "the write gate" to "anything on the write path" and still lose the arm's first
       member.
    2. Derive them from hook source naming the CLI subcommand or the route
       (`hooks/scripts/*.sh` matching `can-write` or `pre-write-check`). That yields
       `writ-bash-write-gate.sh` and `writ-pre-write-dispatch.sh` only, and drops
       `writ-state-write-gate.sh`, which decides Write and Edit refusals with its own logic
       and never names the can-write surface. Under-inclusion is the failure mode these
       derivations exist to remove.

    NO ORACLE IS POSSIBLE FOR THIS ARM, and saying so is better than dressing up a
    tautology. Test modules reach these hook scripts by spawning a path built from a string,
    so the only machine-readable trace of that dependency IS the string this scan looks for.
    An assertion written with the same regex would restate it. A floor is the strongest
    instrument available here, which is why the directory floor exists and why each
    alternative is witnessed separately on a synthetic tree instead.

    A WITNESS FLOOR MADE OF REAL MODULE NAMES was rejected on measurement: the natural
    witness for `writ-state-write-gate` is `tests/test_gate_token_protection.py`, whose
    docstring also names `writ-bash-write-gate`, so dropping the state-gate alternative
    would leave that witness in the population and the floor would pass. Each synthetic
    fixture names exactly one alternative, so it has no such masking.
    """
    return _matching_test_modules(_WRITE_GATE_HOOKS, tests_dir)


def write_gate_regression_modules(*, tests_dir: Path = TESTS) -> list[str]:
    """The caller-facing population: every test module a write-gate change should run.

    A SET UNION OF THE TWO ARMS, never a third regex combining all eight alternatives. The
    merged regex returns the same members today and would silently diverge the moment either
    arm changed, so this is computed FROM the arms and
    `test_count_pin_discipline.py::TestUnionEqualsTheSetUnionOfBothArms` pins the
    RELATIONSHIP rather than a number. That contract earns its place: a union that quietly
    returned a single arm passes non-emptiness, passes every synthetic per-pattern case and
    passes the import oracle, so nothing else would see it.

    The union exists so a caller never has to know there are two arms. One line:

        .venv/bin/python -c "import tests._inventory as inv; print(' '.join(inv.write_gate_regression_modules()))"

    WHAT THIS CANNOT DO, stated rather than implied: nothing here forces a future plan to
    CALL it, and a derivation whose only consumer is a plan's prose is dead code. What it
    does is make the population self-defending, so the answer cannot be silently narrow when
    it IS called, and make the call one line so a hand-written list has no excuse. The teeth
    are the four guards in `test_count_pin_discipline.py` (per-pattern precision, the import
    oracle, the subdirectory floor, the union contract), not enforcement.
    """
    return sorted(
        set(can_write_surface_modules(tests_dir=tests_dir))
        | set(write_gate_hook_modules(tests_dir=tests_dir))
    )


# ── Exec-boundary payload census (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482) ──
#
# The ADR's own census rotted in one cycle: it claimed "one other site with this
# shape" and "every other match passes its payload on stdin or passes bounded
# values", and verification found both halves false. This derivation replaces the
# prose with a MECHANISM: a payload-derived value is one that flows from the hook's
# payload interface (the identifiers `load_hook_env` defines, `HOOK_ENVELOPE` /
# `HOOK_COMMAND` / `HOOK_FILE_PATH`, plus any variable a script assigns from a bare
# `$(cat)` of its own stdin) into a `python3` invocation's argv or env, directly or
# through a chain of ordinary bash assignments. `tests/test_exec_boundary_census.py`
# holds the population found here against an authored MAP of site to (status,
# reason), so a site that disappears, or a `fixed` entry the detector still sees
# crossing, reddens that test rather than this one.
_EXB_PY3_TOKEN = re.compile(r"(?<![\w./-])python3(?![\w.-])")
_EXB_HEREDOC_MARK = re.compile(r"<<-?\s*(?P<q>['\"]?)(?P<delim>\w+)(?P=q)")
_EXB_BARE_CAT_ASSIGN = re.compile(
    r"^\s*(?:local\s+|export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=\$\(\s*cat\s*\)\s*$"
)
_EXB_ASSIGN_TARGETS = re.compile(
    r"(?:^\s*|[;&|(]\s*|&&\s*|\|\|\s*)(?:local\s+|export\s+)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\+?=(?!=))"
)
_EXB_PRINTF_V = re.compile(r"\bprintf\s+-v\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")
_EXB_MAPFILE = re.compile(r"\bmapfile\s+(?:-t\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")
_EXB_PIPE_SEP = re.compile(r"(?<!\|)\|(?!\|)")
# The three names `load_hook_env` defines that carry MODEL-CONTROLLED, unbounded
# text: the whole envelope, the Bash command, and the resolved write path. The
# other names that function sets (HOOK_SESSION_ID, HOOK_TOOL_NAME, ...) are
# internally bounded (session ids are capped at 128 characters) and are not roots.
_EXB_PAYLOAD_ROOTS = ("HOOK_ENVELOPE", "HOOK_COMMAND", "HOOK_FILE_PATH")


def _exb_var_ref(name: str) -> re.Pattern[str]:
    return re.compile(r"\$\{?" + re.escape(name) + r"\b")


def _exb_payload_roots(source: str) -> set[str]:
    roots = {n for n in _EXB_PAYLOAD_ROOTS if _exb_var_ref(n).search(source)}
    for line in source.split("\n"):
        m = _EXB_BARE_CAT_ASSIGN.match(line)
        if m:
            roots.add(m.group("name"))
    return roots


def _exb_assignment_targets(line: str) -> set[str]:
    """Every variable NAME a single line assigns, across the three assignment
    shapes this tree actually uses: `NAME=...`, `printf -v NAME`, `mapfile NAME`.
    Anchored so a python source line living INSIDE a `-c "..."` string (`data =
    json.loads(raw)`, indented, `=` surrounded by spaces) never matches: bash
    assignment has no space before `=`, and the `^` branch requires the name at
    the line's own start (after only whitespace), which an indented python
    statement is not.
    """
    names = {m.group("name") for m in _EXB_ASSIGN_TARGETS.finditer(line)}
    names |= {m.group("name") for m in _EXB_PRINTF_V.finditer(line)}
    names |= {m.group("name") for m in _EXB_MAPFILE.finditer(line)}
    return names


def _exb_dash_c_span_end(lines: list[str], start: int) -> int:
    """The last line of a (possibly multi-line) double-quoted argument opened at
    `lines[start]`: extends while the accumulated text carries an ODD count of
    unescaped `"`, the same continuation `_emission_end` above tracks for a
    `python3 -c` block. A no-op (`end == start`) when the line's own quotes
    already balance, so calling this unconditionally on every assignment-opening
    line is safe."""
    text = lines[start]
    end = start
    last = len(lines) - 1
    while len(re.findall(r'(?<!\\)"', text)) % 2 == 1 and end < last:
        end += 1
        text += "\n" + lines[end]
    return end


def _exb_assignment_spans(lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) 0-based indices for every assignment STATEMENT. `end` extends
    past `start` only when that line itself opens an assignment (so an unrelated
    multi-line quote elsewhere in the file can never merge two unrelated spans)."""
    spans: list[tuple[int, int]] = []
    n = len(lines)
    i = 0
    while i < n:
        end = _exb_dash_c_span_end(lines, i) if _exb_assignment_targets(lines[i]) else i
        spans.append((i, end))
        i = end + 1
    return spans


def _exb_derived_vars(source: str, roots: set[str]) -> set[str]:
    """Transitive closure over assignment statements: a name joins the derived set
    when its OWN assignment statement (its opening line, plus any `-c "..."`
    continuation it opens) references a root or an already-derived name. Iterates
    to a fixed point, so a value copied through several plain assignments is still
    traced -- this tree's own write door needs it: `writ-pre-write-dispatch.sh`'s
    `DISPATCH_BLOB` reads `$CHECK_BODY`, itself three assignments removed from the
    `STDIN_DATA` root (`STDIN_DATA` -> `PARSED_INPUT` -> `_PARSE_LINES` ->
    `CHECK_BODY`), not one.
    """
    lines = source.split("\n")
    spans = _exb_assignment_spans(lines)
    derived: set[str] = set()
    changed = True
    while changed:
        changed = False
        watched = roots | derived
        if not watched:
            break
        watched_re = re.compile(
            r"\$\{?(?:" + "|".join(re.escape(w) for w in sorted(watched, key=len, reverse=True)) + r")\b"
        )
        for start, end in spans:
            names: set[str] = set()
            for ln in lines[start:end + 1]:
                names |= _exb_assignment_targets(ln)
            names -= watched
            if not names:
                continue
            text = "\n".join(lines[start:end + 1])
            if watched_re.search(text) and not names <= derived:
                derived |= names
                changed = True
    return derived


def _exb_trim_before_pipe(text: str) -> str:
    """Drop everything up to and including the LAST single `|` in `text`: a value
    piped INTO python3 (`echo "$HOOK_ENVELOPE" | python3 -c "..."`) crosses to
    `echo`'s argv, not python3's, and is a different site with a different fix."""
    matches = list(_EXB_PIPE_SEP.finditer(text))
    if not matches:
        return text
    return text[matches[-1].end():]


def _exb_heredoc_skip(lines: list[str], body_start: int, delim: str) -> int:
    """The index AFTER a heredoc's terminator line. Applied to EVERY heredoc this
    scan meets, quoted or not: a heredoc body is the payload's STDIN destination,
    which the ADR treats as the unbounded, safe channel regardless of what it
    interpolates, so this derivation does not descend into it looking for a nested
    crossing (a documented limit, not an oversight -- see the module return value's
    docstring)."""
    j = body_start
    while j < len(lines) and lines[j].strip() != delim:
        j += 1
    return j + 1


def exec_boundary_payload_sites(*, scripts_dir: Path = HOOK_SCRIPTS_DIR) -> dict[str, dict[str, object]]:
    """Every `python3` invocation in `scripts_dir` whose ARGV or ENV carries a
    payload-derived value, as `"<script>:<line>"` -> `{"script", "line"}`.

    `scripts_dir` is a keyword for the reason `envelope_emitting_scripts(*,
    scripts_dir=)` gives: the detector's precision is pinned against synthetic
    fixtures under `tmp_path` in `tests/test_exec_boundary_census.py`, never against
    a real script's current wording. Call it a second time with `scripts_dir=REPO /
    "bin" / "lib"` to reach `common.sh`, which sits outside `hooks/scripts/` (the
    ADR's own census sentence was scoped to that directory and missed
    `common.sh:1811` for exactly that reason); the two calls are never merged
    inside this function; a caller wanting both composes the two dicts.

    KNOWN LIMIT, stated rather than worked around: a value that crosses only
    inside an UNQUOTED heredoc's own nested command substitution is not traced,
    because every heredoc body here is skipped uniformly regardless of quoting
    (see `_exb_heredoc_skip`).

    THE EXAMPLE THIS PARAGRAPH USED TO NAME IS GONE, and the retirement is the
    point rather than housekeeping. It named `writ-memory-policy-guard.sh`'s
    inner `python3 -c "...sys.argv[1]..." "$CONTENT"`, spliced into an outer
    `<<PY` body, and said the site was catalogued in the census by file:line
    because this derivation could not see it. That splice was a LIVE
    decision-flipping fail-open (an oversized memory write allowed silently),
    and it has been removed: the content now goes on a pipe and the program to
    `bin/lib/memory-policy-scan.py`. Naming a fixed site as the standing example
    of an open limit is how a docstring starts arguing the opposite of the truth,
    so the limit keeps its statement and loses its instance.

    The limit is no longer the only thing standing between that shape and a
    green run either: `nested_program_splice_sites` below DERIVES exactly this
    mechanism (an unquoted interpreter heredoc whose body interpolates), which is
    what a hand-catalogued file:line could never do. The two derivations are
    deliberately separate: this one answers "does a payload cross an exec
    boundary", that one answers "does bash rewrite the program before the
    interpreter reads it", and a site can be either without being both.
    """
    sites: dict[str, dict[str, object]] = {}
    for path in sorted(Path(scripts_dir).glob("*.sh")):
        source = path.read_text(encoding="utf-8", errors="replace")
        lines = source.split("\n")
        roots = _exb_payload_roots(source)
        if not roots:
            continue
        watched = sorted(roots | _exb_derived_vars(source, roots), key=len, reverse=True)
        if not watched:
            continue
        watched_re = re.compile(r"\$\{?(?:" + "|".join(re.escape(w) for w in watched) + r")\b")
        n = len(lines)
        i = 0
        while i < n:
            text = lines[i]
            end = i
            while text.endswith("\\") and end < n - 1:
                end += 1
                text = text[:-1] + " " + lines[end]
            if text.lstrip().startswith("#"):
                i = end + 1
                continue
            py_match = _EXB_PY3_TOKEN.search(text)
            heredoc = _EXB_HEREDOC_MARK.search(text)
            if heredoc and (not py_match or py_match.start() < heredoc.start()):
                if py_match:
                    before = _exb_trim_before_pipe(text[:py_match.start()])
                    after = text[py_match.start():heredoc.start()]
                    if watched_re.search(before + after):
                        sites[f"{path.name}:{i + 1}"] = {"script": path.name, "line": i + 1}
                i = _exb_heredoc_skip(lines, end + 1, heredoc.group("delim"))
                continue
            if py_match:
                span_end = _exb_dash_c_span_end(lines, end)
                before = _exb_trim_before_pipe(text[:py_match.start()])
                after = text[py_match.start():]
                if span_end != end:
                    after += "\n" + "\n".join(lines[end + 1:span_end + 1])
                if watched_re.search(before + after):
                    sites[f"{path.name}:{i + 1}"] = {"script": path.name, "line": i + 1}
                i = span_end + 1
                continue
            i = end + 1
    return sites


# ── Nested-program splice sites (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3) ──
#
# The mechanism the memory-policy-guard cycle removes: an UNQUOTED interpreter heredoc
# (`python3 <<PY`, not `<<'PY'`) whose BODY bash itself expands before python ever starts,
# because an unquoted heredoc delimiter undergoes the same expansions as double-quoted
# text. A `$(...)` inside the body is a nested command substitution spliced into the
# outer program's TEXT (the live bug this cycle fixes, `writ-memory-policy-guard.sh:52`
# and `:71`); a bare `$VAR` or `${VAR}` is the LATENT hazard the plan names but does not
# ship a fix for, because no shipped pattern contains one today. Both are the same
# MECHANISM -- text the shell rewrites before the interpreter reads it -- so one detector
# finds both.
#
# ENUMERATED BY HAND FIRST, then derived, matching this module's own convention
# (`can_write_surface_modules`'s docstring states the same discipline): `hooks/scripts/*.sh`
# holds exactly three unquoted `python3 <<DELIM` sites whose body interpolates --
# `writ-memory-policy-guard.sh` lines 52 and 71 (both fixed this cycle) and
# `writ-pressure-audit.sh:19` (recorded, not fixed; see the map in
# `tests/test_exec_boundary_census.py`). `bin/lib/*.sh` holds none. MEASURED (not
# reasoned): running this derivation over both roots before this cycle's implementation
# lands returns exactly those three keys and nothing else.
_NPS_HEREDOC_MARK = re.compile(r"<<-?\s*(?P<q>['\"]?)(?P<delim>\w+)(?P=q)")
_NPS_PY_TOKEN = re.compile(r"(?<![\w./-])python3(?![\w.-])")
# A `${`, a `$(`, a backtick, or a bare `$VAR` (a `$` immediately followed by an
# identifier-starting character). Deliberately NOT a bare trailing `$`: bash cannot start
# an expansion on a `$` with nothing after it, so it survives an unquoted heredoc
# unchanged, and a detector that flagged it would report a hazard that cannot fire --
# exactly the distinction the plan draws between the live splice and the latent one.
_NPS_INTERPOLATION = re.compile(r"\$\{|\$\(|`|\$[A-Za-z_][A-Za-z0-9_]*")


def nested_program_splice_sites(*, scripts_dir: Path = HOOK_SCRIPTS_DIR) -> dict[str, dict[str, object]]:
    """Every unquoted interpreter heredoc whose body interpolates, as `"<script>:<line>"`
    -> `{"script", "line"}`, the line being the heredoc's OPENING line (where `python3`
    and `<<DELIM` both appear on the same logical line), not a line inside the body.

    `scripts_dir` is a keyword for the reason `exec_boundary_payload_sites(*,
    scripts_dir=)` gives: the detector's precision is pinned against synthetic fixtures
    under `tmp_path` in `tests/test_exec_boundary_census.py`, never against a real
    script's current wording. Call it a second time with `scripts_dir=REPO / "bin" /
    "lib"` to cover that root too; the two calls are never merged inside this function,
    matching `exec_boundary_payload_sites`'s own split.

    A HEREDOC WHOSE DELIMITER IS QUOTED (`<<'PY'`, `<<"PY"`) IS NEVER FLAGGED, no matter
    what its body contains: bash does not expand anything inside a quoted heredoc, so a
    `$(` or `$VAR` there is inert text, exactly as it would be inside a single-quoted
    string. This is the same distinction `_exb_heredoc_skip` above states it does NOT
    need to draw, because that derivation skips every heredoc body uniformly regardless
    of quoting; this one exists BECAUSE the quoting is exactly what matters here.

    ONLY A `python3` INVOCATION IS IN SCOPE. The population this cycle enumerated by
    hand is three `python3 <<DELIM` sites and nothing else; widening to every
    interpreter this tree might one day heredoc into is unmeasured and left for that
    day, matching `can_write_surface_modules`'s own "residual hole, stated because
    nothing keeps it closed" discipline.
    """
    sites: dict[str, dict[str, object]] = {}
    for path in sorted(Path(scripts_dir).glob("*.sh")):
        lines = _blanked_lines(path.read_text(encoding="utf-8", errors="replace"))
        n = len(lines)
        i = 0
        while i < n:
            line = lines[i]
            heredoc = _NPS_HEREDOC_MARK.search(line)
            if heredoc and _NPS_PY_TOKEN.search(line):
                delim = heredoc.group("delim")
                quoted = bool(heredoc.group("q"))
                body_start = i + 1
                j = body_start
                while j < n and lines[j].strip() != delim:
                    j += 1
                body = "\n".join(lines[body_start:j])
                if not quoted and _NPS_INTERPOLATION.search(body):
                    sites[f"{path.name}:{i + 1}"] = {"script": path.name, "line": i + 1}
                i = j + 1
                continue
            i += 1
    return sites


# ── Daemon-starting hooks (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3) ──
#
# The production side of the daemon-leak cycle. A hook that can reach a daemon launch and
# does not consult `WRIT_NO_AUTOSTART` will spawn a real daemon on whatever WRIT_PORT it
# was handed, which is how a throwaway port ends up squatted by a live daemon for hours.
#
# TWO LAUNCH SHAPES, and they are mechanisms rather than names: the shared flock-guarded
# entry point `writ_ensure_server` (`scripts/lib/writ-server-lib.sh`), and a bare
# `nohup ... serve`, which is what that entry point does internally and what a hook would
# have to write for itself to bypass it.
#
# THE SCOPE BOUNDARY `hooks/scripts/` IS STRUCTURAL, NOT AN EXEMPTION LIST. Those are the
# scripts Claude Code invokes automatically on events, so a launch there fires without
# anybody asking for it; `scripts/bootstrap.sh` and `scripts/ensure-server.sh` exist
# BECAUSE an operator asked for a daemon, and guarding them would break their only job.
#
# ENUMERATED BY HAND FIRST, then derived, the discipline `can_write_surface_modules`
# states: the population today is `writ-rag-inject.sh` (guarded, the check on line 48)
# and `session-start-bootstrap.sh` (its `writ_ensure_server` on line 107 was the unguarded
# one this cycle closed). `writ-worktree-safety.sh` and `writ-bash-write-gate.sh` both
# name `nohup` in a wrapper-prefix table and neither can launch anything, which is why the
# `nohup` alternative requires a `serve` token on the same command.
_DSH_LAUNCH_PATTERNS = (
    re.compile(r"\bwrit_ensure_server\b"),
    re.compile(r"\bnohup\b.*\bserve\b"),
)
_DSH_GUARD = re.compile(r"\bWRIT_NO_AUTOSTART\b")
_DSH_IF = re.compile(r"^\s*if\b")
_DSH_ELIF = re.compile(r"^\s*elif\b")
_DSH_ELSE = re.compile(r"^\s*else\b")
_DSH_FI = re.compile(r"^\s*fi\b")


def _dsh_launch_match(block: str) -> re.Match[str] | None:
    for pattern in _DSH_LAUNCH_PATTERNS:
        match = pattern.search(block)
        if match:
            return match
    return None


def _dsh_guard_verdict(source: str) -> bool | None:
    """None when this script cannot reach a daemon launch; otherwise whether EVERY launch
    it can reach is conditioned on `WRIT_NO_AUTOSTART`.

    THE GUARD IS READ AS A MECHANISM, NOT AS A MENTION OF THE NAME. A launch counts as
    guarded when an OPEN `if`/`elif` condition around it tests the variable, or when the
    variable is tested earlier in the launch's own command list (the one-line
    `[ -z "${WRIT_NO_AUTOSTART:-}" ] && writ_ensure_server` shape). Naming the variable in
    a comment cannot satisfy it: whole-line comments are blanked by `_blanked_lines`, and a
    trailing comment sits AFTER the launch token, where this predicate does not look. An
    `else` branch clears the condition it belongs to rather than inheriting it, because a
    launch reached only when the guard IS set is the opposite of guarded.

    Command spans come from `_emission_end`, so a launch on the third line of a
    backslash-continued command (`writ-rag-inject.sh` writes it that way) is found, and
    the `if` bookkeeping is not thrown off by shell keywords appearing inside a heredoc
    body or a `python3 -c "..."` block.

    KNOWN OVER-REPORTING, MEASURED rather than reasoned, and stated in the direction it
    really fails. A launch token inside a heredoc BODY is matched, not skipped:
    `_emission_end` folds the body into the same block as the command that opens it and
    this scan searches the whole block, so an inert `nohup something serve --port 1 &`
    sitting in a python string inside `<<'PY'` is classified as a real launch and, having
    no conditional around it, reads as UNGUARDED. Measured against a synthetic script:
    `{'probe-heredoc.sh': False}`, and the same for a `writ_ensure_server` token in the
    same position. An earlier revision of this paragraph claimed the opposite ("not seen
    at all"), which was wrong in the more dangerous direction: it promised a MISS where
    the code produces a FALSE POSITIVE.

    The real population is unaffected today, because both of its entries carry a genuine
    launch and both read True, so this is a false-positive surface for a FUTURE hook
    rather than a live miscount.
    `tests/test_daemon_leak_guard.py::TestDaemonStartingHooksReadsIntoHeredocBodies` pins
    the behaviour so the next person to add an interpreter heredoc meets a documented
    property instead of a mystery red. Teaching the walk to genuinely skip a quoted
    heredoc body is a change to the block scan itself, which this cycle's plan does not
    cover.
    """
    lines = _blanked_lines(source)
    conditions: list[str] = []
    verdicts: list[bool] = []
    index = 0
    while index < len(lines):
        end = _emission_end(lines, index)
        block = "\n".join(lines[index:end + 1])
        opening = lines[index]
        match = _dsh_launch_match(block)
        if match:
            verdicts.append(
                bool(_DSH_GUARD.search(block[:match.start()]))
                or any(_DSH_GUARD.search(condition) for condition in conditions)
            )
        if _DSH_IF.match(opening):
            conditions.append(block)
        elif conditions and _DSH_ELIF.match(opening):
            conditions[-1] = block
        elif conditions and _DSH_ELSE.match(opening):
            conditions[-1] = ""
        elif conditions and _DSH_FI.match(opening):
            conditions.pop()
        index = end + 1
    if not verdicts:
        return None
    return all(verdicts)


def daemon_starting_hooks(*, scripts_dir: Path = HOOK_SCRIPTS_DIR) -> dict[str, bool]:
    """Every hook script that can reach a daemon launch, mapped to whether that launch
    consults `WRIT_NO_AUTOSTART`.

    A MAP, NOT A BOOLEAN OVER THE TREE, and that is the load-bearing choice. A hook that
    DECAYS (keeps its launch, loses its guard) stays in the population with a False value
    and fails the completeness assertion by name, where a whole-tree AND would report the
    same red for any cause and a filtered list of "the unguarded ones" would let a hook
    drop out of the population silently by losing its launch instead of gaining a guard.

    `scripts_dir` is a keyword for the reason `envelope_emitting_scripts(*, scripts_dir=)`
    gives: the derivation's precision is pinned against synthetic fixtures under
    `tmp_path` in `tests/test_daemon_leak_guard.py`, never against the real tree's current
    wording alone. That module also asserts this map NON-EMPTY against the real
    `hooks/scripts/`, because a derivation that silently returned `{}` would make the
    completeness assertion beside it pass on any tree.
    """
    out: dict[str, bool] = {}
    for path in sorted(Path(scripts_dir).glob("*.sh")):
        verdict = _dsh_guard_verdict(path.read_text(encoding="utf-8", errors="replace"))
        if verdict is not None:
            out[path.name] = verdict
    return out


# ── Raw Bash tokenizer sites (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3) ──
#
# `shlex.split(text, posix=False)` ends a token at the closing quote of a span that
# started it, so one shell word arrives as two tokens and every consumer downstream reads
# a fragment. `writ/session/bash_tokens.py` calls itself SINGLE SOURCE but never calls
# shlex: it post-processes an already-split list, so the RAW call is duplicated in both
# hooks OUTSIDE the mirror markers, where the mirror tests cannot see it.
#
# KEYED ON THE CALL, NOT ON A SCRIPT NAME. The population is every hook script that makes
# the call at all, and the value is whether EVERY such call is wrapped. A hook that keeps
# its call and loses the wrap reads False and fails by name.
#
# WHOLE-LINE COMMENTS ARE BLANKED, because both hooks DESCRIBE the call in prose several
# times (writ-bash-write-gate.sh names it on lines 553, 1101 and 1328) and a mention is
# not a call.
#
# KNOWN FAIL-CLOSED LIMIT, stated in the direction it really fails: the wrap must be on
# the SAME line as the call. A call broken across lines reads as UNWRAPPED, which reports
# a False for a site that may be correct. That is a demand to look, never a silent pass,
# and both production call sites are written on one line for exactly this reason.
#
# A MENTION IN A DOCSTRING IS NOT A CALL EITHER, and blanking `#` comments does not reach
# one. The shared MIRROR block documents the very defect this guard exists for, so its
# `rejoin_glued_words` docstring SHOWS the raw call three times (`writ-bash-write-gate.sh`
# lines 1541, 1546 and 1548; `writ-worktree-safety.sh` 398, 403 and 405). Those lines are
# python string data, not `#` comments, so the comment blanking above leaves them standing
# and BOTH hooks read False while both real call sites are correctly wrapped. MEASURED,
# which is why this is written as a mechanism and not as a list of line numbers.
#
# SO THE PREDICATE READS CODE THROUGH `ast`, the standard `python_shlex_split_callers`
# below already sets ("so the call SHAPE is what counts"). The hooks' python lives inside
# `python3 <<'PY'` heredocs, so each body is extracted, parsed, and every STRING LITERAL
# it contains becomes a PROSE span; a regex match inside a prose span is a mention and is
# skipped. Shell cannot call `shlex.split` at all, so scoping to python bodies loses no
# real call site.
#
# EXCLUDING THE MIRROR SPAN WOULD HAVE BEEN THE WRONG FIX, and the reason is measured
# rather than argued: the zero-import contract holds for the standalone module
# `writ/session/bash_tokens.py`, NOT for the two pasted hook copies. In
# `writ-bash-write-gate.sh` the heredoc opens at line 1166, its first line is
# `import os, re, shlex, sys`, and the mirror block runs INSIDE that same program, so
# `shlex` is in scope there at runtime and a real call added inside the markers would
# execute normally. Skipping the span would convert today's false POSITIVE into a
# permanent blind spot, which is the worse direction.
#
# FAIL-CLOSED AT EVERY UNCERTAINTY, stated in the direction it really fails: a heredoc
# body that does not parse as python contributes NO prose spans, so every match inside it
# counts as a CALL and the script reads False. A guard that cannot tell demands a look; it
# never passes quietly. `_blanked_lines` is REUSED here (never modified: five other guards
# call it and its docstring pins line-number stability) to find heredoc OPENERS, because
# an opener named inside a comment hijacks the scan otherwise. MEASURED: the prose at
# `writ-bash-write-gate.sh` line 220 names `cat <<'EOF' > docs/notes.txt`, and scanning
# raw lines started a body there that swallowed the rest of the file, leaving the real
# program unparsed and both verdicts False for the wrong reason.
_BTS_CALL = re.compile(r"shlex\.split\(")
_BTS_WRAP = "rejoin_glued_words("
_BTS_HEREDOC = re.compile(r"<<-?\s*(?P<q>['\"]?)(?P<word>[A-Za-z_][A-Za-z0-9_]*)(?P=q)")


def _bts_python_bodies(source: str) -> list[tuple[str, int]]:
    """(body text, index of the body's FIRST line) for every heredoc in `source`.

    Openers are looked for in the COMMENT-BLANKED view and bodies are sliced from the RAW
    one, so a documented opener cannot start a body while a real body keeps its exact
    text. Both delimiter spellings are accepted (`<<'PY'` in the hooks, bare `<<PY` in the
    synthetic fixtures) and an unterminated heredoc simply runs to end of file.
    """
    lines = source.split("\n")
    openers = _blanked_lines(source)
    bodies: list[tuple[str, int]] = []
    i = 0
    while i < len(lines):
        match = _BTS_HEREDOC.search(openers[i])
        if match is None:
            i += 1
            continue
        word = match.group("word")
        start = i + 1
        end = start
        while end < len(lines) and lines[end].strip() != word:
            end += 1
        bodies.append(("\n".join(lines[start:end]), start))
        i = end + 1
    return bodies


def _bts_prose_spans(source: str) -> dict[int, list[tuple[int, int]]]:
    """Line index -> the (start col, end col) ranges on it that are python STRING DATA.

    Only bodies that PARSE contribute, which is the fail-closed half: an unparsed body
    yields nothing here, so its mentions count as calls.
    """
    spans: dict[int, list[tuple[int, int]]] = {}
    for body, offset in _bts_python_bodies(source):
        try:
            tree = ast.parse(body)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if node.end_lineno is None:
                continue
            for lineno in range(node.lineno, node.end_lineno + 1):
                low = node.col_offset if lineno == node.lineno else 0
                high = node.end_col_offset if lineno == node.end_lineno else sys.maxsize
                spans.setdefault(offset + lineno - 1, []).append((low, high))
    return spans


def _bts_verdict(source: str) -> bool | None:
    """None when this script never calls shlex.split; otherwise whether EVERY call it
    makes is nested inside a rejoin_glued_words(...) call on the same line.

    A match that sits inside a `#` comment or inside a python string literal is a MENTION
    and does not enter the verdict at all, so a script that only DESCRIBES the call stays
    out of the population rather than reading False.
    """
    spans = _bts_prose_spans(source)
    verdicts: list[bool] = []
    for index, line in enumerate(_blanked_lines(source)):
        for match in _BTS_CALL.finditer(line):
            if any(low <= match.start() < high for low, high in spans.get(index, ())):
                continue
            verdicts.append(_BTS_WRAP in line[:match.start()])
    if not verdicts:
        return None
    return all(verdicts)


def bash_token_split_sites(*, scripts_dir: Path = HOOK_SCRIPTS_DIR) -> dict[str, bool]:
    """Every hook script that tokenizes Bash text with `shlex.split`, mapped to whether
    every one of its calls is wrapped by the adjacency repair.

    `scripts_dir` is a keyword for the reason `daemon_starting_hooks(*, scripts_dir=)`
    gives: the derivation's precision is pinned against SYNTHETIC scripts under `tmp_path`
    in tests/test_bash_quote_adjacency_gate.py, never against the real tree's current
    wording alone. That module also asserts this map NON-EMPTY against the real
    hooks/scripts/, because a derivation that silently returned {} would make the
    completeness assertion beside it pass on any tree.
    """
    out: dict[str, bool] = {}
    for path in sorted(Path(scripts_dir).glob("*.sh")):
        verdict = _bts_verdict(path.read_text(encoding="utf-8", errors="replace"))
        if verdict is not None:
            out[path.name] = verdict
    return out


def python_shlex_split_callers(*, package_dir: Path = REPO / "writ") -> dict[str, int]:
    """Every module under `writ/` that CALLS shlex.split, mapped to how many calls it
    makes. EMPTY today, and that is the point: the map above is scoped to hooks/scripts/,
    so without this a future tokenizer added under writ/ would not merely read False, it
    would never be in a population at all. Read through `ast`, so the call SHAPE is what
    counts and the several docstring mentions in writ/session/bash_tokens.py (lines 3, 7
    and 86) are not miscounted as calls.
    """
    out: dict[str, int] = {}
    for path in sorted(Path(package_dir).rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        count = 0
        for node in ast.walk(tree):
            func = getattr(node, "func", None)
            if (isinstance(node, ast.Call)
                    and isinstance(func, ast.Attribute) and func.attr == "split"
                    and isinstance(func.value, ast.Name) and func.value.id == "shlex"):
                count += 1
        if count:
            out[str(path.relative_to(REPO))] = count
    return out


# ── Daemon-reachability skip sites (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3) ──
#
# THE CONVENTION THIS EXISTS TO STOP REGROWING: a test class that skips because "the Writ
# server is unreachable" on the SUITE port, which `tests/conftest.py` forces and which
# `pytest_sessionstart` deliberately starts nothing on, so every test behind that skip has
# never executed once. Ten such modules were converted in one cycle, and before the
# population was derived it had been hand-listed three times in one session with a
# different answer each time: a site can produce three runtime skips (three parametrized
# cases), or two (an import-time class mark), or ZERO (a start that succeeds), so a census
# of what FIRED is not the population.
#
# IT KEYS ON THE ADDRESS. Not on a class name (`...Live` is the symptom, and a name-keyed
# guard is the defect this repo has already paid for), not on the reason string, and not on
# `/health` either: `tests/test_phase15_companion_endpoint.py` reached its skip through an
# `except (URLError, OSError)` around `/methodology-companion` and never probed health at
# all, so a health-keyed detector missed it. Eight differently-named local predicates
# (`_server_up`, `_test_daemon_up`, `_daemon_up`, `_health`, `_port_busy`,
# `daemon_cache_dir`, `_daemon_health`, `_ensure_aligned_daemon`) resolved to that ONE
# address, which is the argument for keying on what a condition RESOLVES to rather than on
# what it is called.
#
# WHAT IT MUST NOT CATCH, and the discrimination is MECHANICAL rather than a reason string.
# Roughly forty other sites skip on "Neo4j unreachable" and every one is legitimate:
# `tests/_corpus.py` states the posture (unreachable is the only legitimate skip, and
# reachable-but-empty must FAIL), and a daemon differs from a database only in that a test
# can START one. A Neo4j condition resolves through `tests/_corpus.py::neo4j_reachable` to
# `writ.config.get_neo4j_uri()`, and a driver exception carries no HTTP address, so neither
# reaches the suite address. Three further non-members, each measured against the source
# rather than read off its wording: a socket BIND failure from `HTTPServer(("127.0.0.1",
# free_port()), ...)`, a missing-TOOL `skipif(shutil.which(...) is None)`, and an absent
# optional dependency. `tests/test_daemon_skip_ownership.py` plants the first two of those
# on a synthetic tree and asserts NON-membership, so a future widening reddens immediately
# instead of quietly enrolling forty legitimate skips.
_DAEMON_HELPER = TESTS / "_daemon.py"

# THE TWO ALTERNATIVES a reaching condition can resolve THROUGH, and both are names rather
# than addresses because an address is what they produce. `_port` is the live resolver in
# `tests/_daemon.py` (a function, not a constant, so the suite's port is honoured whenever
# the module was imported); `TEST_DAEMON_PORT` is the constant `tests/conftest.py` assigns
# it from, imported by `tests/test_daemon_test_port.py` today. The suite port LITERAL is
# read from that same constant's VALUE below and is never spelled here, so a change to the
# suite port cannot leave a stale digit in this detector.
_SUITE_ADDRESS_RESOLVERS = ("_port", "TEST_DAEMON_PORT")

# The three owners that already exist, matched over RAW module text for the reason
# `write_gate_hook_modules` gives for its own arm: a module's owner is a DEPENDENCY it
# carries, and the only machine-readable trace of it is the name it imports. The SITE is a
# code SHAPE and is read off the AST instead, because a mention in prose is not a skip.
_SANCTIONED_OWNER_MARKERS = (
    "start_isolated_daemon",
    "tests._hook_runner",
    "tests._prompt_turn",
    "tests.fixtures.server_routes",
)

# A module that reaches a daemon START without a sanctioned owner. Its integration tests DO
# execute, so its defect is a different one (four copies of one hand-rolled start on the
# SUITE port, all four sharing one defective stop, all four retired by plan.md
# 2412ba38-51e1-4b73-895b-7b240a3c21d3) and it is not the unowned arm.
_SELF_STARTED_MARKERS = (
    "ensure-server.sh",
    "writ_ensure_server",
    "ensure_daemon_aligned",
    "start_test_daemon",
)

# `pytest.skip` and `pytest.mark.skipif` in the spellings the suite actually writes, plus
# the bare `from pytest import skip` form. Matched on the dotted call name so a `self.skip`
# or an unrelated `.skipif` attribute on some other object cannot enter the population.
_SKIP_CALLS = ("pytest.skip", "skip")
_SKIPIF_CALLS = ("pytest.mark.skipif", "mark.skipif", "skipif")


def _suite_port_literals() -> set[object]:
    """The suite port as both a string and an int, from `tests.conftest.TEST_DAEMON_PORT`.

    IMPORTED INSIDE THE FUNCTION, not at module top level, for the same reason
    `tests/_daemon.py` and `tests/_hook_runner.py` keep a stdlib-only top level: importing
    the root conftest applies the suite's env isolation, and `import tests._inventory` is a
    one-liner an operator runs outside pytest (see `write_gate_regression_modules`).

    BOTH TYPES, because a module can name the port either way: `tests/_daemon.py::_port`
    returns the string form out of the environment, while a module that binds its own
    constant writes the int (`ALT_PORT = <suite port>`, the shape
    `tests/test_fix2_cache_alignment.py` carried until plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3 moved its four self-heal tests onto an
    OS-assigned port). NO LIVE MODULE BINDS SUCH A CONSTANT TODAY, so the int arm is
    kept for the next one rather than for a current member: a resolver narrowed to the
    string form would go quiet on the shape it was written for.
    """
    from tests.conftest import TEST_DAEMON_PORT

    return {TEST_DAEMON_PORT, int(TEST_DAEMON_PORT)}


def _daemon_probe_names(*, helper: Path = _DAEMON_HELPER) -> set[str]:
    """Every function in `tests/_daemon.py` whose call reaches the port resolver.

    DERIVED BY SCANNING THAT MODULE, never typed as a list, which is the difference between
    a probe set that stays true and one that goes stale the next time a helper is added
    there. Transitive: `daemon_cache_dir` -> `_daemon_health` -> `_health_url` -> `_port`
    is four hops and every one of them is a suite-address probe, while `_isolated_health`,
    `start_isolated_daemon` and `stop_isolated_daemon` take a port as an ARGUMENT and reach
    the resolver at no depth, which is exactly why an owned isolated daemon's start-failure
    skip is not a suite-address site.
    """
    tree = ast.parse(Path(helper).read_text(encoding="utf-8", errors="replace"))
    called: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            called[node.name] = {
                child.func.id
                for child in ast.walk(node)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
            }
    reaching = {name for name in _SUITE_ADDRESS_RESOLVERS if name in called}
    changed = True
    while changed:
        changed = False
        for name, callees in called.items():
            if name not in reaching and callees & reaching:
                reaching.add(name)
                changed = True
    return reaching


def _dotted_call_name(func: ast.AST) -> str:
    parts: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _referenced_names(node: ast.AST) -> set[str]:
    """Every identifier an expression or body mentions, as bare names.

    ATTRIBUTES CONTRIBUTE THEIR LAST SEGMENT, so `self._ensure_aligned_daemon(...)` resolves
    through the method of that name. That was how
    `tests/test_methodology_companion_orchestrator.py` reached its skip, and a Name-only walk
    would have missed it; plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3 converted that module
    onto a sanctioned owner, so the rule has no live subject today and is kept for the next
    class-based module that dispatches its probe through a method.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
    return names


def _names_the_suite_port(node: ast.AST, literals: set[object]) -> bool:
    """Whether a subtree contains the suite port as a literal.

    EXACT EQUALITY against the two forms, so a docstring that merely mentions the port in a
    sentence is not a match: the constant compared against is the whole string, not a
    substring of the prose around it.
    """
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and not isinstance(child.value, bool):
            try:
                if child.value in literals:
                    return True
            except TypeError:
                continue
    return False


def _resolves_to_suite_address(
    node: ast.AST, names: set[str], literals: set[object]
) -> bool:
    return bool(_referenced_names(node) & names) or _names_the_suite_port(node, literals)


def _suite_address_names(
    tree: ast.Module, probes: set[str], literals: set[object]
) -> set[str]:
    """Every name IN ONE MODULE that resolves to the suite address, to a fixpoint.

    Resolution goes through what the plan calls one level and what the code has to compute
    as a closure, because the levels compose in real modules: a name imported from
    `tests/_daemon.py`, a `def` (at any nesting, including a method) whose body mentions
    such a name, and an assignment whose VALUE mentions one, e.g.
    `SERVER = f"http://localhost:{_port()}"` and then `_server_up()` reading SERVER and then
    `up = _server_up()`. The loop runs to a fixpoint rather than in source order, so a
    helper defined below its caller resolves the same as one defined above it.

    A LOCAL ASSIGNMENT COUNTS, and it has to: the condition at the skip is often a plain
    local (`started_daemon = self._ensure_aligned_daemon(...)`, then
    `if not started_daemon:`), and requiring a module-level binding would have missed three
    of the four self-started modules this rule was derived from, all four since converted by
    plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in probes:
                    names.add(alias.asname or alias.name)
    definitions: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append(([node.name], node))
        elif isinstance(node, ast.Assign):
            definitions.append(
                ([t.id for t in node.targets if isinstance(t, ast.Name)], node.value)
            )
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = [node.target.id] if isinstance(node.target, ast.Name) else []
            definitions.append((target, node.value))
    changed = True
    while changed:
        changed = False
        for targets, body in definitions:
            pending = [name for name in targets if name not in names]
            if not pending:
                continue
            if _resolves_to_suite_address(body, names, literals):
                names.update(pending)
                changed = True
    return names


def _skip_call_lines(body: list[ast.stmt]) -> set[int]:
    lines: set[int] = set()
    for statement in body:
        for child in ast.walk(statement):
            if isinstance(child, ast.Call) and _dotted_call_name(child.func) in _SKIP_CALLS:
                lines.add(child.lineno)
    return lines


def _daemon_skip_site_lines(
    tree: ast.Module, names: set[str], literals: set[object]
) -> list[int]:
    """The 1-based line of every skip whose REACHING CONDITION is the suite address.

    TWO RECOGNIZED SHAPES, because both occur in the tree:

    1. An `if <probe>` guard, or a `skipif(<probe>)` evaluated at import time (a decorator,
       a `pytestmark`, or a mark bound to a module constant and applied to a class).
    2. A `pytest.skip` inside an `except` handler around a request to that address. This is
       the shape a health-keyed detector cannot see, and the one
       `tests/test_phase15_companion_endpoint.py` carried.

    THE EXCEPTION TYPES ARE NOT PART OF THE PREDICATE. What makes the second shape a member
    is that the `try` body reaches the suite address; keying on `URLError`/`OSError` would
    be keying on a name again, and a handler catching bare `Exception` around the same
    request is the same defect.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            if _resolves_to_suite_address(node.test, names, literals):
                lines |= _skip_call_lines(node.body) | _skip_call_lines(node.orelse)
        elif isinstance(node, ast.Try):
            handled = set()
            for handler in node.handlers:
                handled |= _skip_call_lines(handler.body)
            if handled and any(
                _resolves_to_suite_address(statement, names, literals)
                for statement in node.body
            ):
                lines |= handled
        elif isinstance(node, ast.Call) and _dotted_call_name(node.func) in _SKIPIF_CALLS:
            arguments: list[ast.AST] = list(node.args) + [kw.value for kw in node.keywords]
            if any(
                _resolves_to_suite_address(argument, names, literals)
                for argument in arguments
            ):
                lines.add(node.lineno)
    return sorted(lines)


def _daemon_skip_owner(source: str) -> str | None:
    """`"sanctioned"` | `"self-started"` | None for a module that carries a site.

    SANCTIONED WINS when a module carries both markers, which is the plan's own definition
    ("self-started: reaches a daemon START but NOT through a sanctioned owner"). It was
    derived from a measured case rather than chosen: `tests/test_fix2_cache_alignment.py`
    took the shared owner for one converted test while four self-heal tests still hand-rolled
    a suite-port start, so its owner verdict described the MODULE while the self-started
    finding was recorded against the start mechanism those copies shared. plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3 moved that module's start onto an OS-assigned port,
    so the precedence rule now has no live subject and stays for the next module that mixes
    the two.
    """
    if any(marker in source for marker in _SANCTIONED_OWNER_MARKERS):
        return "sanctioned"
    if any(marker in source for marker in _SELF_STARTED_MARKERS):
        return "self-started"
    return None


def daemon_reachability_scanned_modules(*, tests_dir: Path = TESTS) -> list[str]:
    """Every test module the detector below WALKS, matched or not, as POSIX paths relative
    to `tests_dir` ITSELF (the same keying `daemon_reachability_skip_sites` uses).

    ONE WALK, TWO VIEWS. `daemon_reachability_skip_sites` ITERATES this list rather than
    re-deriving its own glob, so the matched map and this population cannot disagree about
    what was scanned. A second `rglob` beside the first would be a MODEL of the walk, and a
    model is blind to the walk it models.

    IT EXISTS BECAUSE THE NON-VACUITY FLOOR HAD TO MOVE, and that re-base is not a
    weakening. The floor protects exactly one fact: an emptiness verdict over the unowned
    arm came from a scan that LOOKED. It used to prove that by requiring the matched map
    non-empty, which requires some live module to STAY defective; plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3 converted the last four members, so the matched map
    is now `{}` on the real tree by success and a floor resting on a match would have gone
    red for the right reason. Resting on the scan instead, it still catches every way the
    detector can go blind (a narrowed glob, a wrong default `tests_dir`, a scan that reaches
    nothing) and survives the population being FIXED.
    """
    root = Path(tests_dir)
    return [path.relative_to(root).as_posix() for path in sorted(root.rglob("test_*.py"))]


def daemon_reachability_skip_sites(
    *, tests_dir: Path = TESTS
) -> dict[str, dict[str, object]]:
    """Every collected test module carrying a suite-port-gated skip, as
    `"<module>"` -> `{"owner": "sanctioned" | "self-started" | None, "sites": [<line>, ...]}`.

    A MAP, NOT A LIST OF OFFENDERS, and that is the load-bearing choice rather than a
    convenience. A module that keeps its skip site and loses its owner (a decayed
    conversion, or a new `TestSomethingLive` written next month) MOVES INTO the unowned arm
    and fails BY NAME, where a filtered list of "the unowned ones" would let it drop out of
    the population silently. `tests/test_daemon_skip_ownership.py` holds that property by
    deleting a planted module's owner import and rescanning.

    THE UNOWNED ARM (`owner is None`) IS THE DEFECT: the module probes an address and starts
    nothing, so every test behind the site has never executed. The two owned states are not
    defects here, and each has its own reason: a sanctioned module's skip is a start-failure
    carrying the daemon's own stated reason, and a self-started module's tests DO run.

    NON-EMPTINESS IS SOMEONE ELSE'S ASSERTION, deliberately. This returns `{}` for a tree
    with no qualifying module, including an empty one, because an emptiness assertion over
    the unowned arm passes just as well on a scan that matched NOTHING; the floor that makes
    it non-vacuous lives beside it in `tests/test_daemon_skip_ownership.py` and rests on
    `daemon_reachability_scanned_modules`, because after plan.md
    2412ba38-51e1-4b73-895b-7b240a3c21d3 this map is `{}` ON THE REAL TREE BY SUCCESS: the
    four modules that populated it were converted, so a floor keyed on a MATCH would demand
    a live module stay defective forever.

    Keys are POSIX paths relative to `tests_dir` ITSELF (not its parent, unlike
    `_matching_test_modules`, whose callers paste them into a pytest invocation), so a
    synthetic tree under `tmp_path` keys on the bare filename planted there.

    THE WALK IS `daemon_reachability_scanned_modules`'s, `rglob("test_*.py")` for the reason
    `_matching_test_modules` records: pytest collects on that default pattern and it reaches
    `tests/firedrill/` and `tests/plugin/`, and it is also what keeps this module and every
    non-test helper under `tests/` out of the population structurally rather than by an
    exclusion entry.
    """
    probes = set(_SUITE_ADDRESS_RESOLVERS) | _daemon_probe_names()
    literals = _suite_port_literals()
    root = Path(tests_dir)
    sites: dict[str, dict[str, object]] = {}
    for module in daemon_reachability_scanned_modules(tests_dir=root):
        source = (root / module).read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
        names = _suite_address_names(tree, probes, literals)
        lines = _daemon_skip_site_lines(tree, names, literals)
        if not lines:
            continue
        sites[module] = {
            "owner": _daemon_skip_owner(source),
            "sites": lines,
        }
    return sites


# ── The D2 ratchet (plan.md 2412ba38-51e1-4b73-895b-7b240a3c21d3, decision 6) ──
#
# Four modules hand-rolled a daemon stop as `pkill -f "writ serve --port N"`, a literal that
# matches exactly ONE of the three launch spellings `scripts/lib/writ-server-lib.sh:93-99`
# can produce, so a miss left a daemon squatting the port every later module resolves. All
# four copies are retired; this is what stops a fifth, because retiring a population and
# leaving nothing to stop its regrowth is how this one grew in the first place.
#
# IT KEYS ON THE MECHANISM, NOT ON THE PATTERN STRING. A command list whose FIRST element is
# the constant "pkill" is a process kill being spawned, however its `-f` argument is spelled,
# so the next hand-rolled stop is a member before anyone reads its regex. It deliberately
# does NOT catch `shutil.which("pkill")`, which is a missing-TOOL probe and stays legitimate
# in `tests/test_fix2_cache_alignment.py`'s class mark; that discrimination is planted on a
# synthetic tree in `tests/test_daemon_skip_ownership.py` rather than argued.
#
# THE WALK IS `rglob("*.py")`, NOT `test_*.py`, and the difference is load-bearing rather
# than incidental: `tests/_daemon.py` is not a collected module and is the ONE legitimate
# spawner (it builds its pattern from `_serve_pattern` and VERIFIES the result through
# `_wait_for_isolated_down` instead of trusting the signal), so a collected-modules-only
# glob would structurally miss the only member the real tree is supposed to have.
_PKILL_PROGRAM = "pkill"


def _pkill_command_lines(tree: ast.Module) -> list[int]:
    """The 1-based line of every list or tuple literal whose first element is `"pkill"`."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = node.elts[0]
            if isinstance(first, ast.Constant) and first.value == _PKILL_PROGRAM:
                lines.add(node.lineno)
    return sorted(lines)


def pkill_invocation_sites(*, tests_dir: Path = TESTS) -> dict[str, list[int]]:
    """Every module under `tests/` that SPAWNS `pkill`, as `"<module>"` -> `[<line>, ...]`.

    Keys are POSIX paths relative to `tests_dir` ITSELF, the same keying
    `daemon_reachability_skip_sites` uses, so a synthetic tree under `tmp_path` keys on the
    bare filename planted there.

    THE ASSERTION BELONGS ON THE KEY SET, NEVER ON THE LINE NUMBERS. The lines travel so a
    failure can say where to look, but any edit to `tests/_daemon.py` moves them, and a
    guard that reddens on an unrelated edit is a guard someone deletes.
    """
    root = Path(tests_dir)
    sites: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        lines = _pkill_command_lines(tree)
        if lines:
            sites[path.relative_to(root).as_posix()] = lines
    return sites
