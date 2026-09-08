# ADR: a guard normalizes the text it parses, at the parser, not at the producer

Status: accepted (plan `2412ba38-51e1-4b73-895b-7b240a3c21d3`, 2026-09-08)

## Context

The Stop hook that refuses when tests fail was switched off by color.

`hooks/scripts/writ-run-pending-tests.sh` deliberately does not trust the runner's exit
code, because PHPUnit and strict-warning pytest runs exit non-zero for environment warnings
with no failing test. So it asks `bin/lib/emit-summary.py` instead and treats an empty
summary as a pass. Every text pattern in that parser is anchored with `^` under `re.M`:

- `summarize_pytest`: `^FAILED .*$` and `^E\s+.*$`
- `summarize_phpunit`: `^(FAILURES!|ERRORS!)` and `^\d+\) .*::.+$`
- `summarize_gotest`: `^--- FAIL: .*$`

Runners color the LEADING token of exactly those lines. `FORCE_COLOR=3` is live in this
environment, the hook's child inherits it, and pytest writes SGR escapes into the log even
though its stdout is a file. An escape byte then sits between the line start and the anchor's
first literal character, so no anchor can match. The parser found nothing, printed nothing,
and the hook took the empty summary as a pass and exited 0 with a real test failure on disk.

Measured on the same bytes of the real log the hook produced, which is what settled the
sequence to strip:

    raw log, unmodified:            parser stderr empty,      hook exit 0
    same bytes, SGR escapes removed: [ENF-TEST-001] 1 test failure(s). First: FAILED tests/...

The failure mode is silence, which is why it survived: the hook is designed to be silent on
pass, so a disabled refusal and a clean run are byte-identical from outside. It reddened two
pre-existing firedrill pins on this machine
(`test_triggered_shape_and_record[run-pending-tests]` and
`test_exit_code_is_pinned_at_one[run-pending-tests]`) and stayed green in CI, because
`.github/workflows/pr.yml` sets no `FORCE_COLOR` and so no color ever reached the child
there. A control that only fires on a machine which happens to export one variable is not a
control, which is why this cycle's new end-to-end cases PIN the variable in the child
environment instead of inheriting it.

### The lesson was already learned, one layer over

`tests/_ansi.py` has carried this exact regex since it was written:

```python
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
```

Its module docstring opens with "CI forces color". So the repository already knew that these
producers emit SGR escapes, already knew that content assertions have to normalize before
matching, and applied that knowledge only to the test side. The production parser, the one
whose no-match is an ALLOW, never got it. This is another one-copy-fixed divergence: the same
shape as every other defect in this tree where a fix landed at one of two sites that share a
property.

## Decision

**The layer that DECIDES is the layer that normalizes its input.** Whatever a guard cannot
read is an allow, so a guard must not parse a producer's presentation bytes.

Concretely, `bin/lib/emit-summary.py` gains a module-level pattern and a helper, applied ONCE
in `main()` after the log read and before the `DISPATCH` call:

```python
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_SGR.sub("", text)
```

One site, not one per format. All five anchored patterns and all four formats flow through
that single call, including formats nobody has added yet. Stripping inside each summarizer
would be three copies of the same line, which is the exact duplication shape this repository
has been paying for, and a fourth text format added later would silently arrive unprotected.

Before the dispatch call, not after: the first-finding line the parser captures is printed
straight to stderr for the agent to read, so normalizing after the match would leave escape
bytes in the agent-facing summary. Both directions are pinned in
`tests/test_emit_summary.py`, which asserts the summary is produced AND that no escape byte
reaches stderr, for all three text formats.

### The producer arm is HYGIENE and is explicitly not the control

`hooks/scripts/writ-run-pending-tests.sh` also prefixes the runner invocation:

```bash
env -u FORCE_COLOR -u PY_COLORS NO_COLOR=1 timeout 60s bash -c "$cmd" 2>&1
```

The two variables are the two that appear in pytest's own precedence chain, read off the
installed `_pytest/_io/terminalwriter.py` (`should_do_markup`, lines 36 to 47) rather than
swept speculatively: `PY_COLORS == "1"` returns True, `PY_COLORS == "0"` returns False, a
truthy `NO_COLOR` returns False, a truthy `FORCE_COLOR` returns True, otherwise isatty. So
`NO_COLOR=1` outranks `FORCE_COLOR` but NOT `PY_COLORS=1`, and that is the whole reason
`PY_COLORS` is unset as well.

That arm exists for a reason which is not enforcement: the log path is handed to the agent to
read (`Full log: <path>`), and a log full of escape bytes costs tokens and reads badly.

It CANNOT be the guard, and this is measured rather than argued. `runner_command` is project
configuration: `bin/lib/test_paths.py` reads it from `<cwd>/.claude/writ.json`, so a project
can legitimately configure `pytest --color=yes`, and pytest's `--color` command-line option
outranks every environment variable in the chain above. No environment manipulation inside
the hook can guarantee a plain log.

**The asymmetry is pinned in tests, not left to prose.** The end-to-end case that pins
`FORCE_COLOR=3` in the child environment goes green if EITHER arm lands, and its docstring
says so, so it is not the parser's witness. A second case configures a stub runner that
prints its failure line with a hardcoded SGR escape, never consulting any variable, so it is
immune to the hygiene arm and reddens if and only if the parser strip is missing. A third
assertion, on the same run as the first case, requires the produced `last-test-run.log` to
contain no escape byte, which is the hygiene arm's own witness and reddens when the prefix is
removed while the parser cases stay green. Each arm therefore has a test that only it can
satisfy.

## The widening protocol

SGR only: escape, open bracket, numeric parameters, the letter `m`. A wider pattern would not
have been more correct on the measured input, only less reviewable.

**A new sequence is added together with a fixture containing that exact sequence, taken from
a real runner's output. A widening with no fixture is speculation and is rejected.**

Deliberately absent, named here so a later reader does not think they were forgotten:

- Cursor and erase sequences (escape, bracket, `2K`) and OSC strings. Not observed in the
  measured log.
- Charset selection (escape, open paren, `B`). Not observed either.
- A bare carriage return, which defeats these same anchors for a DIFFERENT reason: `re.M`
  makes `^` match at the string start and after a newline only, so a carriage return is not a
  line boundary and a `FAILED` preceded by one would not match. It is not handled because the
  measured log's `FAILED` lines matched immediately after the SGR strip, which is direct
  evidence there is no carriage return in front of them. If one is ever observed, it arrives
  under the protocol above, with its fixture.

## Alternatives rejected

**Reuse `tests/_ansi.py::plain()`.** Rejected for two reasons, and the second is the one that
matters. `bin/lib/` cannot import from `tests/`, which is only a path problem. But `plain()`
also replaces box borders and collapses all whitespace with `" ".join(...split())`, which
would fold the entire log into ONE line and destroy the line structure every anchored pattern
in the parser depends on. Calling it here would have traded one silent no-match for another.
What the two layers share is the knowledge, not the function, and this document is the
durable form of the shared knowledge.

**Pin the two spellings equal across the two files.** Rejected: coupling a production script's
source text to a test helper's source text couples two layers whose requirements differ (one
must preserve line structure, the other must destroy it). The property is pinned behaviorally
instead: for each colored fixture, no escape byte survives into what the parser reads or into
what reaches stderr.

**Strip inside each summarizer.** Rejected: three copies of one line, and a fourth format
added later inherits nothing.

**Suppress color at the producer only.** Rejected: defeatable from above by project
configuration, as measured. It ships as hygiene, never as the control.

**Trust the runner's exit code instead of parsing.** Not on the table, and the reason predates
this cycle: PHPUnit and strict-warning pytest runs exit non-zero without a failing test, which
is precisely why the parser became the decider.

## Consequences accepted

**The JSON format flows through the strip and its behavior does not change.** One structural
reason and one measured one:

- RFC 8259 forbids unescaped control characters inside strings, so a well-formed analyzer
  document has no raw escape byte, and a pattern matching the literal escape byte cannot
  alter a document that has none.
- Measured during implementation rather than carried as a reasoned fact: `json.loads` REJECTS
  a raw escape byte inside a string with `JSONDecodeError: Invalid control character`, and
  `json.dumps` writes color as the six-character escaped spelling (backslash, `u`, `0`, `0`,
  `1`, `b`) which decodes back to a real escape byte. So a parseable document carries color
  only as six ordinary characters, which `_ANSI_SGR` does not match, and the document reaches
  `json.loads` byte-identical. Both directions are pinned as tests.

The JSON path was never in danger anyway: `hooks/scripts/validate-file.sh` is the parser's
only other caller, it calls with `--format json`, and its `exit 1` sits inside the
`if [ $EXIT_CODE -ne 0 ]` branch, so it never consults the summary at all. That gate cannot
be disabled by an empty summary.

**A guard can now report a failure that used to be invisible.** If a lint document ever
contained a raw escape byte it was silent before the change and is still silent (it does not
parse either way), but any log whose failure lines were colored now produces a finding where
it produced none. That is strictly more information and never less, and it is the point.

**`env -u` is a GNU coreutils dependency in `hooks/scripts/`, and the first one.** `env`
execs `timeout`, so the exit status the `|| rc=$?` chain captures is still `timeout`'s,
including 124 on a timeout. Confirmed by running the drill, not by reading it.

**The prefix is inert for two of the three runners, and that is fine.** The default go test
`runner_command` emits no color of its own, and PHPUnit takes its color decision from a CLI
flag and its XML configuration rather than from `NO_COLOR` (not verified here: this tree has
no PHPUnit install). A hygiene arm being inert for a runner costs nothing, because the guard
arm covers all three regardless. This is one more reason the parser is the guard.
