"""Phase 6 hotfix: defensive json.loads(sys.argv[N]) in writ hook scripts.

Background: PSR-006 surfaced uncaught json.JSONDecodeError tracebacks
visible in Claude Code's UI on every Write. Root cause: writ-rag-inject.sh
and writ-pretool-rag.sh pass JSON-encoded rule_id arrays via shell argv
to inline `python3 -c` blocks, then call `json.loads(sys.argv[N])`.
When upstream content occasionally contains an embedded control char
(literal newline inside a string value), json.loads raises and the
traceback bubbles to the user.

Tests verify the defensive pattern:
  1. Bug reproduction -- the unguarded pattern crashes on bad input.
  2. Recovered pattern -- with try/except + stderr diagnostic, bad
     input no longer crashes; fallback is empty list; stderr carries
     the [writ-hook json.loads recovery] marker so future debugging
     has a trail.
  3. Hook scripts on disk -- each affected `json.loads(sys.argv[N])`
     callsite is wrapped in try/except in production.

The fix lives in the bash hook scripts (inline python heredocs); these
tests assert pattern presence + behavior, not Python-module behavior.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

WRIT_ROOT = Path(__file__).resolve().parent.parent
HOOKS = WRIT_ROOT / "hooks" / "scripts"

# The marker every defensive recovery writes to stderr.
RECOVERY_MARKER = "[writ-hook json.loads recovery]"

# The bad-JSON input that triggers the bug: a control char inside a string.
BAD_JSON_PAYLOAD = '["A\nB"]'


# --- Bug reproduction (sanity check that the bug class is real) ------------


class TestBugReproduction:
    """Confirms the unguarded json.loads(sys.argv[N]) pattern crashes
    exactly as PSR-006's debug log showed. Anchors the bug class so the
    fix tests have a concrete failure mode to guard against."""

    def test_unguarded_pattern_crashes_on_bad_json(self) -> None:
        """A literal control char inside a JSON string triggers
        json.JSONDecodeError -- non-zero exit + stderr traceback."""
        script = "import json, sys\nprint(json.loads(sys.argv[1]))"
        proc = subprocess.run(
            ["python3", "-c", script, BAD_JSON_PAYLOAD],
            capture_output=True, text=True, timeout=5,
        )
        assert proc.returncode != 0, "unguarded pattern should crash"
        assert "JSONDecodeError" in proc.stderr
        assert "Invalid control character" in proc.stderr


# --- Recovered-pattern behavior --------------------------------------------


_DEFENSIVE_BLOCK = """
import json, sys
try:
    ids = json.loads(sys.argv[1])
except (json.JSONDecodeError, ValueError) as _e:
    sys.stderr.write(
        f"[writ-hook json.loads recovery] argv[1] in test: {_e}\\n"
        f"  len={len(sys.argv[1])} sample={sys.argv[1][:200]!r}\\n"
    )
    ids = []
print(repr(ids))
"""


class TestRecoveredPatternBehavior:
    """The defensive pattern: try/except around json.loads, stderr
    diagnostic on failure, [] fallback. Successful inputs unchanged."""

    def test_bad_input_does_not_crash(self) -> None:
        proc = subprocess.run(
            ["python3", "-c", _DEFENSIVE_BLOCK, BAD_JSON_PAYLOAD],
            capture_output=True, text=True, timeout=5,
        )
        assert proc.returncode == 0, (
            f"defensive block must exit 0 on bad input; stderr={proc.stderr!r}"
        )

    def test_bad_input_falls_back_to_empty_list(self) -> None:
        proc = subprocess.run(
            ["python3", "-c", _DEFENSIVE_BLOCK, BAD_JSON_PAYLOAD],
            capture_output=True, text=True, timeout=5,
        )
        assert "[]" in proc.stdout, (
            f"defensive block must print [] on bad input; got {proc.stdout!r}"
        )

    def test_bad_input_writes_recovery_marker_to_stderr(self) -> None:
        proc = subprocess.run(
            ["python3", "-c", _DEFENSIVE_BLOCK, BAD_JSON_PAYLOAD],
            capture_output=True, text=True, timeout=5,
        )
        assert RECOVERY_MARKER in proc.stderr, (
            f"defensive block must log {RECOVERY_MARKER!r} on bad input"
        )

    def test_bad_input_logs_length_and_sample(self) -> None:
        proc = subprocess.run(
            ["python3", "-c", _DEFENSIVE_BLOCK, BAD_JSON_PAYLOAD],
            capture_output=True, text=True, timeout=5,
        )
        assert f"len={len(BAD_JSON_PAYLOAD)}" in proc.stderr
        assert "sample=" in proc.stderr

    def test_good_input_parses_unchanged(self) -> None:
        good = '["A", "B", "C"]'
        proc = subprocess.run(
            ["python3", "-c", _DEFENSIVE_BLOCK, good],
            capture_output=True, text=True, timeout=5,
        )
        assert proc.returncode == 0
        assert "['A', 'B', 'C']" in proc.stdout
        assert RECOVERY_MARKER not in proc.stderr


# --- Hook-source pattern audit ---------------------------------------------


# THE POPULATION IS DERIVED, NEVER LISTED.
#
# It used to be a hardcoded list naming only the hooks PSR-006 happened to touch.
# A hardcoded list fails in both directions: it went RED when
# writ-rag-inject.sh legitimately lost its last argv callsite (plan dfacff61 moved
# that work server-side), and it was silently BLIND to every other hook that has
# one. Deriving it means a hook with no callsite is simply not in the population,
# and a hook that GAINS one is covered the moment it does, with nothing to remember
# to update.
#
# What the derivation found on 2026-09-01, which the list had been hiding: FIVE
# hooks carry an argv callsite, and validate-rules.sh's was completely unguarded.
# Its block points both stdout and stderr at the user's stderr (`2>&1 >&2`), so a
# malformed cache blob printed a raw traceback in the UI: PSR-006 itself, still
# live in a hook nobody had listed. It is guarded now.
_ARGV_LOADS_RE = re.compile(r"json\.loads\(sys\.argv\[")


def _hook_source(name: str) -> str:
    return (HOOKS / name).read_text()


def _hooks_with_argv_json_loads() -> list[str]:
    """Every hook script whose inline python parses JSON out of shell argv."""
    return sorted(
        path.name for path in HOOKS.glob("*.sh")
        if _ARGV_LOADS_RE.search(path.read_text())
    )


_ARGV_JSON_HOOKS = _hooks_with_argv_json_loads()


# THE GUARD DETERMINATION IS STRUCTURAL, NOT POSITIONAL.
#
# Two earlier drafts of this detector were vacuous, each more narrowly than the
# last, and only mutation found either. Recorded here because the shape repeats:
#
#   Draft 1 tested the FILE. `"try:" in src` reads green on any file that guards
#   something, anything, anywhere. Reverting validate-rules.sh to its unguarded
#   state left the suite green.
#
#   Draft 2 tested PROXIMITY: a `try:` within five lines above and an `except`
#   within eight below. It never checked that the try WRAPS the callsite, so a
#   completely bare callsite sitting between one closed try/except above it and
#   another closed one below it read as guarded. The bash arm had the same defect
#   in a different coordinate: it scanned forward for the first line that looked
#   like a block terminator, so a heredoc block with no protection was credited
#   with the tolerant redirect belonging to a LATER, unrelated inline block.
#
# Neither draft misfired against the five real hooks, which is exactly why each
# would have rotted in silence: the next hook written with ordinary error handling
# nearby gets a free pass. So the question is answered from STRUCTURE now. On the
# python side that is indentation: a callsite is guarded when it sits in the body
# of a `try:` at a strictly smaller indent whose body has not already closed and
# whose matching `except` comes after it. On the bash side it is block identity:
# find the opener that starts the callsite's OWN block, derive that block's
# terminator form from it, and read the protection off the correct line.
#
# Both counterexamples live in TestHooksAreDefensive as permanent fixtures.


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _body_still_open(lines: list[str], try_idx: int, call_idx: int, try_indent: int) -> bool:
    """Is the `try:` body at try_idx still open when line call_idx runs?

    A python suite ends at the first line that dedents to the header's own indent
    or further, which is also where `except` sits. So a single dedent between the
    two means the try closed before the callsite and cannot be wrapping it.
    """
    for k in range(try_idx + 1, call_idx):
        if not lines[k].strip():
            continue
        if _indent(lines[k]) <= try_indent:
            return False
    return True


def _enclosing_try(lines: list[str], i: int) -> int | None:
    """Index of the `try:` whose BODY contains line i, or None.

    Walks outward one enclosing scope at a time: only a line at a strictly
    smaller indent can enclose, and finding one tightens the search to indents
    smaller than IT, so `try:` / `if` / `for` nesting is followed correctly and a
    sibling `try:` at the callsite's own indent is never mistaken for a parent.
    """
    ceiling = _indent(lines[i])
    for j in range(i - 1, -1, -1):
        line = lines[j]
        if not line.strip():
            continue
        ind = _indent(line)
        if ind >= ceiling:
            continue
        if line.strip() == "try:" and _body_still_open(lines, j, i, ind):
            return j
        ceiling = ind
    return None


def _has_matching_except(lines: list[str], try_idx: int, call_idx: int) -> bool:
    """Does the enclosing try's body END in an `except` that runs after the callsite?

    `finally` deliberately does NOT count: it runs the cleanup and re-raises, so a
    malformed payload still surfaces its traceback, which is the whole failure
    mode here.
    """
    try_indent = _indent(lines[try_idx])
    for k in range(call_idx + 1, len(lines)):
        if not lines[k].strip():
            continue
        if _indent(lines[k]) > try_indent:
            continue
        return _indent(lines[k]) == try_indent and lines[k].strip().startswith("except")
    return False


def _python_guarded_at(lines: list[str], i: int) -> bool:
    j = _enclosing_try(lines, i)
    return j is not None and _has_matching_except(lines, j, i)


# `python3 -c "` opens a block that a bash line beginning with the closing quote
# ends; `<<'WORD'` opens one that a line equal to WORD ends. The two are not
# interchangeable, and treating them as one terminator shape is what let a
# heredoc borrow a later block's redirect.
_BLOCK_OPEN_HEREDOC = re.compile(r"<<-?\s*'([A-Za-z_][A-Za-z0-9_]*)'")
_BLOCK_OPEN_INLINE = re.compile(r"python3\s+-c\s+\"")


def _enclosing_block(lines: list[str], i: int) -> tuple[int, str | None, int | None] | None:
    """(opener index, heredoc word or None, closer index or None) for the inline
    python block containing line i, or None when line i is not inside one."""
    opener = None
    word: str | None = None
    for j in range(i - 1, -1, -1):
        heredoc = _BLOCK_OPEN_HEREDOC.search(lines[j])
        if heredoc:
            opener, word = j, heredoc.group(1)
            break
        if _BLOCK_OPEN_INLINE.search(lines[j]):
            opener, word = j, None
            break
    if opener is None:
        return None
    closer = None
    for k in range(i + 1, len(lines)):
        if word is None:
            if lines[k].lstrip().startswith('"'):
                closer = k
                break
        elif lines[k].strip() == word:
            closer = k
            break
    return opener, word, closer


def _bash_guarded_at(lines: list[str], i: int) -> bool:
    """The callsite's OWN block discards its stderr AND tolerates its failure, so
    neither a traceback nor a non-zero exit escapes the hook.

    Which LINE carries that protection depends on the block form, and getting it
    from the wrong line is the defect this replaced. An inline `python3 -c "`
    block carries its redirects on the line that closes the quote, after the
    argv list. A heredoc carries them on the OPENER, because the heredoc word is
    the last token of the command.
    """
    block = _enclosing_block(lines, i)
    if block is None:
        return False
    opener, word, closer = block
    if word is not None:
        guard_line = lines[opener]
    elif closer is not None:
        guard_line = lines[closer]
    else:
        return False
    return "2>/dev/null" in guard_line and "|| true" in guard_line


def _unguarded_argv_callsites(name: str) -> list[int]:
    """1-based line numbers of every argv callsite that is protected by NEITHER
    mechanism.

    TWO mechanisms count, because two are in use and both work. Recognising only
    the python one would fail hooks that are genuinely protected, which is the
    guard-by-name mistake this cycle already hit elsewhere:

      1. python-level: the block wraps the parse in `try:` and recovers.
      2. bash-level: the invocation discards the block's stderr AND tolerates its
         failure, so neither a traceback nor a non-zero exit escapes the hook.

    What does NOT count is an invocation that shows the block's stderr and has no
    python-level guard, which is exactly the state validate-rules.sh was in.
    """
    lines = _hook_source(name).split("\n")
    return [
        i + 1
        for i, ln in enumerate(lines)
        if _ARGV_LOADS_RE.search(ln)
        and not _python_guarded_at(lines, i)
        and not _bash_guarded_at(lines, i)
    ]


class TestHooksAreDefensive:
    """Every `json.loads(sys.argv[...])` callsite in the tree is protected, so a
    malformed payload costs the block's output and never a raw traceback in the
    user's UI.

    MUTATION: dropping the `try:` from writ-web-capture.sh's block, or the
    `2>/dev/null ... || true` from writ-subagent-stop.sh's invocation, turns the
    matching parameter red. Adding a NEW hook with an unguarded callsite turns a
    NEW parameter red without any edit here, which is the whole point of deriving
    the population.

    The old `test_recovery_marker_present` is GONE rather than re-parametrized.
    The marker is one diagnostic idiom, not the contract: four of the five hooks
    in the derived population recover correctly without ever writing it, so
    demanding the string would have failed working code. The contract is that the
    callsite is guarded, which is what this class now asserts. The marker itself
    is still exercised end to end by TestRecoveredPatternBehavior above and is
    still emitted by writ-dispatch-discipline.sh and validate-rules.sh.
    """

    def test_the_population_is_not_empty(self) -> None:
        """Anti-vacuity. A derived population that matched nothing would make
        every parametrized test below vanish and the module read green while
        asserting nothing at all."""
        assert _ARGV_JSON_HOOKS, (
            "no hook script contains json.loads(sys.argv[..]); either the tree "
            "genuinely stopped using the idiom (delete this class) or the "
            "detector regex has drifted"
        )

    @pytest.mark.parametrize("hook_name", _ARGV_JSON_HOOKS)
    def test_every_argv_loads_callsite_is_guarded(self, hook_name: str) -> None:
        src = _hook_source(hook_name)
        assert _ARGV_LOADS_RE.search(src), (
            f"{hook_name} is in the derived population but has no callsite; "
            "the derivation and the assertion disagree"
        )
        unguarded = _unguarded_argv_callsites(hook_name)
        assert unguarded == [], (
            f"{hook_name}: json.loads(sys.argv[..]) is unguarded at line(s) "
            f"{unguarded}. Wrap it in try/except in the python block, or make "
            "the bash invocation discard stderr and tolerate failure. "
            "Unguarded, a malformed payload prints a traceback in the user's "
            "UI (PSR-006)."
        )

    # The detector's own proof, run against synthetic sources rather than the
    # tree, so it holds whatever state the real hooks are in. The UNGUARDED case
    # deliberately carries a try/except a few lines BELOW the callsite, guarding
    # something else: that is the exact shape that made the first draft of this
    # detector read green, because it tested the FILE rather than the CALLSITE.
    _UNGUARDED = (
        'python3 -c "\n'
        "import json, sys\n"
        "cache = json.loads(sys.argv[1])\n"
        "try:\n"
        "    other = open(sys.argv[2]).read()\n"
        "except OSError:\n"
        "    other = ''\n"
        '" "$CACHE" "$F" 2>&1 >&2\n'
    )
    _PYTHON_GUARDED = (
        'python3 -c "\n'
        "import json, sys\n"
        "try:\n"
        "    cache = json.loads(sys.argv[1])\n"
        "except (json.JSONDecodeError, ValueError):\n"
        "    cache = {}\n"
        '" "$CACHE" 2>&1 >&2\n'
    )
    _BASH_GUARDED = (
        'python3 -c "\n'
        "import json, sys\n"
        "cache = json.loads(sys.argv[1])\n"
        '" "$CACHE" 2>/dev/null || true\n'
    )
    # THE TWO COUNTEREXAMPLES THAT BROKE THE PREVIOUS DETECTOR. Permanent, and the
    # reason this class cannot go back to a line window. Both were EXECUTED against
    # the proximity-based draft and both came back "guarded" with nothing wrapping
    # the callsite at all.
    #
    # A bare callsite BRACKETED by two closed, unrelated try/except pairs. The
    # proximity draft saw a `try:` above and an `except` below and said guarded.
    # Nothing here wraps line 7.
    _BRACKETED_BY_UNRELATED_GUARDS = (
        'python3 -c "\n'
        "import json, sys\n"
        "try:\n"
        "    first = open(sys.argv[2]).read()\n"
        "except OSError:\n"
        "    first = ''\n"
        "cache = json.loads(sys.argv[1])\n"
        "try:\n"
        "    second = open(sys.argv[3]).read()\n"
        "except OSError:\n"
        "    second = ''\n"
        '" "$CACHE" "$A" "$B" 2>&1 >&2\n'
    )
    # The same trap one level in, which also proves a SIBLING `try:` at the
    # callsite's own indent is not mistaken for a parent: the try on line 9 starts
    # after line 8 and encloses nothing of it.
    _BRACKETED_INSIDE_A_BLOCK = (
        'python3 -c "\n'
        "import json, sys\n"
        "if sys.argv[1]:\n"
        "    try:\n"
        "        first = open(sys.argv[2]).read()\n"
        "    except OSError:\n"
        "        first = ''\n"
        "    cache = json.loads(sys.argv[1])\n"
        "    try:\n"
        "        second = open(sys.argv[3]).read()\n"
        "    except OSError:\n"
        "        second = ''\n"
        '" "$CACHE" "$A" "$B" 2>&1 >&2\n'
    )
    # The bash arm's twin. The callsite is in an UNPROTECTED heredoc block; the
    # tolerant redirect belongs to a different block further down. The positional
    # draft scanned forward for the first terminator-looking line and credited it.
    _HEREDOC_CREDITED_A_LATER_BLOCK = (
        "ADDS=$(python3 - \"$ENVELOPE\" <<'PY'\n"
        "import json, sys\n"
        "cache = json.loads(sys.argv[1])\n"
        "print(cache)\n"
        "PY\n"
        ")\n"
        'python3 -c "\n'
        "import sys\n"
        "print('a different block entirely')\n"
        '" 2>/dev/null || true\n'
    )
    # The positive control for the heredoc form: protection on the OPENER, which
    # is where a heredoc carries it, must still be recognised.
    _HEREDOC_GUARDED_ON_ITS_OPENER = (
        "python3 - \"$ENVELOPE\" 2>/dev/null <<'PY' || true\n"
        "import json, sys\n"
        "cache = json.loads(sys.argv[1])\n"
        "PY\n"
    )
    # try/finally is NOT a guard: the cleanup runs and the exception re-raises, so
    # the traceback still reaches the user.
    _TRY_FINALLY_IS_NOT_A_GUARD = (
        'python3 -c "\n'
        "import json, sys\n"
        "try:\n"
        "    cache = json.loads(sys.argv[1])\n"
        "finally:\n"
        "    sys.stderr.write('done')\n"
        '" "$CACHE" 2>&1 >&2\n'
    )

    @pytest.mark.parametrize("body,expected_unguarded", [
        (_UNGUARDED, [3]),
        (_PYTHON_GUARDED, []),
        (_BASH_GUARDED, []),
        (_BRACKETED_BY_UNRELATED_GUARDS, [7]),
        (_BRACKETED_INSIDE_A_BLOCK, [8]),
        (_HEREDOC_CREDITED_A_LATER_BLOCK, [3]),
        (_HEREDOC_GUARDED_ON_ITS_OPENER, []),
        (_TRY_FINALLY_IS_NOT_A_GUARD, [4]),
    ], ids=["unguarded", "python-guarded", "bash-guarded",
            "bracketed-by-unrelated-guards", "bracketed-inside-a-block",
            "heredoc-credited-a-later-block", "heredoc-guarded-on-its-opener",
            "try-finally-is-not-a-guard"])
    def test_the_detector_separates_guarded_from_unguarded(
        self, tmp_path, body, expected_unguarded, monkeypatch
    ) -> None:
        planted = tmp_path / "planted.sh"
        planted.write_text(body)
        monkeypatch.setattr(
            "tests.test_phase6_hook_defensive_json._hook_source",
            lambda _name: planted.read_text(),
        )
        assert _unguarded_argv_callsites("planted.sh") == expected_unguarded

    @pytest.mark.parametrize("hook_name", _ARGV_JSON_HOOKS)
    def test_hook_syntax_still_valid(self, hook_name: str) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(HOOKS / hook_name)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, (
            f"{hook_name} bash syntax broken: {proc.stderr}"
        )


# --- Phase 4c stderr-tee preservation --------------------------------------


class TestStderrTeeStillCapturesRecoveries:
    """Phase 4c D1's stderr-tee idiom must continue to forward the recovery
    diagnostics to /tmp/writ-hook-debug.log so future root-cause analysis has a
    trail. dbaa200 GATED that tee behind WRIT_DEBUG (the bare
    `tee -a /tmp/writ-hook-debug.log` literal was replaced by
        exec 2> >(tee -a "$(_writ_debug_enabled \\
            && echo "${WRIT_HOOK_LOG:-/tmp/writ-hook-debug.log}" \\
            || echo /dev/null)" >&2)
    ) in the production hooks and in the sibling test_phase4c_stderr_capture.py,
    but this assertion was missed -- it still grepped the removed bare literal
    and so FAILED at HEAD. This mirrors the sibling's gated-idiom assertion."""

    @staticmethod
    def _has_gated_tee(content: str) -> bool:
        """Match the WRIT_DEBUG-gated stderr-tee idiom by its three stable
        markers instead of the removed bare literal."""
        return (
            "_writ_debug_enabled" in content
            and "tee -a" in content
            and "/tmp/writ-hook-debug.log" in content
        )

    @pytest.mark.parametrize("hook_name", _ARGV_JSON_HOOKS)
    def test_hook_has_stderr_tee_or_inherits_from_dispatch(self, hook_name: str) -> None:
        """Either the hook itself carries the WRIT_DEBUG-gated tee idiom, or it
        is covered by writ-pre-write-dispatch.sh which carries it. writ-rag-inject.sh
        has no tee of its own, so it relies on the dispatch hook's gated idiom
        (which dbaa200 left intact)."""
        dispatch = (HOOKS / "writ-pre-write-dispatch.sh").read_text()
        own = _hook_source(hook_name)
        has_tee = self._has_gated_tee(own) or self._has_gated_tee(dispatch)
        assert has_tee, (
            f"{hook_name}: stderr must reach /tmp/writ-hook-debug.log via the "
            "WRIT_DEBUG-gated tee idiom (_writ_debug_enabled + tee -a + the "
            "/tmp/writ-hook-debug.log fallback path) in its own source or via "
            "writ-pre-write-dispatch.sh"
        )
