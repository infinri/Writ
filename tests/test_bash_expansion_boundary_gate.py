"""The Bash write gate performs the shell expansions its project-boundary
classification depends on (plan.md / capabilities.md, session
dfacff61-23d5-474e-846c-2e2f0f0ea482).

THE ORACLE, reproduced here rather than restated as a table: for a spelling S, a
REAL `bash -c 'printf "%s\\n" S'` process is asked what S resolves to, under the
SAME environment and cwd the extractor is given. A spelling is a MATCH when the
extractor's classified abspath equals that oracle answer (after the same
cwd-join/normpath the classification loop itself applies); a DIVERGE when the
extractor's abspath equals the pre-fix LITERAL join and DIFFERS from the oracle
(a disclosed, deliberate residue); an UNKNOWN when the extractor emits an
`unknown` row and no `local`/`outside` row at all, because a parameter it cannot
resolve makes the apparent path stop being evidence.

Every entry's verdict is judged by running the real shell at test time. Nothing
here hardcodes "this spelling denies, that one allows" -- adding a spelling to the
population requires no new expected value, only a new Case naming the verdict
class the property itself must then prove.

RED-BY-CONSTRUCTION, not by skeleton. `expand_word` does not exist in
hooks/scripts/writ-bash-write-gate.sh yet, so the MATCH-class assertions below
fail today for the real reason: the extractor's classified abspath is the
pre-fix literal cwd-join, which disagrees with what the oracle says a real shell
does. The few tests that depend on a marker that does not exist AT ALL yet (the
`UNEXPANDED FORMS` header block, the `expand_word` function itself) call
`pytest.fail` with a `skeleton:` message instead, matching this suite's existing
convention (tests/test_project_boundary.py, tests/firedrill/test_refusal_inventory.py)
of failing loudly rather than skipping.

SCOPE NOTE on the DIVERGE population (say-so-loudly, per the dispatch brief): this
file's DIVERGE class covers exactly the three directory-stack tilde forms plan.md's
Analysis section 1 names and gives concrete oracle-testable examples for (`~+`,
`~-`, a numbered `~N`). plan.md's "Deliberately NOT performed" list also names
command substitution, process substitution, arithmetic expansion, brace expansion,
pathname expansion, the parameter-expansion operator forms, and positional/special
parameters -- none of which capabilities.md's 17 items name an oracle-testable
spelling for, and building real, non-flaky bash oracles for all of them (glob
expansion in particular needs real matching files on disk) is out of this file's
scope. The header-ratchet below is therefore a consistency check between THIS
population and the header block, not an exhaustive enumeration of every
unimplemented bash mechanism; if the implementer's header block also discloses the
other mechanisms, that is additional, uncontradicted documentation this file does
not pin either way.

ENVIRONMENT NOTE, since HOME's un/set-ness is part of what is under test: every
case builds its OWN env dict rather than touching the real process environment,
copying `os.environ` first (so PATH etc. still resolve `bash`/`python3`) and then
pinning HOME, OLDPWD and a second, HOME-independent variable to real, EXISTING
directories under `tmp_path` (a real bash on this host silently ignores an
inherited OLDPWD that does not point at a directory it can chdir into for `cd -`
bookkeeping, so a synthetic marker string is not good enough here) and popping the
sentinel unset name so its absence is a property of the harness, never a
coincidence of whatever the developer's own shell happens to export. The SAME env
dict is handed to both the extractor subprocess and the oracle shell for every
case: comparing a shell running under a DIFFERENT environment than the extractor
saw would prove nothing.
"""
from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

# autouse: pins cwd to a sandbox, this suite's standing convention (tests/test_bash_write_gate.py).
from tests.fixtures.session_state import sandbox_cwd  # noqa: F401
from tests.test_bash_write_gate import (
    HOOK_SH,
    SKILL_ROOT,
    _extractor_src,
    _run_hook,
    _seed,
)
from tests.test_project_boundary import _post_approval_cache
from tests.test_worktree_safety_extractor import _run as _run_worktree_hook
from tests.test_worktree_safety_extractor import cache_root, project  # noqa: F401

SENTINEL_UNSET_VAR = "NOPE_UNSET"
SECOND_VAR_NAME = "WRIT_EXPANSION_TEST_VAR2"
LOGIN = getpass.getuser()


def _gates():
    from writ.session import gates
    return gates


# --------------------------------------------------------------------------- #
# Fixture: one real, isolated environment per test, built fresh from tmp_path.
# --------------------------------------------------------------------------- #
@dataclass
class Fixture:
    tmp_path: Path
    project: Path
    home: Path
    oldpwd: Path
    var2_dir: Path
    stackdir: Path
    env: dict            # HOME present (the fixture home dir)
    env_no_home: dict     # HOME genuinely absent
    env_empty_home: dict  # HOME present but EMPTY, which bash treats as its value


def _build_env(*, home: str | None, oldpwd: str, var2: str) -> dict:
    env = dict(os.environ)
    if home is None:
        env.pop("HOME", None)
    else:
        env["HOME"] = home
    env.pop(SENTINEL_UNSET_VAR, None)
    env["OLDPWD"] = oldpwd
    env[SECOND_VAR_NAME] = var2
    return env


@pytest.fixture
def boundary_fixture(tmp_path: Path) -> Fixture:
    project_dir = tmp_path / "proj"
    home_dir = tmp_path / "home"
    oldpwd_dir = tmp_path / "oldpwd"
    var2_dir = tmp_path / "var2target"
    stackdir = tmp_path / "stackdir"
    for d in (project_dir, home_dir, oldpwd_dir, var2_dir, stackdir):
        d.mkdir()
    env = _build_env(home=str(home_dir), oldpwd=str(oldpwd_dir), var2=str(var2_dir))
    env_no_home = _build_env(home=None, oldpwd=str(oldpwd_dir), var2=str(var2_dir))
    # HOME="" is NOT the same as HOME unset, and the difference is a real divergence:
    # `env -i HOME= bash -c 'printf %s ~/x'` prints `/x`, while with HOME genuinely unset
    # bash consults the password database. Measured on this machine.
    env_empty_home = _build_env(home="", oldpwd=str(oldpwd_dir), var2=str(var2_dir))
    return Fixture(tmp_path, project_dir, home_dir, oldpwd_dir, var2_dir, stackdir,
                    env, env_no_home, env_empty_home)


# --------------------------------------------------------------------------- #
# The two real-process runners: the extractor (production code under test) and
# the oracle (a real bash, asked the identical question).
# --------------------------------------------------------------------------- #
def _extract_rows(cmd: str, cwd: Path, env: dict) -> list[tuple[str, ...]]:
    """Every row the extractor prints for CMD, as FULL tab-split tuples -- not
    the 2-column shape tests/test_bash_write_gate.py's `_extract()` assumes,
    because the `unknown` row this cycle adds may carry more than one field
    after the kind column."""
    full_env = dict(env, WRIT_BASH_CMD=cmd, WRIT_CWD=str(cwd))
    p = subprocess.run([sys.executable, "-c", _extractor_src()], env=full_env,
                       capture_output=True, text=True, timeout=15)
    assert p.returncode == 0, (cmd, p.returncode, p.stdout, p.stderr)
    rows: list[tuple[str, ...]] = []
    for line in p.stdout.splitlines():
        if "\t" in line:
            rows.append(tuple(line.split("\t")))
    return rows


def _classified_abspath(rows: list[tuple[str, ...]]) -> str:
    hits = [r for r in rows if r and r[0] in ("local", "outside")]
    assert len(hits) == 1, f"expected exactly one local/outside row, got {rows!r}"
    return hits[0][1]


def _oracle_word(spelling: str, cwd: Path, env: dict, *, prelude: str = "") -> str:
    """Ask a real bash process what SPELLING resolves to in ARGUMENT position.

    Contract: exactly one line of output. `prelude` may run setup statements that
    build shell state the spelling itself depends on (directory-stack `pushd`s for
    the `~N` residue form); it must never itself print anything (redirected to
    /dev/null by its own callers).
    """
    script = "%sprintf '%%s\\n' %s" % (prelude, spelling)
    p = subprocess.run(["bash", "-c", script], cwd=str(cwd), env=env,
                       capture_output=True, text=True, timeout=10)
    assert p.returncode == 0, (spelling, p.returncode, p.stdout, p.stderr)
    lines = p.stdout.splitlines()
    assert len(lines) == 1, (
        f"oracle contract violated for {spelling!r}: expected exactly one line, "
        f"got {lines!r} (stderr={p.stderr!r})"
    )
    return lines[0]


def _expected_abspath(cwd: Path, oracle_word: str) -> str:
    """The SAME join/normpath the classification loop itself applies, the line reading
    `ap = t if os.path.isabs(t) else os.path.normpath(os.path.join(cwd, t))` in the hook
    (quoted rather than cited by number: this cycle's own insertions moved it, and a
    line citation here has gone stale twice already): absolute words pass through,
    relative words join under cwd.
    This is not the thing under test -- the EXPANSION is -- so it is computed
    identically for every entry rather than asserted per-case."""
    return oracle_word if os.path.isabs(oracle_word) else os.path.normpath(
        os.path.join(str(cwd), oracle_word))


def _literal_join(cwd: Path, word: str) -> str:
    """The pre-fix behaviour: no expansion at all, just today's cwd-join. Used
    only by the DIVERGE assertions to prove the gate's abspath is EXACTLY that
    (not merely "not the oracle's"), so a divergence that silently changes shape
    also reddens."""
    return word if os.path.isabs(word) else os.path.normpath(os.path.join(str(cwd), word))


def _unexpanded_forms_from_header(source: str) -> set[str]:
    """Names-only block the hook header must carry, parsed the same way
    tests/test_bash_egress_gate.py's `uncovered_prefix_names` parses its own
    sibling `UNCOVERED PREFIXES` block."""
    try:
        start = source.index("# UNEXPANDED FORMS BEGIN")
    except ValueError:
        pytest.fail(
            "skeleton: hooks/scripts/writ-bash-write-gate.sh has no "
            "'# UNEXPANDED FORMS BEGIN' block yet (plan.md ## Files)"
        )
    start = source.index("\n", start) + 1
    end = source.index("# UNEXPANDED FORMS END", start)
    body = " ".join(line.lstrip("#").strip() for line in source[start:end].splitlines())
    return {n.strip() for n in body.split(",") if n.strip()}


# --------------------------------------------------------------------------- #
# The population. Every entry's verdict is PROVED, never assumed, by the tests
# below; this table only names a spelling, its verdict CLASS, and (for DIVERGE)
# the form name the header block must also carry.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Case:
    id: str
    spelling_template: str          # may contain "{p}", substituted with a unique probe basename
    verdict: str                    # "MATCH" | "DIVERGE" | "UNKNOWN"
    home: str = "fixture"           # "fixture" | "none" | "empty"
    form: str | None = None         # required when verdict == "DIVERGE"
    prelude_builder: Callable[[Fixture], str] = field(default=lambda fx: "")


POPULATION: list[Case] = [
    # -- the six measured bypasses (resolve OUTSIDE today, must classify `outside`
    #    and deny via the project boundary once expanded) --
    Case("bare_tilde_slash", "~/{p}", "MATCH"),
    Case("bare_tilde", "~", "MATCH"),
    Case("tilde_login_dotted", "~%s/{p}" % LOGIN, "MATCH"),
    Case("dollar_home", "$HOME/{p}", "MATCH"),
    Case("dollar_brace_home", "${HOME}/{p}", "MATCH"),
    Case("double_quoted_dollar_home", '"$HOME/{p}"', "MATCH"),

    # -- the four legitimate in-project spellings that must STAY allowed --
    Case("double_quoted_tilde_stays_inside", '"~/{p}"', "MATCH"),
    Case("single_quoted_tilde_stays_inside", "'~/{p}'", "MATCH"),
    Case("single_quoted_dollar_home_stays_inside", "'$HOME/{p}'", "MATCH"),
    Case("mid_word_tilde_stays_inside", "src/~/{p}", "MATCH"),

    # -- additional MATCH controls named in plan.md's Test design section --
    Case("plain_relative_control", "src/{p}", "MATCH"),
    Case("dotted_relative_control", "./src/{p}", "MATCH"),
    Case("dotdot_traversal_control", "../../../etc/{p}", "MATCH"),
    Case("escaped_tilde", "\\~/{p}", "MATCH"),
    Case("escaped_dollar_home", "\\$HOME/{p}", "MATCH"),
    # MEASURED FINDING, reported loudly rather than adjusted to fit (dispatch brief's
    # own instruction): `shlex.split(..., posix=False)` -- unchanged by this plan, and
    # the ONLY tokenizer in this file -- splits a QUOTE-THEN-UNQUOTED word into TWO
    # tokens at the closing quote (verified directly: `shlex.split('cp x "$HOME"/y',
    # posix=False)` yields `['cp', 'x', '"$HOME"', '/y']`, four tokens, not three), while
    # an UNQUOTED-THEN-QUOTE word (`partial_quote_suffix` below, `$HOME"/{p}"`) and a
    # TILDE-THEN-QUOTE word (`tilde_quoted_suffix` below, `~/"{p}"`) both stay ONE token.
    # For a `cp` destination this means `cand[-1]` (the last non-flag positional) picks
    # only the trailing `/{p}` fragment and silently drops the quoted `"$HOME"` fragment
    # as an earlier, ignored positional -- a token-BOUNDARY defect, not an expansion one.
    # `expand_word` runs PER TOKEN (plan.md's own words: "a single left-to-right scan"
    # over one collected token), so it cannot reunite two tokens shlex already split
    # apart. This entry is kept as a MATCH assertion (the desired end state really is
    # "resolves like $HOME/{p}") rather than removed or weakened: if it is STILL red
    # after expand_word ships exactly as plan.md describes, that redness is real signal
    # that this token-adjacency gap needs its own fix, not a sign the test is wrong.
    Case("partial_quote_prefix", '"$HOME"/{p}', "MATCH"),
    Case("partial_quote_suffix", '$HOME"/{p}"', "MATCH"),
    Case("tilde_quoted_suffix", '~/"{p}"', "MATCH"),
    Case("second_env_var_not_home", "$%s/{p}" % SECOND_VAR_NAME, "MATCH"),
    Case("tilde_root", "~root", "MATCH"),
    Case("tilde_unknown_login", "~nosuchuser123456xyz/{p}", "MATCH"),
    Case("single_quoted_nope_unset_stays_inside", "'$%s/{p}'" % SENTINEL_UNSET_VAR, "MATCH"),
    Case("home_removed_tilde_fallback", "~/{p}", "MATCH", home="none"),
    # HOME PRESENT BUT EMPTY is a THIRD state, not a spelling of "unset". Review caught
    # the gate conflating them with a falsiness test, so an empty HOME fell back to the
    # password database while bash uses the empty value and resolves `~/x` to `/x`. That
    # matters wherever the project root IS the invoking user's real home, because the
    # passwd answer then reads as in-project for a write the shell sends to the
    # filesystem root. Reddens when the tilde branch tests `not val` instead of
    # `val is None`.
    Case("empty_home_uses_the_empty_value", "~/{p}", "MATCH", home="empty"),

    # -- the three directory-stack tilde forms: disclosed residue, DIVERGE --
    Case("tilde_pwd", "~+/{p}", "DIVERGE", form="tilde-pwd"),
    Case("tilde_oldpwd", "~-/{p}", "DIVERGE", form="tilde-oldpwd"),
    Case("tilde_dirstack", "~1/{p}", "DIVERGE", form="tilde-dirstack",
         prelude_builder=lambda fx: "pushd %s >/dev/null; " % shlex.quote(str(fx.stackdir))),

    # -- the unresolved-variable ask --
    Case("nope_unset_unknown_ask", "$%s/{p}" % SENTINEL_UNSET_VAR, "UNKNOWN"),
]

MATCH_CASES = [c for c in POPULATION if c.verdict == "MATCH"]
DIVERGE_CASES = [c for c in POPULATION if c.verdict == "DIVERGE"]
UNKNOWN_CASES = [c for c in POPULATION if c.verdict == "UNKNOWN"]


def _case_env(fx: Fixture, case: Case) -> dict:
    if case.home == "fixture":
        return fx.env
    return fx.env_empty_home if case.home == "empty" else fx.env_no_home


# The one MATCH case `expand_word` cannot satisfy, because the defect is in the TOKENIZER
# and not in expansion. Measured: `shlex.split('cp x "$HOME"/y', posix=False)` yields
# ['cp', 'x', '"$HOME"', '/y'], two tokens, so the destination picker takes only the
# trailing fragment and the quoted one is dropped as an earlier positional. `expand_word`
# runs per already-collected token and cannot reunite them, so closing this needs a
# tokenizer change, which is a different cycle with its own evidence.
#
# strict=True ON PURPOSE: when that cycle lands, this flips to XPASS and FAILS, which is
# the prompt to delete the marker. A non-strict xfail would let a fixed defect go on
# advertising itself as broken forever.
TOKEN_BOUNDARY_XFAIL = {
    "partial_quote_prefix": (
        "token-boundary defect, not an expansion one: shlex.split(posix=False) splits "
        '\'"$HOME"/x\' into two tokens and the destination picker sees only "/x". '
        "Closing it requires a tokenizer change, out of scope for this cycle."
    ),
}


def _match_param(case: "Case"):
    """Wrap a case in pytest.param when a known separate defect makes it xfail."""
    reason = TOKEN_BOUNDARY_XFAIL.get(case.id)
    if reason is None:
        return case
    return pytest.param(case, marks=pytest.mark.xfail(strict=True, reason=reason))


def _case_dest(case: Case) -> str:
    """Substitute the probe basename WITHOUT str.format.

    `.format` reads `{HOME}` in the `${HOME}/{p}` template as a replacement field and
    raises KeyError before the extractor is ever called, so that case could not run on
    any implementation, passing or failing. A brace form is one of the seven measured
    bypasses, so losing it would have left the spelling untested while the file looked
    like it covered it. `.replace` substitutes the one placeholder and leaves every other
    brace alone.
    """
    probe = "probe_%s.txt" % case.id
    return case.spelling_template.replace("{p}", probe)


# --------------------------------------------------------------------------- #
# 0. non-vacuity: the population and every verdict class are non-empty.
# --------------------------------------------------------------------------- #
class TestPopulationIsNonVacuous:
    def test_population_is_non_empty(self):
        """Reddens if POPULATION is ever emptied, which would make every test
        below pass on any tree."""
        assert POPULATION

    @pytest.mark.parametrize("cls", ["MATCH", "DIVERGE", "UNKNOWN"])
    def test_every_verdict_class_has_at_least_one_entry(self, cls):
        assert any(c.verdict == cls for c in POPULATION), cls

    def test_every_diverge_entry_carries_a_form_name(self):
        """Reddens if a divergence entry is added with no form name (capability 14's
        other half)."""
        assert all(c.form for c in DIVERGE_CASES), DIVERGE_CASES


# --------------------------------------------------------------------------- #
# 1. MATCH class: the gate's classified abspath must equal the real oracle's.
# --------------------------------------------------------------------------- #
class TestMatchClassAgreesWithTheRealShell:
    """Reddens for the six bypass spellings if expand_word's matching branch is
    deleted or narrowed: a leading-tilde deletion, a login-name charset narrowed
    to [A-Za-z0-9_] (drops the dotted name), a brace-form drop, a name-HOME
    special case instead of reading the environment, or a HOME-env-only tilde
    with no passwd-database fallback. Reddens for the four legitimate spellings
    (and the other MATCH controls) if expansion is applied to dequote's OUTPUT
    instead of the raw token, which would wrongly resolve all of them and turn
    the fix into false refusals."""

    @pytest.mark.parametrize(
        "case", [_match_param(c) for c in MATCH_CASES], ids=[c.id for c in MATCH_CASES],
    )
    def test_gate_abspath_equals_the_oracles(self, boundary_fixture, case):
        fx = boundary_fixture
        if case.id == "tilde_login_dotted" and "." not in LOGIN:
            pytest.skip("current OS login has no dot; this measured spelling needs one")
        env = _case_env(fx, case)
        dest = _case_dest(case)
        rows = _extract_rows("cp README.md %s" % dest, fx.project, env)
        got = _classified_abspath(rows)
        oracle = _oracle_word(dest, fx.project, env)
        expected = _expected_abspath(fx.project, oracle)
        assert got == expected, (case.id, dest, got, expected, rows)


# --------------------------------------------------------------------------- #
# 2. DIVERGE class: the gate stays literal AND that literal genuinely differs
#    from what a real shell does. Two-sided on purpose.
# --------------------------------------------------------------------------- #
class TestDivergentFormsStayLiteralButTrulyDiverge:
    @pytest.mark.parametrize("case", DIVERGE_CASES, ids=[c.id for c in DIVERGE_CASES])
    def test_diverges_in_both_directions(self, boundary_fixture, case):
        """Reddens if a divergence silently closes (the gate starts expanding and
        now matches the oracle) or silently changes shape (the gate's join no
        longer equals today's un-expanded literal join) -- either way, someone
        must re-label this entry deliberately rather than have it pass quietly."""
        fx = boundary_fixture
        dest = _case_dest(case)
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        got = _classified_abspath(rows)
        literal = _literal_join(fx.project, dest)
        assert got == literal, (
            f"{case.id}: the gate's abspath is no longer today's un-expanded "
            f"literal join ({literal!r}); it changed shape and must be "
            f"re-labelled, not silently accepted: got {got!r}"
        )
        prelude = case.prelude_builder(fx)
        oracle = _oracle_word(dest, fx.project, fx.env, prelude=prelude)
        oracle_abspath = _expected_abspath(fx.project, oracle)
        assert got != oracle_abspath, (
            f"{case.id} no longer diverges from the oracle ({oracle_abspath!r}); "
            "if expand_word now legitimately handles this form, re-label the "
            "entry deliberately rather than leaving a false divergence pin"
        )


# --------------------------------------------------------------------------- #
# 3. UNKNOWN class: an unresolved parameter emits `unknown` and no local/outside
#    row at all -- the raw extractor half of capability 8.
# --------------------------------------------------------------------------- #
class TestUnknownClassEmitsNoLocalOrOutsideRow:
    @pytest.mark.parametrize("case", UNKNOWN_CASES, ids=[c.id for c in UNKNOWN_CASES])
    def test_unknown_row_with_no_local_or_outside_row(self, boundary_fixture, case):
        """Reddens if the unknown branch falls back to appending the literal
        value, which would emit `local` (or `outside`) instead of `unknown`."""
        fx = boundary_fixture
        dest = _case_dest(case)
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        kinds = {r[0] for r in rows if r}
        assert "unknown" in kinds, rows
        assert "local" not in kinds and "outside" not in kinds, rows


# --------------------------------------------------------------------------- #
# capability 1: the six-bypass abspath is refused by the real project-boundary
# predicate, in-process, no daemon, no flake.
# --------------------------------------------------------------------------- #
class TestBypassAbspathIsRefusedByTheProjectBoundary:
    def test_tilde_slash_bypass_is_denied_with_enf_project_boundary(self, boundary_fixture):
        """Reddens if the leading-tilde branch of expand_word is deleted: the row
        returns to `<cwd>/~/probe.txt` and the kind returns to `local`, so this
        abspath is never even OFFERED to `_can_write_check` as an outside path."""
        fx = boundary_fixture
        probe = "probe_boundary_deny.txt"
        rows = _extract_rows("cp README.md ~/%s" % probe, fx.project, fx.env)
        outside_hits = [r for r in rows if r and r[0] == "outside"]
        assert outside_hits, (
            f"expected an `outside` row for a real home-directory bypass; got {rows!r}"
        )
        abspath = outside_hits[0][1]
        assert abspath == str(fx.home / probe), (abspath, rows)
        cache = _post_approval_cache(str(fx.project))
        result = _gates()._can_write_check(
            "expand-boundary-deny", {"tool_input": {"file_path": abspath}}, SKILL_ROOT, cache)
        assert result["can_write"] is False, result
        assert "ENF-PROJECT-BOUNDARY" in (result["reason"] or ""), result


# --------------------------------------------------------------------------- #
# capability 6 (direct half): the four legitimate spellings are not just
# classified `local`, they are actually ALLOWED by `_can_write_check` too.
# --------------------------------------------------------------------------- #
class TestLegitimateInProjectSpellingsStayAllowed:
    LEGIT_TEMPLATES = ['"~/{p}"', "'~/{p}'", "'$HOME/{p}'", "src/~/{p}"]

    @pytest.mark.parametrize("tmpl", LEGIT_TEMPLATES, ids=lambda t: t)
    def test_stays_local_and_can_write_allows_it(self, boundary_fixture, tmpl):
        """Reddens if expansion is applied to the post-dequote value, which
        resolves all four of these and converts the fix into four false
        refusals."""
        fx = boundary_fixture
        dest = tmpl.format(p="probe_legit.txt")
        rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        local_hits = [r for r in rows if r and r[0] == "local"]
        assert local_hits, (dest, rows)
        abspath = local_hits[0][1]
        cache = _post_approval_cache(str(fx.project))
        result = _gates()._can_write_check(
            "expand-boundary-allow", {"tool_input": {"file_path": abspath}}, SKILL_ROOT, cache)
        assert result["can_write"] is True, result


# --------------------------------------------------------------------------- #
# capability 4: the three $HOME spellings resolve to the SAME abspath as each
# other, not merely each matching the oracle independently.
# --------------------------------------------------------------------------- #
class TestTheThreeHomeSpellingsAgreeWithEachOther:
    def test_dollar_home_brace_and_double_quoted_forms_are_identical(self, boundary_fixture):
        """Reddens if the brace form is dropped from the parameter branch, or if
        a double-quoted span is treated like a single-quoted one (any one of the
        three diverging from the other two is the failure)."""
        fx = boundary_fixture
        probe = "probe_home_forms.txt"
        dests = ["$HOME/%s" % probe, "${HOME}/%s" % probe, '"$HOME/%s"' % probe]
        abspaths = set()
        for dest in dests:
            rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
            abspaths.add(_classified_abspath(rows))
        assert abspaths == {str(fx.home / probe)}, abspaths


# --------------------------------------------------------------------------- #
# capability 9: credential classification precedes the unknown-ask arm.
# --------------------------------------------------------------------------- #
class TestCredentialClassificationPrecedesTheUnknownAsk:
    """Reddens if the unknown check is ordered before is_cred in the
    classification loop."""

    def test_unresolved_variable_credential_target_still_denies(self, tmp_path):
        sid = "expand-cred-order-unset"
        _seed(sid, mode="conversation")
        out = _run_hook("cp seed.txt $%s/.env" % SENTINEL_UNSET_VAR, sid, str(tmp_path))
        assert out is not None, "the credential-shaped unresolved target was silent"
        assert out.get("permissionDecision") == "deny", out
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out

    def test_expanded_tilde_credential_target_still_denies(self, tmp_path):
        sid = "expand-cred-order-tilde"
        _seed(sid, mode="conversation")
        out = _run_hook("cp seed.txt ~/.env", sid, str(tmp_path))
        assert out is not None, "the expanded-tilde credential target was silent"
        assert out.get("permissionDecision") == "deny", out
        assert "SEC-CREDENTIAL-WRITE" in out.get("permissionDecisionReason", ""), out


# --------------------------------------------------------------------------- #
# capability 8, full-hook half: the ask itself, naming the unresolved variable.
# --------------------------------------------------------------------------- #
class TestUnresolvedVariableDrivesAnAskThroughTheRealHook:
    def test_full_hook_asks_naming_the_unresolved_variable(self, tmp_path):
        """Reddens if the unknown branch never reaches emit_ask, or if the
        reason it builds does not name the unresolved variable."""
        sid = "expand-unknown-ask-sid"
        _seed(sid, mode="conversation")
        out = _run_hook("cp README.md $%s/probe.txt" % SENTINEL_UNSET_VAR, sid, str(tmp_path))
        assert out is not None, "the unresolved-variable target was silent"
        assert out.get("permissionDecision") == "ask", out
        assert SENTINEL_UNSET_VAR in out.get("permissionDecisionReason", ""), out


class TestUnknownAskIsDeclaredInTheFiredrillCensus:
    def test_declared_ask_arm_is_a_real_working_refusal(self, tmp_path):
        """Ties tests/firedrill/_census.py's new ask-arm declaration to real
        coverage: reddens if that census entry stops matching a real subprocess
        ask, which is exactly what would make the declaration decorative rather
        than load-bearing."""
        from tests.firedrill._census import by_id
        from tests.firedrill._harness import make_isolation, run_hook

        entry = by_id("bash-write-unresolved-variable-ask")
        iso = make_isolation(tmp_path, session_id="expand-census-ask")
        setup = entry.setup(iso)
        result = run_hook(entry.script, setup["envelope"], iso,
                          extra_env=setup.get("extra_env"))
        assert result.permission_decision() == "ask", result.stdout
        audit = result.audit()
        assert audit, "audit stream is empty; the hook recorded nothing"
        rows = [r for r in audit
                if r.get("event") == "gate_decision" and r.get("gate") == entry.gate_name]
        assert rows, audit
        assert rows[-1].get("decision") == "ask", rows


# --------------------------------------------------------------------------- #
# capability 10: expansion reaches every write vector's target, not only `cp`.
# --------------------------------------------------------------------------- #
VECTORS: dict[str, str] = {
    "spaced_redirect": "echo x > %s",
    "glued_redirect": "echo x >%s",
    "tee": "tee %s",
    "dd_of": "dd if=/dev/zero of=%s",
    "sed_i": "sed -i s/a/b/ %s",
    "cp_dash_t": "cp -t %s README.md",
}
DISCRIMINATING_SPELLINGS = ["~/{p}", '"~/{p}"', "$HOME/{p}", "'$HOME/{p}'",
                           "$%s/{p}" % SENTINEL_UNSET_VAR]


class TestExpansionCrossesEveryWriteVector:
    """Reddens if expand_word is wired at one call site and the others keep
    calling dequote: each vector's classification must agree with the
    already-oracle-verified `cp` destination for the SAME spelling."""

    @pytest.mark.parametrize(
        "vector,tmpl",
        [(v, t) for v in VECTORS for t in DISCRIMINATING_SPELLINGS],
        ids=["%s-%s" % (v, t) for v in VECTORS for t in DISCRIMINATING_SPELLINGS],
    )
    def test_vector_agrees_with_the_cp_destination_reference(self, boundary_fixture, vector, tmpl):
        fx = boundary_fixture
        probe = "probe_vec_%s.txt" % vector
        dest = tmpl.format(p=probe)
        reference_rows = _extract_rows("cp README.md %s" % dest, fx.project, fx.env)
        vector_rows = _extract_rows(VECTORS[vector] % dest, fx.project, fx.env)
        if tmpl == "$%s/{p}" % SENTINEL_UNSET_VAR:
            ref_kinds = {r[0] for r in reference_rows if r}
            vec_kinds = {r[0] for r in vector_rows if r}
            assert "unknown" in ref_kinds, reference_rows
            assert "unknown" in vec_kinds, (vector, vector_rows)
            assert "local" not in vec_kinds and "outside" not in vec_kinds, (vector, vector_rows)
        else:
            reference = _classified_abspath(reference_rows)
            got = _classified_abspath(vector_rows)
            assert got == reference, (vector, dest, got, reference)

    def test_group_wrapped_cp_to_bare_tilde_is_expanded(self, boundary_fixture):
        """The group-wrapped `(cp README.md ~)` form: reddens if
        strip_unbalanced_close is not called as expand_word's own first step,
        since the token expand_word would otherwise see is `~)`, whose
        tilde-prefix runs to `)` rather than to a login name, staying literal
        while the shell itself still copies to $HOME."""
        fx = boundary_fixture
        rows = _extract_rows("(cp README.md ~)", fx.project, fx.env)
        abspath = _classified_abspath(rows)
        assert abspath == str(fx.home), rows


class TestAssignmentPositionSettledByOracle:
    """The two spellings plan.md deliberately leaves to the oracle rather than
    to anyone's reading of the bash manual, because the tilde sits after an `=`
    inside a word. Neither test hardcodes EXPANDS vs literal -- each computes
    the real bash answer at run time and asserts the extractor agrees with it,
    so a wrong guess here is impossible by construction; only a wrong extractor
    is."""

    def test_dd_of_assignment_position_tilde(self, boundary_fixture):
        fx = boundary_fixture
        probe = "probe_dd_of_assign.txt"
        whole_word = "of=~/%s" % probe
        oracle_whole = _oracle_word(whole_word, fx.project, fx.env)
        assert oracle_whole.startswith("of="), oracle_whole
        expected = _expected_abspath(fx.project, oracle_whole[len("of="):])
        rows = _extract_rows("dd if=/dev/zero of=~/%s" % probe, fx.project, fx.env)
        got = _classified_abspath(rows)
        assert got == expected, (oracle_whole, got, rows)

    def test_cp_target_directory_assignment_position_tilde(self, boundary_fixture):
        fx = boundary_fixture
        probe = "probe_cp_td_assign.txt"
        whole_word = "--target-directory=~/%s" % probe
        oracle_whole = _oracle_word(whole_word, fx.project, fx.env)
        assert oracle_whole.startswith("--target-directory="), oracle_whole
        expected = _expected_abspath(fx.project, oracle_whole[len("--target-directory="):])
        rows = _extract_rows("cp --target-directory=~/%s README.md" % probe, fx.project, fx.env)
        got = _classified_abspath(rows)
        assert got == expected, (oracle_whole, got, rows)


# --------------------------------------------------------------------------- #
# capability 11: interpreter-scanned paths are NOT expanded.
# --------------------------------------------------------------------------- #
class TestInterpreterScannedPathsAreNotExpanded:
    def test_interpreter_tilde_path_is_not_expanded(self, boundary_fixture):
        """Reddens if expand_word is applied to scan_tokens hits: the row would
        then carry the fixture home directory instead of today's un-expanded
        cwd-join."""
        fx = boundary_fixture
        cmd = "python3 -c \"open('~/x','w')\""
        rows = _extract_rows(cmd, fx.project, fx.env)
        local_hits = [r for r in rows if r and r[0] == "local"]
        assert local_hits, rows
        assert local_hits[0][1] == str(fx.project / "~" / "x"), rows


# --------------------------------------------------------------------------- #
# capability 12: HOME removed from both sides -- the passwd-database fallback,
# and $HOME itself becoming an unknown ask.
# --------------------------------------------------------------------------- #
class TestHomeRemovedFallbackAndUnknownAsk:
    def test_bare_tilde_still_resolves_via_the_password_database(self, boundary_fixture):
        """Reddens if the tilde branch reads only os.environ['HOME'] and has no
        fallback: with HOME genuinely absent, both the gate and a real bash fall
        back to the invoking user's own passwd-database home directory."""
        fx = boundary_fixture
        probe = "probe_home_removed.txt"
        rows = _extract_rows("cp README.md ~/%s" % probe, fx.project, fx.env_no_home)
        got = _classified_abspath(rows)
        oracle = _oracle_word("~/%s" % probe, fx.project, fx.env_no_home)
        expected = _expected_abspath(fx.project, oracle)
        assert got == expected, (got, expected, rows)

    def test_dollar_home_becomes_an_unknown_ask_when_home_is_removed(self, boundary_fixture):
        """The paired half: with HOME genuinely absent (a missing key, not a bad
        value), $HOME/probe.txt must ask rather than silently resolve to
        `/probe.txt` (the filesystem root) the way an ordinary unresolved
        variable does."""
        fx = boundary_fixture
        probe = "probe_home_removed_dollar.txt"
        rows = _extract_rows("cp README.md $HOME/%s" % probe, fx.project, fx.env_no_home)
        kinds = {r[0] for r in rows if r}
        assert "unknown" in kinds, rows
        assert "local" not in kinds and "outside" not in kinds, rows


# --------------------------------------------------------------------------- #
# capability 14: the header's UNEXPANDED FORMS block names exactly the forms
# the population records as divergences.
# --------------------------------------------------------------------------- #
class TestUnexpandedFormsHeaderRatchet:
    def test_header_names_exactly_the_populations_diverge_forms(self):
        """Reddens if a form gains expansion in the code without leaving the
        block, or if a divergence entry is added with no form name (the
        non-empty half is TestPopulationIsNonVacuous.
        test_every_diverge_entry_carries_a_form_name)."""
        src = Path(HOOK_SH).read_text()
        declared = _unexpanded_forms_from_header(src)
        population_forms = {c.form for c in DIVERGE_CASES}
        assert declared == population_forms, (declared, population_forms)


# --------------------------------------------------------------------------- #
# capability 15: the mirror block is untouched.
# --------------------------------------------------------------------------- #
class TestExpandWordDoesNotLiveInTheMirrorBlock:
    def test_expand_word_is_not_defined_inside_the_mirror_block(self):
        """Reddens if any part of expand_word is placed between MIRROR BEGIN and
        MIRROR END, the block writ-worktree-safety.sh and
        writ/session/bash_tokens.py mirror byte-for-byte."""
        src = Path(HOOK_SH).read_text()
        if "def expand_word(" not in src:
            pytest.fail(
                "skeleton: hooks/scripts/writ-bash-write-gate.sh has no "
                "expand_word function yet (plan.md ## Files)"
            )
        mirror_start = src.index("# MIRROR BEGIN split_control_operators")
        mirror_end = src.index("# MIRROR END split_control_operators")
        def_pos = src.index("def expand_word(")
        assert not (mirror_start <= def_pos <= mirror_end), (
            "expand_word must not live inside the MIRROR BEGIN/END block shared "
            "with writ-worktree-safety.sh and writ/session/bash_tokens.py"
        )


# --------------------------------------------------------------------------- #
# capability 16: the worktree hook's same-shaped blindness is RECORDED, not
# fixed. This is a pin on TODAY's verdict, so it is green now and reddens only
# if that verdict changes without a deliberate update to this test.
# --------------------------------------------------------------------------- #
class TestWorktreeHookBlindnessIsRecordedNotFixed:
    def test_git_worktree_add_tilde_evil_is_denied_with_the_gitignore_message_today(
        self, cache_root, project
    ):
        """Reddens if writ-worktree-safety.sh's verdict for this exact command
        changes without a deliberate update to this pin. If that change is a
        real fix for the worktree hook's own blind spot (plan.md ## Analysis
        section 4: it should `sys.exit(0)` because the shell actually creates
        the worktree OUTSIDE the project), update this test to match -- it is
        recorded as a KNOWN DEFECT this cycle, not fixed."""
        decision, reason = _run_worktree_hook("git worktree add ~/evil x", cache_root, project)
        assert decision == "deny", (decision, reason)
        assert "gitignore" in reason.lower(), reason
