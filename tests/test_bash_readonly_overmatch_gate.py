"""Program item 1g: a command that only READS a project path is not a Bash write.

THE DEFECT, from this session's own transcripts (2026-10-06). Read-only explorer and
test-writer sub-agents were refused by the Bash write gate for "writing" files they only read.
Every captured refusal of that kind had one shape: a stdin-fed interpreter
(`python3 - <<'E' ... E`) somewhere on the line, and a `sed -n`, `grep` or `cat` naming a
project path in ANOTHER command of the same line. The extractor's stdin pass scanned the whole
command line for path literals, so the read's path became a write target and reached the
role-scope arm ([ENF-ROLE-SCOPE]). One more refused an interpreter spelled by path as a write to
the interpreter binary.

The fix narrows the stdin scan to the interpreter's pipeline, the heredoc bodies opened in it and
every assignment word, and applies only on lines whose other commands write no file. The pins
below hold BOTH halves: the reads now pass, and nothing the old scan gated has stopped being gated.
"""
from __future__ import annotations

import pytest

from tests.test_bash_interpreter_write_gate import (  # noqa: F401  (gate is a fixture)
    _blocked,
    _run_hook,
    gate,
)
from tests.test_bash_write_gate import _extract, _extractor_src, run_extractor


def _local(rel: str) -> tuple[str, str]:
    return ("local", f"/proj/{rel}")


def _rows(lines: list[str]) -> set[tuple[str, ...]]:
    return {tuple(line.split("\t", 1)) for line in lines if "\t" in line}


def _mutated_rows(anchor: str, replacement: str, cmd: str) -> set[tuple[str, ...]]:
    src = _extractor_src()
    assert src.count(anchor) == 1, anchor
    _proc, lines = run_extractor(cmd, "/proj", src=src.replace(anchor, replacement))
    return _rows(lines)


READ_ONLY = {
    "heredoc-then-sed-and-grep": (
        "cd /proj; python3 - <<'E'\nimport json\nprint(json.dumps({'a': 1}))\nE\n"
        "sed -n 192,225p src/common.py; grep -rn HOOK_IS_ERROR hooks bin 2>/dev/null | head -5"
    ),
    "grep-then-heredoc-then-sed": (
        "cd /proj; grep -rn \"HOOK_IS_ERROR\" hooks bin --include=*.sh | head -5; "
        "python3 - <<'E'\nimport json\ng = json.load(open('tests/fixtures/q.json'))\n"
        "print(len(g))\nE\nsed -n 1,40p src/schema.py"
    ),
    "interpreter-by-path": "/opt/venv/bin/python - <<'E' 2>&1 | tail -14\nprint(1)\nE",
    "grep-alone": "grep -n foo src/x.py",
}

STILL_WRITES_EXACT = {
    "body-write-then-unrelated-read": (
        "python3 - <<'E'\nopen('src/x.py','w').write('x')\nE\nsed -n 1p src/y.py",
        {_local("src/x.py")},
    ),
    "body-attributed-across-and": (
        "python3 - <<'E' && ls src/z.py\nopen('src/x.py','w')\nE",
        {_local("src/x.py")},
    ),
    "redirect-in-another-command": (
        "echo x > src/a.py; python3 - <<'E'\nprint(1)\nE",
        {_local("src/a.py")},
    ),
    "assigned-then-written-by-the-program": (
        "F=src/x.py; python3 - <<'E'\nimport os\nopen(os.environ['F'],'w')\nE",
        {_local("src/x.py")},
    ),
}

STILL_WRITES_MEMBER = {
    "pipe-feeds-the-program": ("cat notes.md | python3 -", _local("notes.md")),
    "printf-feeds-an-argument-free-interpreter": (
        "printf \"open('src/a.py','w')\" | python3", _local("src/a.py")),
    "exported-then-passed-as-an-argument": (
        "export F=src/x.py; python3 - \"$F\" <<'E'\nimport sys\nopen(sys.argv[1],'w')\nE",
        _local("src/x.py")),
    "program-written-to-a-file-first": (
        "printf \"open('src/x.py','w')\" > /tmp/p.py; python3 - < /tmp/p.py",
        _local("src/x.py")),
    "heredoc-program-written-to-a-file-first": (
        "cat > /tmp/p.py <<'E'\nopen('src/x.py','w')\nE\npython3 - < /tmp/p.py",
        _local("src/x.py")),
}


class TestReadOnlyCommandsYieldNoTarget:
    @pytest.mark.parametrize("cmd", list(READ_ONLY.values()), ids=list(READ_ONLY))
    def test_no_write_target_is_extracted(self, cmd):
        assert _extract(cmd) == set(), cmd

    @pytest.mark.parametrize("cmd", list(READ_ONLY.values()), ids=list(READ_ONLY))
    def test_the_real_hook_allows_it_before_any_approval(self, gate, cmd):
        assert _run_hook(gate, cmd.replace("/proj", str(gate.proj))) is None, cmd


class TestCoverageIsNotReduced:
    @pytest.mark.parametrize("cmd, expected", list(STILL_WRITES_EXACT.values()),
                             ids=list(STILL_WRITES_EXACT))
    def test_exactly_the_written_path_is_a_target(self, cmd, expected):
        assert _extract(cmd) == expected, cmd

    @pytest.mark.parametrize("cmd, expected", list(STILL_WRITES_MEMBER.values()),
                             ids=list(STILL_WRITES_MEMBER))
    def test_a_path_that_can_reach_the_program_is_still_a_target(self, cmd, expected):
        assert expected in _extract(cmd), cmd

    def test_a_heredoc_body_write_is_refused_through_the_hook(self, gate):
        cmd = "python3 - <<'E'\nopen('src/a.py','w').write('x')\nE\nsed -n 1,5p README.md"
        out = _run_hook(gate, cmd)
        assert _blocked(out), out
        assert "a.py" in out.get("permissionDecisionReason", "")

    def test_an_assigned_path_written_by_the_program_is_refused_through_the_hook(self, gate):
        cmd = "F=src/a.py; python3 - <<'E'\nimport os\nopen(os.environ['F'],'w')\nE"
        out = _run_hook(gate, cmd)
        assert _blocked(out), out
        assert "a.py" in out.get("permissionDecisionReason", "")


class TestEachNarrowingRuleIsLoadBearing:
    """Mutation proofs: undo one rule in a copy of the extractor and the matching pin flips."""

    def test_scanning_the_whole_line_brings_the_false_read_target_back(self):
        rows = _mutated_rows("else stdin_source_tokens(stream))", "else stream)",
                             READ_ONLY["heredoc-then-sed-and-grep"])
        assert _local("src/common.py") in rows, rows

    def test_keeping_the_verb_token_brings_the_interpreter_target_back(self):
        rows = _mutated_rows("skip = arg0 - 1 if verb else -1", "skip = -1",
                             READ_ONLY["interpreter-by-path"])
        assert ("outside", "/opt/venv/bin/python") in rows, rows

    def test_narrowing_a_line_that_writes_a_file_loses_the_program_it_wrote(self):
        rows = _mutated_rows(
            "scan_tokens(stream if line_writes else stdin_source_tokens(stream))",
            "scan_tokens(stdin_source_tokens(stream))",
            STILL_WRITES_MEMBER["program-written-to-a-file-first"][0])
        assert _local("src/x.py") not in rows, rows

    def test_dropping_assignment_words_loses_the_assigned_path(self):
        rows = _mutated_rows(
            "out += [tok for tok in seg if ASSIGNMENT.match(dequote(strip_group_opener(tok)))]",
            "pass",
            STILL_WRITES_EXACT["assigned-then-written-by-the-program"][0])
        assert _local("src/x.py") not in rows, rows


class TestTheHeredocWalkIsShared:
    """heredoc_spans is the one body walk; strip_heredoc_bodies is built on it."""

    def _names(self):
        from writ.session.bash_tokens import SEP, heredoc_spans, strip_heredoc_bodies
        return SEP, heredoc_spans, strip_heredoc_bodies

    def test_a_body_is_found_between_its_newline_and_its_terminator(self):
        sep, spans, strip = self._names()
        toks = ["python3", "-", "<<'E'", sep, "open('src/x.py','w')", sep, "E", sep, "ls"]
        assert spans(toks) == [(2, 3, 6, 7)]
        assert strip(toks) == ["python3", "-", "<<'E'", sep, "ls"]

    def test_an_opener_with_no_newline_has_no_body(self):
        sep, spans, strip = self._names()
        toks = ["cat", "<<'E'", ">", "out.txt"]
        assert spans(toks) == []
        assert strip(toks) == toks

    def test_an_unterminated_body_runs_to_the_end(self):
        sep, spans, strip = self._names()
        toks = ["python3", "<<E", sep, "print(1)"]
        assert spans(toks) == [(1, 2, 4, 4)]
        assert strip(toks) == ["python3", "<<E"]

    def test_the_gate_imports_the_shared_walk(self):
        assert "heredoc_spans" in _extractor_src()
