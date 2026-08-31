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

import json
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS_JSON = REPO / "hooks" / "hooks.json"
HOOK_SCRIPTS_DIR = REPO / "hooks" / "scripts"

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
    report every line after one off by one: `writ-pre-write-dispatch.sh` line 200 holds a
    literal U+2028 inside a python string.
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
