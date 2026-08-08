"""Every `python3 -c "..."` block embedded in a shell script must be valid Python.

WHY THIS EXISTS. While converting the friction row builder I wrapped its python block in a
new shell `if` and indented it by four spaces. Those lines are Python SOURCE inside a
double-quoted shell string, so the indentation turned working code into an
IndentationError. `bash -n` passed, because the shell is fine; the shell does not care what
the string it carries says. And the block only runs when jq is absent, so every test on a
machine with jq would have passed while a jq-less machine silently lost the audit rows
those lines produce.

That is the whole failure mode: embedded interpreters are invisible to both languages'
syntax checkers. This file extracts each block and compiles it, so a broken one fails here
rather than on someone else's machine.

EXTRACTION MIRRORS WHAT THE SHELL ACTUALLY DOES, which is the only way the compile is
meaningful:
  - Inside a double-quoted string the shell unescapes \\" \\\\ \\$ \\` and a
    backslash-newline. python never sees the backslashes, so neither does the compiler
    here. Skipping this step reports `\\"` as "unexpected character after line
    continuation character", which is a bug in the checker, not the script.
  - Shell expansions ($VAR, ${VAR}, $(cmd)) are replaced with a placeholder identifier.
    The block cannot be compiled with them in place, and a placeholder preserves the
    surrounding structure: `foo($X)` and `foo(_WRIT_SUBST)` are both parseable, and a
    substitution inside a python string literal is unaffected either way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SEARCH_DIRS = [REPO / "hooks" / "scripts", REPO / "bin" / "lib", REPO / "scripts"]

OPEN = re.compile(r'python3\s+-c\s+"')
# `$(...)`, `${...}`, `$VAR`, `$1`. Ordered longest-first so the greedy forms win.
EXPANSION = re.compile(r"\$\([^)]*\)|\$\{[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|\$[0-9@*#?]")
PLACEHOLDER = "_WRIT_SUBST"


def _shell_unescape(body: str) -> str:
    """Undo the unescaping a double-quoted shell string performs before python sees it."""
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in '"\\`$':
                out.append(nxt)
                i += 2
                continue
            if nxt == "\n":            # line continuation: both characters vanish
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _in_comment(text: str, idx: int) -> bool:
    """True if `idx` sits on a `#` comment line.

    These files DOCUMENT interpreter one-liners in prose, e.g. writ-bash-write-gate.sh
    explaining that `python3 -c "json.dump(...open(p,'w'))"` slips past a write-verb
    extractor. That example is deliberately abbreviated and will never compile, and failing
    on it would push the next author to reword a comment to satisfy a test.
    """
    line_start = text.rfind("\n", 0, idx) + 1
    return text[line_start:idx].lstrip().startswith("#")


def _closing_quote(text: str, start: int) -> int:
    """Index of the `"` that ends the shell string opened at `start`, or -1.

    The rule is the shell's: inside a double-quoted string, the first UNESCAPED `"` closes
    it. A python block therefore quotes its own strings with single quotes and escapes any
    literal double quote as \\".

    An earlier version looked for a line beginning with `"` instead, which overshot every
    block whose closing quote is mid-line (`print(...)" "$field" 2>/dev/null`) and swallowed
    the following shell code, then reported the resulting mess as a python syntax error.
    Eight blocks failed that way, all of them fine.
    """
    i = start
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _blocks() -> list[tuple[Path, int, str]]:
    """(path, 1-based line of the opening quote, python source) for each embedded block."""
    found: list[tuple[Path, int, str]] = []
    for directory in SEARCH_DIRS:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.sh")):
            text = path.read_text(errors="replace")
            pos = 0
            while True:
                m = OPEN.search(text, pos)
                if not m:
                    break
                start = m.end()
                end = _closing_quote(text, start)
                if end == -1:
                    break
                if not _in_comment(text, m.start()):
                    found.append((path, text[:start].count("\n") + 1, text[start:end]))
                pos = end + 1
    return found


BLOCKS = _blocks()
IDS = [f"{p.name}:{line}" for p, line, _ in BLOCKS]


class TestEmbeddedPythonIsValid:
    @pytest.mark.parametrize("path,line,body", BLOCKS, ids=IDS)
    def test_block_compiles(self, path: Path, line: int, body: str) -> None:
        source = EXPANSION.sub(PLACEHOLDER, _shell_unescape(body))
        try:
            compile(source, f"{path.name}:{line}", "exec")
        except SyntaxError as exc:
            pytest.fail(
                f"embedded python at {path.name}:{line} does not compile: {exc.msg} "
                f"(line {exc.lineno} of the block). Neither `bash -n` nor a python test "
                f"run would catch this; a common cause is re-indenting the block to nest "
                f"it inside a shell `if`.\n"
                f"--- block ---\n{source[:600]}"
            )

    def test_there_are_blocks_to_check(self) -> None:
        """Anti-vacuity: an extractor that found nothing would pass on any tree, including
        one where every embedded block is broken."""
        assert len(BLOCKS) > 30, f"only found {len(BLOCKS)} embedded python blocks"

    def test_the_checker_catches_a_planted_indentation_error(self) -> None:
        """The exact mistake this file was written for: valid python made invalid by the
        indentation added when wrapping it in a shell `if`."""
        broken = "    import json\n    print(json.dumps({}))\n"
        with pytest.raises(SyntaxError):
            compile(EXPANSION.sub(PLACEHOLDER, _shell_unescape(broken)),
                    "planted", "exec")

    def test_the_checker_accepts_the_same_block_unindented(self) -> None:
        """Other half: the check must object to the indentation, not to the code."""
        fine = "import json\nprint(json.dumps({}))\n"
        compile(EXPANSION.sub(PLACEHOLDER, _shell_unescape(fine)), "planted", "exec")


class TestTheExtractorMirrorsTheShell:
    """If extraction diverges from what the shell does, the compile above tests fiction."""

    @pytest.mark.parametrize("raw,expected", [
        (r'print(f\"hi\")', 'print(f"hi")'),
        (r"a = \\", "a = \\"),
        (r"cost = \$5", "cost = $5"),
        # `\\` collapses to one backslash; the following `\n` is a literal backslash-n,
        # which the shell leaves alone because n is not one of the four special characters.
        (r"x = 1 \\\n", "x = 1 " + "\\" + "\\n"),
    ])
    def test_unescape_matches_double_quoted_shell_rules(self, raw, expected) -> None:
        assert _shell_unescape(raw) == expected

    def test_a_backslash_newline_is_removed(self) -> None:
        """Inside double quotes the shell joins the lines and both characters disappear."""
        assert _shell_unescape("a = (1,\\\n2)") == "a = (1,2)"

    def test_a_single_quote_is_left_alone(self) -> None:
        """python's own string quoting must survive extraction untouched."""
        assert _shell_unescape("x = 'lit'") == "x = 'lit'"

    @pytest.mark.parametrize("raw", ["$VAR", "${VAR}", "$(cmd arg)", "$1", "$@"])
    def test_every_expansion_form_is_substituted(self, raw: str) -> None:
        assert EXPANSION.sub(PLACEHOLDER, f"f({raw})") == f"f({PLACEHOLDER})"

    def test_substitution_leaves_parseable_source(self) -> None:
        assert compile(EXPANSION.sub(PLACEHOLDER, "print('$VAR', ${X})"),
                       "t", "exec") is not None

    def test_a_documented_example_in_a_comment_is_skipped(self) -> None:
        """writ-bash-write-gate.sh explains the gate's blind spot with an abbreviated
        one-liner. It is prose, it will never compile, and failing on it would make the
        test dictate comment wording."""
        assert _in_comment('# python3 -c "json.dump(...)"', 5)

    def test_real_code_is_not_treated_as_a_comment(self) -> None:
        """Anti-vacuity: an over-eager comment check would silently skip every block and
        make the whole file vacuous."""
        assert not _in_comment('X=$(python3 -c "import json")', 5)

    def test_a_trailing_comment_after_code_is_not_skipped(self) -> None:
        """`#` appearing later on a code line does not make the line a comment."""
        assert not _in_comment('X=$(python3 -c "pass")  # note', 5)
