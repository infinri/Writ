"""Plan 2412ba38-51e1-4b73-895b-7b240a3c21d3: a word out of a python comment
executed by bash on every SubagentStop.

THE DEFECT. `hooks/scripts/writ-subagent-stop.sh:308` opens `python3 -c "`, a
double-quoted bash word that runs to the lone `"` starting line 343. Line 328
holds `# claim `general-purpose` on 2026-08-27 with nothing observed.` --
an UNESCAPED backtick pair, inside a bash double-quoted string, around a word
that is not a command. Bash forks a command substitution for it before python
ever sees the program text: the shell prints
`writ-subagent-stop.sh: line 308: general-purpose: command not found` to
stderr on every SubagentStop dispatch, and python receives the comment with
the word deleted (`claim  on 2026-08-27`). Nothing downstream breaks today --
the block is a comment -- but the mechanism is arbitrary: any future backtick
anywhere in that 35-line block is executed by bash under the operator's uid.
The fix (escaping the two backticks, out of scope for this file: see plan.md
Files) does not change what this suite pins; it only flips this suite from
red to green.

WHAT WOULD MAKE THIS PASS VACUOUSLY, and the test that exists specifically to
foreclose it:

  - `_hook_shell_scripts` resolving to an empty list (a wrong root, a typo'd
    glob suffix) would make every "zero findings" assertion below pass by
    scanning nothing, indistinguishable from "every block is fixed" --
    TestVacuousDetectionGuard exists to catch that, on the REAL tree.
  - `_python_c_blocks` or `_unescaped_backticks` being stubs that always return
    `[]` would make the false-positive controls pass for the wrong reason --
    TestMutationProofUsesTheSameFunction exists to catch that, and it is
    wired so the mutation cases call the EXACT SAME function objects the real
    sweep calls (asserted by identity, not merely "a function with the same
    name"), so a detector gutted to always return `[]` reddens there first.
  - A naive extractor that ends a `python3 -c "` block at the next
    newline-then-quote (rather than the first quote preceded by an EVEN
    number of backslashes) would run past the real end of the block and
    report false hits on ordinary shell code that follows it --
    TestFalsePositiveControls exists to catch that in both directions:
    fires when it must (an escaped quote does not close the block) and stays
    silent when it must (a backtick after the real close, in a single-quoted
    opener, or past an opener that never closes).
  - Asserting only "no `command not found` on stderr" would pass just as well
    against a harness that captures no stderr at all as against a hook that
    is actually fixed -- TestRuntimeHarnessPositiveControl exists to catch
    that: the identical runner, pointed at a synthetic script reproducing the
    real hook's own `exec 2> >(tee ...)` redirect and the pre-fix construct,
    must show the error, or the negative assertion proves nothing.
  - Reading the fix off the SOURCE (grep for a backslash) would not show that
    bash stopped forking a command; only running the hook does. The PATH
    shim in TestInterpreterReceivesTheRestoredWord records the actual argv
    python received, so the assertion is direct evidence out of the argument
    vector, not an inference from the source file.

QUOTE-AWARE BOUNDARY RULE, the one this whole file exists to get right. Inside
a bash double-quoted word, `\\"` is an escaped quote (stays inside the string)
and `\\\\"` is a literal backslash followed by a quote that CLOSES the string.
The rule is therefore about the PARITY of the run of backslashes immediately
before a `"`: an odd count means the quote is escaped (does not close), an
even count (including zero) means the quote is real (closes). The identical
parity rule decides whether a BACKTICK is escaped inside the span
`_unescaped_backticks` is handed. `_python_c_blocks` finds every
`python3\\s+-c\\s+"` opener and returns the span from just past the opening
quote to the first closing quote under this rule; a `python3 -c '...'` opener
(single-quoted) is never matched, because bash performs no substitution
inside single quotes and a backtick there is inert.

POPULATION. `_hook_shell_scripts(root)` globs `hooks/scripts/*.sh` and
`bin/lib/*.sh` under whatever root it is handed -- the repo for the real
sweep, a synthetic tree under `tmp_path` for the mutation and false-positive
cases -- so no filename is ever hardcoded into this file. Measured today
(2026-09-22), the real tree yields 92 `python3 -c "` blocks across 29 files
and exactly one unescaped backtick pair (the one this plan fixes).

ISOLATION. Every test that drives the real hook as a subprocess uses
`tests._hook_runner.run_hook` with `tests._hook_runner.hook_env(daemon=None,
cache_dir=<tmp_path>)` -- the fail-closed no-daemon shape: `WRIT_SOCKET`
names a path that does not exist, `WRIT_PORT` names a free port nothing
serves, `WRIT_NO_AUTOSTART=1`, and `WRIT_CACHE_DIR` is the caller's own
`tmp_path` subdirectory. `WRIT_LOG_ROOT` is left to the suite's autouse
per-test isolation (tests/conftest.py's `_isolate_friction_log`), which
already points it at `tmp_path/logs` before this module's tests run.
`WRIT_SUBAGENT_STOP_CAPTURE` is set explicitly to a path under the test's own
`tmp_path` on every run, so no test of ours ever appends to the operator's
shared `/tmp/writ-subagent-stop-payloads.jsonl`. No test in this file reads
or writes the operator's live cache dir, live logs, the production graph, or
any shared `/tmp` file.

No assertion in this file reads documentation prose.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests._hook_runner import hook_env, run_hook

REPO = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO / "hooks" / "scripts"
BIN_LIB_DIR = REPO / "bin" / "lib"
HOOK = HOOKS_DIR / "writ-subagent-stop.sh"

# The two directories `_hook_shell_scripts` globs, exactly as named in the
# plan's population description. A synthetic tree built for the mutation and
# false-positive cases reproduces one or both of these under tmp_path.
_SCRIPT_SUBDIRS = ("hooks/scripts", "bin/lib")

# The opener, spelled as the plan spells it. `\s` covers the line-continuation
# and multi-space spellings; the trailing `"` is what keeps a single-quoted
# `python3 -c '` opener out of the population entirely.
_OPENER = re.compile(r'python3\s+-c\s+"')

# Every run_hook call in this file drives one bash hook that spawns several
# python interpreters; the shimmed run spawns two per interpreter.
_HOOK_TIMEOUT = 120


def _backslash_run(src: str, index: int) -> int:
    """How many consecutive backslashes sit immediately before `src[index]`.

    The single primitive under both parity rules below: a quote or a backtick
    is escaped exactly when this count is ODD.
    """
    count = 0
    position = index - 1
    while position >= 0 and src[position] == "\\":
        count += 1
        position -= 1
    return count


# ---------------------------------------------------------------------------
# THE TWO DETECTOR PRIMITIVES. Both are called by the real sweep AND by every
# synthetic mutation / false-positive case in this file -- never a copy of
# either -- so a detector gutted to always return `[]` reddens everywhere at
# once instead of only in the case a copy happened to preserve.
# ---------------------------------------------------------------------------
def _python_c_blocks(src: str) -> list[tuple[int, int]]:
    """Every `python3\\s+-c\\s+"` opener in `src`, as a `(start, end)` span
    (0-based string offsets) from just past the opening quote to the first
    closing `"` that is NOT escaped.

    ESCAPED MEANS preceded by an ODD number of consecutive backslashes:
    `\\"` stays inside the double-quoted bash word (does not close it),
    `\\\\"` is a literal backslash followed by a quote that DOES close it.
    This is the precise rule bash itself applies inside a double-quoted word,
    and it is the entire reason this function exists rather than a
    scan-to-the-next-newline-then-quote heuristic, which runs past the real
    end of the block and reports dozens of false hits over ordinary shell
    code that follows.

    A `python3 -c '...'` opener (single-quoted) is NEVER matched: bash
    performs no substitution inside single quotes, so a backtick there is
    inert and out of scope for this detector.

    Must not raise on an opener that is never closed (no matching
    non-escaped `"` before the end of `src`); such an opener contributes no
    span to the result.
    """
    spans: list[tuple[int, int]] = []
    for opener in _OPENER.finditer(src):
        start = opener.end()
        index = start
        while index < len(src):
            if src[index] == '"' and _backslash_run(src, index) % 2 == 0:
                spans.append((start, index))
                break
            index += 1
    return spans


def _unescaped_backticks(src: str, span: tuple[int, int]) -> list[int]:
    """The 0-based offsets, within `src`, of every backtick inside `span`
    that is NOT escaped -- the same odd/even backslash-parity rule
    `_python_c_blocks` uses to decide whether a quote closes a string applies
    here to decide whether a backtick is live: a run of zero or an EVEN
    number of backslashes immediately before the backtick means it is UNESCAPED
    (bash performs the command substitution -- a finding), and an ODD number
    of backslashes immediately before it means the backtick is escaped down
    to a literal one bash will not act on (not a finding). Returns `[]` for a
    span with none.
    """
    start, end = span
    return [
        index
        for index in range(start, end)
        if src[index] == "`" and _backslash_run(src, index) % 2 == 0
    ]


def _scan(paths: list[Path]) -> list[tuple[str, int]]:
    """Every unescaped-backtick finding across `paths`, as
    `(file_name, one_based_line_number)` pairs, built by reading each file,
    running `_python_c_blocks` over its text, and running
    `_unescaped_backticks` over every span found. `file_name` is the path's
    own `.name` (not a full path), and the line number is 1-based (the
    newline count in `src[:offset]`, plus one).

    A LOCATION IS REPORTED ONCE. The result's own type is a location, and the
    defect this detector is derived from is a backtick PAIR sitting on one
    line, so the two offsets of `\\`general-purpose\\`` are one place an editor
    has to go and not two.
    """
    findings: list[tuple[str, int]] = []
    for path in paths:
        src = path.read_text(encoding="utf-8")
        for span in _python_c_blocks(src):
            for offset in _unescaped_backticks(src, span):
                finding = (path.name, src[:offset].count("\n") + 1)
                if finding not in findings:
                    findings.append(finding)
    return findings


def _hook_shell_scripts(root) -> list[Path]:
    """Every `hooks/scripts/*.sh` and `bin/lib/*.sh` file under `root`,
    globbed AT CALL TIME -- never a hardcoded list. The real sweep passes the
    repo root; the mutation and false-positive cases pass a synthetic
    directory under `tmp_path` built with the same two subdirectories (only
    the ones a given test actually populates need exist).
    """
    base = Path(root)
    scripts: list[Path] = []
    for subdir in _SCRIPT_SUBDIRS:
        scripts.extend(sorted((base / subdir).glob("*.sh")))
    return scripts


# ---------------------------------------------------------------------------
# Test-only builders. Declared as helpers (not inlined per test) so every
# caller constructs its synthetic fixture the same way.
# ---------------------------------------------------------------------------
def _write_script(root: Path, rel_dir: str, name: str, body: str) -> Path:
    """Writes `body` to `root/rel_dir/name`, creating parent directories as
    needed, and returns the path. `rel_dir` is one of `_SCRIPT_SUBDIRS`
    ("hooks/scripts" or "bin/lib"), so the resulting tree has exactly the
    shape `_hook_shell_scripts(root)` globs.
    """
    target = Path(root) / rel_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _script_name() -> str:
    """A fresh `.sh` name per synthetic fixture, so no test in this file can
    recognize its own population by a literal it wrote in advance."""
    return f"probe-{uuid.uuid4().hex[:8]}.sh"


def _fires_body() -> str:
    """The pre-fix construct, reproduced: an unescaped backtick PAIR around a
    word that is not a command, inside a double-quoted `python3 -c "` block."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'python3 -c "\n'
        "import sys\n"
        "# claim `general-purpose` on 2026-08-27 with nothing observed.\n"
        "print(sys.argv[1])\n"
        '" "$1"\n'
        "exit 0\n"
    )


def _backtick_line(body: str) -> int:
    """The 1-based line of the first backtick in a synthetic body, derived
    from the body rather than written down beside it."""
    for number, line in enumerate(body.splitlines(), start=1):
        if "`" in line:
            return number
    raise AssertionError(f"fixture holds no backtick at all: {body!r}")


# The four false-positive constructs and the block count each must still
# yield, so a zero-finding verdict is never trusted from an extractor that
# extracted nothing for that shape either.
_FALSE_POSITIVE_BODIES: dict[str, tuple[str, int]] = {
    # The real closing quote sits MID-LINE, so a scan-to-the-next-
    # newline-then-quote extractor runs past it to the end of the file and
    # swallows the backtick two lines below.
    "after_closing_quote": (
        "#!/usr/bin/env bash\n"
        'python3 -c "\n'
        "import sys\n"
        'print(sys.argv[1])" "$1"\n'
        "echo `date`\n"
        "exit 0\n",
        1,
    ),
    # Two escaped quotes inside the block, then the real close mid-line, then
    # a backtick outside it.
    "after_escaped_quote_past_real_close": (
        "#!/usr/bin/env bash\n"
        'python3 -c "\n'
        "import sys\n"
        'print(\\"escaped\\")" "$1"\n'
        "echo `date`\n"
        "exit 0\n",
        1,
    ),
    # Single-quoted opener: not matched at all, so no span exists to scan.
    "single_quoted_block": (
        "#!/usr/bin/env bash\n"
        "python3 -c '\n"
        "import sys\n"
        "# a `backtick` here is inert\n"
        "print(sys.argv[1])\n"
        "' \"$1\"\n"
        "exit 0\n",
        0,
    ),
    # An opener with no non-escaped closing quote anywhere after it.
    "unterminated_opener": (
        "#!/usr/bin/env bash\n"
        'python3 -c "\n'
        "import sys\n"
        "# a `backtick` in a block that never closes\n"
        "print(sys.argv[1])\n"
        "exit 0\n",
        0,
    ),
}


def _install_argv_recording_python3(tmp_path: Path) -> tuple[Path, Path]:
    """Writes a shim named `python3` under `tmp_path/bin` that appends one
    JSON line per invocation (at minimum: the full argv list) to
    `tmp_path/python3-argv.jsonl`, then `exec`s the REAL interpreter with the
    same argv so the hook's behavior is otherwise unaffected.

    ORDER IS LOAD-BEARING: this resolves `shutil.which("python3")` BEFORE the
    shim exists anywhere on PATH -- i.e. before the caller prepends the
    returned bin dir to PATH -- and bakes that absolute, already-resolved
    path into the shim script. Resolving it after prepending would have the
    shim find itself on PATH and recurse into itself instead of the real
    interpreter.

    Returns `(bin_dir, record_path)`. The caller prepends `bin_dir` to the
    subprocess env's `PATH` before running the hook.
    """
    real = shutil.which("python3")
    assert real, "no python3 on PATH to shim, so this measurement cannot be taken"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    record = tmp_path / "python3-argv.jsonl"
    shim = bin_dir / "python3"
    shim.write_text(
        "#!" + real + "\n"
        "import json, os, sys\n"
        "with open(" + repr(str(record)) + ", 'a') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "os.execv(" + repr(real) + ", [" + repr(real) + "] + sys.argv[1:])\n",
        encoding="utf-8",
    )
    os.chmod(shim, 0o755)
    return bin_dir, record


def _write_prefix_control_script(tmp_path: Path) -> tuple[Path, Path, str]:
    """Writes a synthetic bash script under `tmp_path`, NOT the real hook,
    that reproduces the two facts the runtime positive control needs
    together: the real hook's own stderr redirect
    (`exec 2> >(tee -a "$SINK" >&2)`, matching writ-subagent-stop.sh:24 in
    shape) and the PRE-FIX construct -- an unescaped backtick pair around a
    nonexistent command name inside a `python3 -c "..."` block. `$SINK` is a
    path under `tmp_path` the caller can read back, not `/dev/null` and not
    any shared operator path. Run through the SAME `run_hook` harness the
    real-hook tests use, this script's stderr must show `command not found`;
    if it did not, the harness itself would be blind to the error class, and
    the real hook's absence of that line would prove nothing.

    Returns `(script_path, sink_path, missing_command_name)`.
    """
    sink = tmp_path / "control-stderr.log"
    missing = f"writ-no-such-control-command-{uuid.uuid4().hex[:8]}"
    script = tmp_path / "prefix-control.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'SINK="{sink}"\n'
        'exec 2> >(tee -a "$SINK" >&2)\n'
        'python3 -c "\n'
        f"# claim `{missing}` on 2026-08-27 with nothing observed.\n"
        "print('control')\n"
        '"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    return script, sink, missing


def _subagent_stop_envelope(
    *, agent_id: str, agent_type: str = "", session_id: str = "parent-session"
) -> str:
    """A minimal synthetic SubagentStop JSON envelope carrying only
    `agent_id`, `agent_type` and `session_id` -- no `transcript_path` and no
    `agent_transcript_path` keys, so writ-subagent-stop.sh's transcript
    tripwire branch (line 216: `if [ -n "$AGENT_TRANSCRIPT" ] || [ -n
    "$PARENT_TRANSCRIPT" ]`) is never entered and stays out of every runtime
    assertion in this file. `agent_id` must be non-empty, or the hook exits
    at line 88 before reaching either python3 -c block this suite cares
    about.
    """
    assert agent_id, "the hook exits before the block under test on an empty agent_id"
    return json.dumps(
        {"agent_id": agent_id, "agent_type": agent_type, "session_id": session_id}
    )


# ---------------------------------------------------------------------------
# Anti-vacuity for the population itself (Capability 3): a zero-finding
# result over the real tree must not be producible by an extractor that
# extracted nothing.
# ---------------------------------------------------------------------------
class TestVacuousDetectionGuard:
    def test_the_real_tree_yields_a_non_empty_python_c_block_population(self) -> None:
        """`_python_c_blocks`, run over every file `_hook_shell_scripts(REPO)`
        returns, must find at least one block SOMEWHERE in the real tree
        (measured 2026-09-22: 92 blocks across 29 files). A count of zero here
        means the glob, the root, or the extractor is broken -- not that
        every hook script in this repo stopped invoking `python3 -c`."""
        scripts = _hook_shell_scripts(REPO)
        assert scripts, (
            f"_hook_shell_scripts({REPO}) returned NOTHING, so every zero-finding "
            f"assertion in this module would pass by scanning an empty population. "
            f"Expected the union of {HOOKS_DIR}/*.sh and {BIN_LIB_DIR}/*.sh "
            f"(those directories exist: {HOOKS_DIR.is_dir()}, {BIN_LIB_DIR.is_dir()})"
        )
        blocks = {path.name: len(_python_c_blocks(path.read_text(encoding="utf-8")))
                  for path in scripts}
        total = sum(blocks.values())
        carrying = [name for name, count in blocks.items() if count]
        assert total > 0, (
            f"_python_c_blocks extracted ZERO blocks from {len(scripts)} real hook "
            f"scripts. Measured 2026-09-22: 92 blocks across 29 files under "
            f"{HOOKS_DIR} alone. Zero means the extractor, not the tree, so no "
            f"zero-finding verdict below can be trusted"
        )
        assert carrying, "no single file carried a block, which the total above denies"


# ---------------------------------------------------------------------------
# Capability 4: the population is derived by glob, never a literal list.
# ---------------------------------------------------------------------------
class TestGlobDerivedPopulation:
    @pytest.mark.parametrize("rel_dir", _SCRIPT_SUBDIRS, ids=["hooks_scripts", "bin_lib"])
    def test_hook_shell_scripts_globs_both_directories_under_an_arbitrary_root(
        self, tmp_path: Path, rel_dir: str
    ) -> None:
        """A single `.sh` file written under EITHER `hooks/scripts` or
        `bin/lib` beneath an otherwise-empty synthetic root is returned by
        `_hook_shell_scripts(root)`; a root with neither directory populated
        yields an empty list first, so the presence in the parametrized case
        is attributable to the glob and not to some other default."""
        assert _hook_shell_scripts(tmp_path) == [], (
            f"an empty root already yielded scripts: {_hook_shell_scripts(tmp_path)}"
        )
        name = _script_name()
        written = _write_script(tmp_path, rel_dir, name, _fires_body())
        assert _hook_shell_scripts(tmp_path) == [written], (
            f"a .sh file at {rel_dir}/{name} under {tmp_path} was not returned by the "
            f"glob; got {_hook_shell_scripts(tmp_path)}"
        )

    def test_a_new_sh_file_dropped_into_a_synthetic_tree_is_scanned_with_no_test_edit(
        self, tmp_path: Path
    ) -> None:
        """Writes TWO new `.sh` files under a synthetic tree -- names neither
        hardcoded here nor anywhere else in this module -- one holding an
        unescaped backtick in a `python3 -c "` block and one with none.
        `_scan(_hook_shell_scripts(tmp_path))` must report exactly the first
        file's name with no edit to this test's population logic, proving
        the sweep follows whatever is on disk rather than a name this file
        knows in advance."""
        fires = _fires_body()
        dirty = _write_script(tmp_path, _SCRIPT_SUBDIRS[0], _script_name(), fires)
        clean_name = _script_name()
        _write_script(
            tmp_path, _SCRIPT_SUBDIRS[1], clean_name, fires.replace("`", "\\`")
        )
        scripts = _hook_shell_scripts(tmp_path)
        assert len(scripts) == 2, f"expected both new files in the population: {scripts}"
        assert _scan(scripts) == [(dirty.name, _backtick_line(fires))], (
            f"the sweep did not follow the tree: {_scan(scripts)}, for files "
            f"{[p.name for p in scripts]}"
        )


# ---------------------------------------------------------------------------
# Capabilities 1-2: the real tree, as it exists once the fix in plan.md
# lands.
# ---------------------------------------------------------------------------
class TestRealTreeIsClean:
    def test_writ_subagent_stop_sh_has_no_unescaped_backtick_in_its_python_c_blocks(
        self,
    ) -> None:
        """Every span `_python_c_blocks` finds in
        `hooks/scripts/writ-subagent-stop.sh`'s own source, run through
        `_unescaped_backticks`, yields zero offsets. This is the exact
        capability the plan's fix exists to satisfy: today it is unescaped at
        line 328 (`# claim `general-purpose` on 2026-08-27 ...`), and this
        test is expected to fail until that line is edited."""
        src = HOOK.read_text(encoding="utf-8")
        spans = _python_c_blocks(src)
        assert spans, (
            f"{HOOK.name} yielded no double-quoted python3 -c block at all, so the "
            f"zero below would say nothing about it"
        )
        live = [
            (src[:offset].count("\n") + 1, src[offset - 20:offset + 20])
            for span in spans
            for offset in _unescaped_backticks(src, span)
        ]
        assert live == [], (
            f"{HOOK.name} still carries {len(live)} unescaped backtick(s) inside a "
            f"python3 -c \" block; bash command-substitutes each one on every "
            f"SubagentStop: {live}"
        )

    def test_writ_subagent_stop_sh_role_source_comment_still_carries_general_purpose_in_escaped_backticks(
        self,
    ) -> None:
        """The fix must not delete the markup, only escape it: the hook's
        source must contain the literal substring
        ``\\`general-purpose\\` `` (backslash, backtick, the word, backslash,
        backtick) inside the same `python3 -c "` block the unescaped pair
        used to live in, so the author's comment survives character for
        character once bash stops treating it as a command."""
        src = HOOK.read_text(encoding="utf-8")
        marker = "\\`general-purpose\\`"
        offset = src.find(marker)
        assert offset != -1, (
            f"{HOOK.name} no longer carries the escaped markup {marker!r}; the fix "
            f"was to escape the backticks, not to reword the comment"
        )
        spans = _python_c_blocks(src)
        assert any(start <= offset < end for start, end in spans), (
            f"{marker!r} sits at offset {offset}, outside every python3 -c \" block "
            f"{spans} -- so it is no longer the comment the interpreter receives"
        )

    def test_the_glob_derived_sweep_over_the_real_tree_reports_zero_findings(
        self,
    ) -> None:
        """`_scan(_hook_shell_scripts(REPO))` -- the full production
        population, `hooks/scripts/*.sh` plus `bin/lib/*.sh` -- returns an
        empty list. Broader than the single-file case above: a NEW instance
        of this same defect, introduced anywhere else under either
        directory, must also fail here."""
        scripts = _hook_shell_scripts(REPO)
        assert scripts, "the real population is empty, so this sweep proves nothing"
        assert _scan(scripts) == [], (
            f"unescaped backticks inside a double-quoted python3 -c block, each one a "
            f"word bash executes under the operator's uid when its hook fires: "
            f"{_scan(scripts)}"
        )


# ---------------------------------------------------------------------------
# Capabilities 5-6, and the non-negotiable that ties them to the real sweep:
# the mutation proof must run through the identical function objects, not a
# parallel copy that could silently drift from what production code uses.
# ---------------------------------------------------------------------------
class TestMutationProofUsesTheSameFunction:
    def test_the_sweep_and_the_mutation_cases_share_the_same_function_objects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity, not merely name equality: whatever function
        `TestRealTreeIsClean` calls to scan the real tree must be the EXACT
        object `is`-identical to the one the two mutation tests below call
        over a synthetic tree. A refactor that quietly forks a second copy
        of the detector for the synthetic cases must fail this test even
        though every other assertion in this class might still pass."""
        module = sys.modules[__name__]
        seen_blocks: list[object] = []
        seen_ticks: list[object] = []
        real_blocks, real_ticks = _python_c_blocks, _unescaped_backticks

        def blocks_recorder(src):
            seen_blocks.append(blocks_recorder)
            return real_blocks(src)

        def ticks_recorder(src, span):
            seen_ticks.append(ticks_recorder)
            return real_ticks(src, span)

        monkeypatch.setattr(module, "_python_c_blocks", blocks_recorder)
        monkeypatch.setattr(module, "_unescaped_backticks", ticks_recorder)

        _scan(_hook_shell_scripts(REPO))
        real_sweep = (len(seen_blocks), len(seen_ticks))
        _write_script(tmp_path, _SCRIPT_SUBDIRS[0], _script_name(), _fires_body())
        _scan(_hook_shell_scripts(tmp_path))
        synthetic = (len(seen_blocks) - real_sweep[0], len(seen_ticks) - real_sweep[1])

        assert real_sweep[0] > 0 and real_sweep[1] > 0, (
            f"the REAL-tree sweep never reached the patched detector "
            f"{real_sweep}, so this identity proof measured nothing"
        )
        assert synthetic[0] > 0 and synthetic[1] > 0, (
            f"the SYNTHETIC sweep never reached the same patched detector "
            f"{synthetic}: the mutation cases are running through a forked copy, so "
            f"a detector gutted in production would stay green here"
        )
        assert {id(fn) for fn in seen_blocks} == {id(blocks_recorder)}, (
            "more than one _python_c_blocks object served the two sweeps"
        )
        assert {id(fn) for fn in seen_ticks} == {id(ticks_recorder)}, (
            "more than one _unescaped_backticks object served the two sweeps"
        )

    def test_an_unescaped_backtick_pair_in_a_synthetic_block_yields_exactly_one_finding_naming_file_and_line(
        self, tmp_path: Path
    ) -> None:
        """A synthetic `hooks/scripts/*.sh` file under `tmp_path`, holding a
        `python3 -c "..."` block with one unescaped backtick pair on a known
        line, makes `_scan(_hook_shell_scripts(tmp_path))` return EXACTLY one
        `(file_name, line_number)` pair, naming that file and the 1-based
        line the backtick sits on -- not zero, not more than one, and not a
        different file or line."""
        body = _fires_body()
        script = _write_script(tmp_path, _SCRIPT_SUBDIRS[0], _script_name(), body)
        findings = _scan(_hook_shell_scripts(tmp_path))
        assert findings == [(script.name, _backtick_line(body))], (
            f"the mutation did not fire as one located finding: {findings}, for a "
            f"backtick pair on line {_backtick_line(body)} of {script.name}"
        )

    def test_escaping_both_backticks_in_the_same_script_yields_zero_findings(
        self, tmp_path: Path
    ) -> None:
        """The identical script from the fires-case above, with both
        backticks backslash-escaped (`\\``), makes the same sweep return an
        empty list -- the silent half of the same mutation, proving the
        detector's positive result above was about the backticks and not
        about the file's mere existence."""
        body = _fires_body().replace("`", "\\`")
        script = _write_script(tmp_path, _SCRIPT_SUBDIRS[0], _script_name(), body)
        src = script.read_text(encoding="utf-8")
        assert src.count("\\`") == 2, (
            f"the silent fixture is not the fires fixture with both backticks "
            f"escaped: {src!r}"
        )
        assert _python_c_blocks(src), (
            "the escaped variant lost its python3 -c block, so zero findings here "
            "would come from an empty span list rather than from the escaping"
        )
        assert _scan(_hook_shell_scripts(tmp_path)) == [], (
            f"escaping both backticks still reported a finding: "
            f"{_scan(_hook_shell_scripts(tmp_path))}"
        )


# ---------------------------------------------------------------------------
# Capabilities 7-11: the four named false-positive controls plus the
# no-opener-at-all edge, each a shape the naive scan-to-next-newline-quote
# extractor gets wrong.
# ---------------------------------------------------------------------------
class TestFalsePositiveControls:
    @pytest.mark.parametrize(
        "scenario",
        ["after_closing_quote", "after_escaped_quote_past_real_close",
         "single_quoted_block", "unterminated_opener"],
    )
    def test_the_named_construct_yields_zero_findings(
        self, tmp_path: Path, scenario: str
    ) -> None:
        """Four constructs, each built into its own synthetic
        `hooks/scripts/*.sh` file and swept via
        `_scan(_hook_shell_scripts(tmp_path))`, must ALL report zero
        findings:

          - after_closing_quote: a backtick placed in the plain shell code
            AFTER a `python3 -c "..."` block's real closing quote. The naive
            scan-to-next-newline-quote extractor would have already run past
            the true close and could catch this backtick inside a
            (wrongly-extended) span; the quote-aware extractor must not.
          - after_escaped_quote_past_real_close: a block containing an
            escaped quote (`\\"`) partway through, with a backtick placed
            AFTER the block's REAL (non-escaped) closing quote -- proving the
            escaped quote correctly did NOT end the block early, so nothing
            after the true close is in scope at all.
          - single_quoted_block: a `python3 -c '...'` opener (single quotes)
            containing a bare backtick. Bash performs no substitution inside
            single quotes, and `_python_c_blocks` must not even match this
            opener, so no span is produced for `_unescaped_backticks` to
            scan.
          - unterminated_opener: a `python3 -c "` opener with no matching
            non-escaped closing quote anywhere in the file. Must yield zero
            findings AND must not raise.

        Each case is additionally provable NOT vacuous by construction: the
        number of blocks `_python_c_blocks` extracts for the shape is
        asserted here before the zero-finding conclusion is trusted, and
        every one of these bodies carries a backtick, so a detector that
        merely returned `[]` for everything could not pass the fires-case in
        the class above.
        """
        body, expected_blocks = _FALSE_POSITIVE_BODIES[scenario]
        assert "`" in body, f"the {scenario} fixture carries no backtick to miss"
        script = _write_script(tmp_path, _SCRIPT_SUBDIRS[0], _script_name(), body)
        src = script.read_text(encoding="utf-8")
        assert len(_python_c_blocks(src)) == expected_blocks, (
            f"{scenario} extracted {_python_c_blocks(src)} rather than "
            f"{expected_blocks} block(s); the zero-finding verdict below would be "
            f"about the wrong span"
        )
        assert _scan(_hook_shell_scripts(tmp_path)) == [], (
            f"{scenario} reported a false positive: "
            f"{_scan(_hook_shell_scripts(tmp_path))}"
        )

    def test_a_backtick_before_an_escaped_quote_inside_the_block_is_reported(
        self, tmp_path: Path
    ) -> None:
        """The positive half of Capability 8: a `python3 -c "..."` block
        containing an escaped quote (`\\"`) partway through, with a backtick
        placed BEFORE that escaped quote (i.e. still inside the block,
        before its real close), IS reported by
        `_scan(_hook_shell_scripts(tmp_path))` -- proving the escaped quote
        does not end the block early and the backtick inside is still in
        scope."""
        body = (
            "#!/usr/bin/env bash\n"
            'python3 -c "\n'
            "import sys\n"
            "# claim `general-purpose` before an escaped quote\n"
            'print(\\"escaped\\")" "$1"\n'
            "echo done\n"
            "exit 0\n"
        )
        script = _write_script(tmp_path, _SCRIPT_SUBDIRS[0], _script_name(), body)
        assert len(_python_c_blocks(script.read_text(encoding="utf-8"))) == 1
        assert _scan(_hook_shell_scripts(tmp_path)) == [
            (script.name, _backtick_line(body))
        ], (
            f"a backtick before the escaped quote was not reported: "
            f"{_scan(_hook_shell_scripts(tmp_path))}. The escaped quote ended the "
            f"block early, so everything after it fell out of scope"
        )

    def test_a_script_with_no_python_c_opener_at_all_yields_an_empty_block_list_and_zero_findings(
        self, tmp_path: Path
    ) -> None:
        """Capability 11: an ordinary `.sh` file with no `python3 -c "`
        anywhere in it makes `_python_c_blocks` return `[]` directly (not
        merely a sweep that happens to find nothing), and `_scan` over the
        tree containing only this file returns `[]` as well."""
        body = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'echo "no interpreter here"\n'
            "STAMP=`date`\n"
            'echo "$STAMP"\n'
            "exit 0\n"
        )
        script = _write_script(tmp_path, _SCRIPT_SUBDIRS[1], _script_name(), body)
        assert _python_c_blocks(script.read_text(encoding="utf-8")) == []
        assert _hook_shell_scripts(tmp_path) == [script]
        assert _scan(_hook_shell_scripts(tmp_path)) == []


# ---------------------------------------------------------------------------
# Capabilities 12-13: the real hook, driven as a subprocess, and the harness's
# own positive control for its negative assertion.
# ---------------------------------------------------------------------------
class TestRuntimeNoCommandNotFound:
    def test_the_real_hook_writes_no_command_not_found_and_exits_zero(
        self, tmp_path: Path
    ) -> None:
        """Drives `hooks/scripts/writ-subagent-stop.sh` as a real subprocess
        via `tests._hook_runner.run_hook`, under
        `hook_env(daemon=None, cache_dir=<tmp_path>/cache)` plus
        `WRIT_SUBAGENT_STOP_CAPTURE` pointed at a file under `tmp_path`
        (never the operator's shared `/tmp/writ-subagent-stop-payloads.jsonl`),
        fed a synthetic SubagentStop envelope (`_subagent_stop_envelope`)
        naming an `agent_id` but no transcript paths. The completed process's
        `stderr` must NOT contain `command not found`, and its return code
        must be `0`.
        """
        cache = tmp_path / "cache"
        cache.mkdir()
        env = hook_env(daemon=None, cache_dir=str(cache))
        env["WRIT_SUBAGENT_STOP_CAPTURE"] = str(tmp_path / "capture.jsonl")
        completed = run_hook(
            HOOK,
            _subagent_stop_envelope(
                agent_id=f"agent-{uuid.uuid4().hex[:8]}", agent_type="writ-implementer"
            ),
            env=env,
            cwd=str(tmp_path),
            timeout=_HOOK_TIMEOUT,
        )
        assert "command not found" not in completed.stderr, (
            f"bash still executed a word out of the hook's own source: "
            f"{completed.stderr!r}"
        )
        assert completed.returncode == 0, (
            f"the hook exited {completed.returncode}; stderr {completed.stderr!r}"
        )


class TestRuntimeHarnessPositiveControl:
    def test_a_synthetic_pre_fix_script_with_the_same_stderr_redirect_does_show_command_not_found(
        self, tmp_path: Path
    ) -> None:
        """The control for the test above: `_write_prefix_control_script`
        writes a synthetic script (not the real hook) that reproduces BOTH
        the real hook's `exec 2> >(tee -a "$SINK" >&2)` redirect and the
        pre-fix unescaped-backtick construct. Run through the identical
        `run_hook` harness, its `stderr` DOES contain `command not found`.
        Without this test passing, the negative assertion in
        `TestRuntimeNoCommandNotFound` would be equally satisfied by a
        harness that cannot see stderr through the process-substitution
        redirect at all, which would make that test's silence meaningless.
        """
        cache = tmp_path / "cache"
        cache.mkdir()
        script, sink, missing = _write_prefix_control_script(tmp_path)
        env = hook_env(daemon=None, cache_dir=str(cache))
        env["WRIT_SUBAGENT_STOP_CAPTURE"] = str(tmp_path / "capture.jsonl")
        completed = run_hook(
            script,
            _subagent_stop_envelope(agent_id=f"agent-{uuid.uuid4().hex[:8]}"),
            env=env,
            cwd=str(tmp_path),
            timeout=_HOOK_TIMEOUT,
        )
        assert "command not found" in completed.stderr, (
            f"the harness could not see `command not found` through the same "
            f"`exec 2> >(tee ...)` redirect the real hook uses, so the negative "
            f"assertion in TestRuntimeNoCommandNotFound proves nothing. stderr "
            f"{completed.stderr!r}, sink "
            f"{sink.read_text() if sink.exists() else '<no sink>'!r}"
        )
        assert missing in completed.stderr, (
            f"the captured error names something other than the control's own "
            f"nonexistent command {missing}: {completed.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Capability 14: positive evidence, read out of the argument vector the
# interpreter actually received, that the fix restores the deleted word.
# ---------------------------------------------------------------------------
class TestInterpreterReceivesTheRestoredWord:
    def test_path_shimmed_python3_records_general_purpose_inside_the_subagent_complete_program(
        self, tmp_path: Path
    ) -> None:
        """`_install_argv_recording_python3(tmp_path)` shims `python3` on
        PATH (real interpreter resolved BEFORE the shim exists on PATH) and
        drives the real hook via `run_hook` with that PATH prepended. Among
        the recorded invocations, the one whose `-c` program text contains
        `subagent_complete` (the final entry-building block,
        writ-subagent-stop.sh:308-343) must contain the word
        `general-purpose` in its program text. Pre-fix, bash deletes that
        word from the argument before python ever sees it, so this
        assertion is the one the defect breaks: it reads the word out of the
        argv python actually received, not out of the source file on disk.
        """
        cache = tmp_path / "cache"
        cache.mkdir()
        bin_dir, record = _install_argv_recording_python3(tmp_path)
        env = hook_env(daemon=None, cache_dir=str(cache))
        env["WRIT_SUBAGENT_STOP_CAPTURE"] = str(tmp_path / "capture.jsonl")
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        completed = run_hook(
            HOOK,
            _subagent_stop_envelope(
                agent_id=f"agent-{uuid.uuid4().hex[:8]}", agent_type="writ-implementer"
            ),
            env=env,
            cwd=str(tmp_path),
            timeout=_HOOK_TIMEOUT,
        )
        assert completed.returncode == 0, (
            f"the shimmed run did not complete: {completed.stderr!r}"
        )
        assert record.exists(), (
            f"the PATH shim at {bin_dir}/python3 recorded nothing, so the hook never "
            f"resolved python3 through it and this measurement is blind"
        )
        invocations = [json.loads(line) for line in record.read_text().splitlines() if line]
        programs = [
            argv[1]
            for argv in invocations
            if len(argv) > 1 and argv[0] == "-c" and "subagent_complete" in argv[1]
        ]
        assert len(programs) == 1, (
            f"expected exactly one recorded `-c` program building the "
            f"subagent_complete entry, got {len(programs)} across "
            f"{len(invocations)} python3 invocations"
        )
        assert "general-purpose" in programs[0], (
            "the interpreter received the role_source comment with the word DELETED, "
            "which is what bash does when it command-substitutes an unescaped "
            f"backtick pair: {programs[0]!r}"
        )


# ---------------------------------------------------------------------------
# Capability 15: the edit must not change the hook's observable behavior
# beyond the one corrupted word.
# ---------------------------------------------------------------------------
class TestHookBehaviorUnchanged:
    def test_one_subagent_complete_entry_carries_role_source_and_cache_state_and_exit_is_zero(
        self, tmp_path: Path
    ) -> None:
        """Driven the same way as `TestRuntimeNoCommandNotFound`, from a cwd
        that resolves to a real project (a `tmp_path` directory holding a
        `.git`), the run must exit 0 and must produce EXACTLY ONE
        `subagent_complete` entry in the `metrics` stream under that
        project's log root (`writ.shared.logging.read_streams`), and that
        entry must carry both a `role_source` key and a `cache_state` key
        with non-empty string values -- proving the escape fix changed
        nothing about the row's shape or count.
        """
        from writ.shared.logging import read_streams, resolve_project

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        initialized = subprocess.run(
            ["git", "init", "-q"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert initialized.returncode == 0, (
            f"could not make a real project to run from: {initialized.stderr!r}"
        )
        cache = tmp_path / "cache"
        cache.mkdir()
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        env = hook_env(daemon=None, cache_dir=str(cache))
        env["WRIT_SUBAGENT_STOP_CAPTURE"] = str(tmp_path / "capture.jsonl")
        completed = run_hook(
            HOOK,
            _subagent_stop_envelope(agent_id=agent_id, agent_type="writ-implementer"),
            env=env,
            cwd=str(project_dir),
            timeout=_HOOK_TIMEOUT,
        )
        assert completed.returncode == 0, (
            f"the hook exited {completed.returncode}: {completed.stderr!r}"
        )
        project = resolve_project(str(project_dir))
        entries = [
            row
            for row in read_streams(project, ["metrics"])
            if row.get("event") == "subagent_complete" and row.get("agent_id") == agent_id
        ]
        assert len(entries) == 1, (
            f"expected exactly one subagent_complete row for {agent_id} under project "
            f"{project!r}, got {len(entries)}: {entries}"
        )
        entry = entries[0]
        assert isinstance(entry.get("role_source"), str) and entry["role_source"], (
            f"the row lost its role_source: {entry}"
        )
        assert isinstance(entry.get("cache_state"), str) and entry["cache_state"], (
            f"the row lost its cache_state: {entry}"
        )


# ---------------------------------------------------------------------------
# Capability 16: the in-scope capture-path override, and its unchanged
# default.
# ---------------------------------------------------------------------------
class TestCapturePathOverride:
    def test_writ_subagent_stop_capture_env_override_lands_the_envelope_under_tmp_path(
        self, tmp_path: Path
    ) -> None:
        """With `WRIT_SUBAGENT_STOP_CAPTURE` set to a path under `tmp_path`,
        running the real hook with a distinctive `agent_id` appends the raw
        envelope to THAT file (readable, non-empty, containing the
        distinctive id).

        THE SHARED `/tmp/writ-subagent-stop-payloads.jsonl` IS NOT TOUCHED
        HERE, not even to read its length: this module's isolation rule is
        that no test of ours reads or writes a shared operator file, and a
        "did not grow" assertion over that one is measurably false-failing
        anyway (it grows from the operator's own live SubagentStop dispatches
        while the suite runs: measured 7 lines to 11 during this plan's
        implementation, none of them from a test). The hook has exactly one
        append site and it writes `$_WRIT_STOP_CAP`, so an envelope landing
        here is an envelope that did not land there.
        """
        cache = tmp_path / "cache"
        cache.mkdir()
        capture = tmp_path / "capture.jsonl"
        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        env = hook_env(daemon=None, cache_dir=str(cache))
        env["WRIT_SUBAGENT_STOP_CAPTURE"] = str(capture)
        completed = run_hook(
            HOOK,
            _subagent_stop_envelope(agent_id=agent_id, agent_type="writ-implementer"),
            env=env,
            cwd=str(tmp_path),
            timeout=_HOOK_TIMEOUT,
        )
        assert completed.returncode == 0, (
            f"the hook exited {completed.returncode}: {completed.stderr!r}"
        )
        assert capture.exists(), (
            f"WRIT_SUBAGENT_STOP_CAPTURE={capture} was ignored, so the envelope went "
            f"to the hook's default shared path instead"
        )
        lines = [line for line in capture.read_text().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected one captured envelope, got {lines}"
        assert json.loads(lines[0])["agent_id"] == agent_id, (
            f"the captured envelope is not this run's: {lines[0]!r}"
        )

    def test_the_source_default_capture_path_is_unchanged_when_the_override_is_unset(
        self,
    ) -> None:
        """A source-shape pin, not a behavioral run: with no
        `WRIT_SUBAGENT_STOP_CAPTURE` in play, the hook's own source still
        names the literal default
        `/tmp/writ-subagent-stop-payloads.jsonl` as the fallback in its
        `_WRIT_STOP_CAP` assignment, so the widened capture-path change is
        byte-identical to today's behavior for every caller that never sets
        the override.
        """
        src = HOOK.read_text(encoding="utf-8")
        assignment = (
            '_WRIT_STOP_CAP="${WRIT_SUBAGENT_STOP_CAPTURE:'
            '-/tmp/writ-subagent-stop-payloads.jsonl}"'
        )
        assert assignment in src, (
            f"{HOOK.name} no longer assigns _WRIT_STOP_CAP as an override over the "
            f"unchanged default; expected the line {assignment!r}"
        )
        assert src.count("_WRIT_STOP_CAP=") == 1, (
            "more than one assignment of _WRIT_STOP_CAP, so which path a default run "
            "appends to is no longer decided in one place"
        )
